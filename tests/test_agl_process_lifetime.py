"""Process lifetime in AgL hosts: runs settle or terminate forked children, REPL entries scope them.

Every scenario runs in the isolated runner (a single-threaded subprocess), because the process
host refuses to fork otherwise. A subprocess is costly, so a module-scoped fixture runs a whole
family of scenarios in one, and each test asserts its own scenario's results and output.
Companions fork through ``agl.runtime.processes``; a child announces its end on fd 1, so the
captured output shows what ended and in which order.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from agnette.agl import PipelineDriver
from agnette.agl.runtime.processes import ProcessHostError
from agnette.agl.runtime.sessions import AgentDispatcherSessionHost
from agnette.core.process import terminating_signals_exit
from tests._agl_helpers import agl_roots, repl_session_with_root, run_inline_code
from tests.agl.isolated_runner import run_callable

_LIBRARY_AGL = """\
@extern-name("spawn_sleeper")
extern def spawn(tag: text) -> text

@extern-name("spawn_finisher")
extern def finisher(tag: text) -> text

@extern-name("spawn_marker")
extern def marker(path: text) -> text

@extern-name("spawn_failure")
extern def failing(path: text) -> text

@extern-name("spawn_nested_failure")
extern def nested-failure(path: text) -> unit

@extern-name("spawn_stateful_child")
extern def spawn-stateful() -> unit

@extern-name("fork_noisy_hook")
extern def fork-noisy() -> unit

@extern-name("consume_outcome")
extern def consume(id: text) -> unit

@extern-name("detach_process")
extern def detach(id: text) -> unit

@extern-name("touch_state")
extern def touch() -> unit

@extern-name("forked_state")
extern def fork-state() -> text

@extern-name("interrupt_run")
extern def interrupt() -> unit

@extern-name("signal_self")
extern def signal-self(number: int) -> unit

@extern-name("sigint_when_blocked")
extern def sigint-when-blocked() -> unit
"""

_LIBRARY_PY = """\
import os
import subprocess
import sys
import time

from agl import runtime
from tests._proc_helpers import signal_when_sleeping_script, sleep_until_signalled
from tests.agl.programs.externs.process_host import forked_state


def _say(text):
    os.write(1, (text + "\\n").encode())


def _boom():
    raise RuntimeError("child failed")


def spawn_sleeper(tag):
    # Returns once the child is inside its `try`, so a termination reaches it there.
    ready, announce = os.pipe()

    def child():
        try:
            os.write(announce, b"r")
            sleep_until_signalled()
        finally:
            _say(f"{tag} terminated")
        return b""

    info = runtime.processes.fork(child, ())
    os.close(announce)
    os.read(ready, 1)
    os.close(ready)
    return info.id


def spawn_finisher(tag):
    def child():
        time.sleep(0.1)
        _say(f"{tag} finished")
        return b""

    return runtime.processes.fork(child, ()).id


def spawn_marker(path):
    def child():
        time.sleep(0.2)
        open(path, "w").close()
        return b""

    return runtime.processes.fork(child, ()).id


def spawn_failure(path):
    info = runtime.processes.fork(_boom, ())
    with open(path, "a") as pids:
        pids.write(f"{info.pid}\\n")
    return info.id


def spawn_nested_failure(path):
    # The child forks a failing grandchild of its own and never consumes it.
    host = runtime.processes

    def child():
        with open(path, "a") as pids:
            pids.write(f"{os.getpid()}\\n{host.fork(_boom, ()).pid}\\n")
        return b""

    host.outcome(host.fork(child, ()).id)


def spawn_stateful_child():
    host = runtime.processes

    def child():
        runtime.state("own", lambda: 1, lambda value: _say("child state closed"))
        return b""

    host.outcome(host.fork(child, ()).id)


def fork_noisy_hook():
    # A fork hook leaves unterminated output in the stdout buffer.
    def hook(value):
        sys.stdout.write("hook ran;")
        return value

    runtime.state("noisy", lambda: 1, lambda value: None, fork=hook)
    host = runtime.processes
    host.outcome(host.fork(lambda: b"", ()).id)
    sys.stdout.write("\\n")


def consume_outcome(id):
    runtime.processes.outcome(id)


def detach_process(id):
    runtime.processes.detach(id)


def touch_state():
    runtime.state("touched", lambda: 1, lambda value: _say("state closed"))


def interrupt_run():
    raise KeyboardInterrupt


def signal_self(number):
    os.kill(os.getpid(), number)
    time.sleep(5)


_senders = []


def sigint_when_blocked():
    # An outside process (not one of the host's children) interrupts this one once it is blocked.
    script = signal_when_sleeping_script(os.getpid(), 2)
    _senders.append(subprocess.Popen(["sh", "-c", script]))
"""


class _MarkingSessions(AgentDispatcherSessionHost):
    """Announces its cleanup in the owning process, and in forked children if *in_children*."""

    def __init__(self, in_children: bool = False) -> None:
        super().__init__(None)
        self._owner = os.getpid()
        self._in_children = in_children
        self.announce = True

    def close_all(self) -> None:
        owner = os.getpid() == self._owner
        if self.announce and (owner or self._in_children):
            os.write(1, (b"" if owner else b"child ") + b"sessions closed\n")
        super().close_all()


def _write_library(workdir: Path) -> None:
    (workdir / "procs.agl").write_text(_LIBRARY_AGL)
    (workdir / "procs.py").write_text(_LIBRARY_PY)


def _begin_case(name: str) -> None:
    """Start the output of scenario *name* in the shared stdout."""
    os.write(1, f"=={name}==\n".encode())


def _wait_for(path: Path) -> bool:
    deadline = time.monotonic() + 30
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return path.exists()


@dataclass(frozen=True)
class _Scenarios:
    """What one isolated run returned for each scenario, and the output it produced."""

    results: dict[str, Any]
    output: dict[str, str]


def _run_scenarios(
    function: Callable[..., Any],
    tmp_path_factory: pytest.TempPathFactory,
    name: str,
    cases: list[str] | None = None,
) -> _Scenarios:
    """Run *function* (given *cases*, the scenarios to run, if any) in one isolated run."""
    args = None if cases is None else {"cases": cases}
    run = run_callable(function, tmp_path_factory.mktemp(name), args)
    sections = re.split(r"^==(\w+)==\n", run.stdout, flags=re.MULTILINE)
    return _Scenarios(run.value, dict(zip(sections[1::2], sections[2::2], strict=True)))


def _trace_records(trace: Path, kind: str) -> list[dict[str, Any]]:
    records = (json.loads(line) for line in trace.read_text().splitlines())
    return [record for record in records if record["kind"] == kind]


# ---------------------------------------------------------------------------
# Runs (the signal handling of ``agm exec``)
# ---------------------------------------------------------------------------


def _run_program(
    workdir: Path, body: str, *, in_children: bool = False, trace_file: Path | None = None
) -> dict[str, Any]:
    """Run *body* as a script importing the library, under the signal handling of ``agm exec``."""
    runtime = PipelineDriver(
        session_host=_MarkingSessions(in_children),
        get_sandbox_context=None,
        resolve_agent_spec=None,
    )
    source = f"import procs::*\nimport std/os\n{body}\n"
    report: dict[str, Any] = {"exit": None, "ok": None, "interrupted": False}
    stderr = io.StringIO()
    try:
        with terminating_signals_exit(), contextlib.redirect_stderr(stderr):
            result = run_inline_code(
                runtime,
                source,
                roots=agl_roots(workdir),
                default_stdlib=True,
                trace_file=trace_file,
            )
        report["ok"] = result.ok
        report["raised"] = result.error is not None
    except SystemExit as exit_request:
        report["exit"] = exit_request.code
    except KeyboardInterrupt:
        report["interrupted"] = True
    report["stderr"] = stderr.getvalue()
    return report


_RUN_END_CLEANUP = "sessions closed\nstate closed\n"

# A program that forks a long sleeper, then ends abnormally in the given way.
_ABNORMAL_ENDS = {
    "raise": ('raise Abort(message = "boom")', {"raised": True}),
    "interrupt": ("interrupt()", {"interrupted": True}),
    "exit": ("os::exit(3)", {"exit": 3}),
    "sigterm": (f"signal-self({int(signal.SIGTERM)})", {"exit": 128 + signal.SIGTERM}),
    "sighup": (f"signal-self({int(signal.SIGHUP)})", {"exit": 128 + signal.SIGHUP}),
}


def _unconsumed_failures(workdir: Path, name: str, trace_file: Path | None) -> dict[str, Any]:
    """Fork two failing children, consume the second one's outcome, and end the run."""
    pids = workdir / f"{name}.pids"
    body = f'let kept = failing("{pids}")\nlet consumed = failing("{pids}")\nconsume(consumed)\n'
    report = _run_program(workdir, body, trace_file=trace_file)
    return {
        "report": report,
        "pids": [int(line) for line in pids.read_text().split()],
        "records": _trace_records(trace_file, "process_unconsumed") if trace_file else [],
        "writer": os.getpid(),
    }


def _run_scenario_family(workdir: Path, cases: list[str]) -> dict[str, Any]:
    _write_library(workdir)
    survivor = workdir / "survivor"
    plans: dict[str, tuple[str, dict[str, Any]]] = {
        "normal": ('touch()\nlet id = finisher("child")', {}),
        **{
            name: (f'touch()\nlet id = spawn("child")\n{trigger}', {})
            for name, (trigger, _) in _ABNORMAL_ENDS.items()
        },
        "detached": (f'detach(marker("{survivor}"))', {}),
        "child_teardown": ("touch()\nspawn-stateful()", {"in_children": True}),
        "noisy_hook": ("fork-noisy()", {}),
    }
    traces = {"traced": workdir / "trace.jsonl", "untraced": None}
    results: dict[str, Any] = {}
    for name in cases:
        _begin_case(name)
        if name in traces:
            results[name] = _unconsumed_failures(workdir, name, traces[name])
        else:
            body, options = plans[name]
            results[name] = _run_program(workdir, body, **options)
    if "detached" in cases:
        results["survived"] = _wait_for(survivor)
    return results


# Few scenarios per isolated run, so that no test's run outgrows the CPU budget.


@pytest.fixture(scope="module")
def ends(tmp_path_factory: pytest.TempPathFactory) -> _Scenarios:
    cases = ["normal", *_ABNORMAL_ENDS]
    return _run_scenarios(_run_scenario_family, tmp_path_factory, "ends", cases)


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> _Scenarios:
    cases = ["detached", "child_teardown", "noisy_hook", "traced", "untraced"]
    return _run_scenarios(_run_scenario_family, tmp_path_factory, "runs", cases)


def test_a_normal_end_settles_children_before_sessions_and_companion_state(
    ends: _Scenarios,
) -> None:
    assert ends.results["normal"]["ok"]
    assert ends.output["normal"] == "child finished\n" + _RUN_END_CLEANUP


@pytest.mark.parametrize("name", sorted(_ABNORMAL_ENDS))
def test_an_abnormal_end_terminates_children_before_sessions_and_companion_state(
    ends: _Scenarios, name: str
) -> None:
    expected = _ABNORMAL_ENDS[name][1]
    report = ends.results[name]
    assert {key: report[key] for key in expected} == expected
    assert ends.output[name] == "child terminated\n" + _RUN_END_CLEANUP


def test_an_unconsumed_failure_at_a_normal_end_is_warned_and_traced(runs: _Scenarios) -> None:
    value = runs.results["traced"]
    kept_pid, consumed_pid = value["pids"]
    [record] = value["records"]
    assert record["child_pid"] == kept_pid
    assert record["pid"] == value["writer"]
    assert f"process {kept_pid} " in value["report"]["stderr"]
    assert f"process {consumed_pid} " not in value["report"]["stderr"]


def test_an_unconsumed_failure_is_warned_without_a_trace_file(runs: _Scenarios) -> None:
    value = runs.results["untraced"]
    assert f"process {value['pids'][0]} " in value["report"]["stderr"]


def test_a_detached_child_survives_the_run_end(runs: _Scenarios) -> None:
    assert runs.results["detached"]["ok"]
    assert runs.results["survived"]


def test_a_child_closes_its_sessions_before_its_companion_state(runs: _Scenarios) -> None:
    # The child's own state is closed last in the child; the run's sessions, then its state.
    assert runs.output["child_teardown"] == (
        "child sessions closed\nchild state closed\n" + _RUN_END_CLEANUP
    )


def test_output_a_fork_hook_leaves_buffered_is_not_repeated_by_the_child(
    runs: _Scenarios,
) -> None:
    assert runs.output["noisy_hook"] == "hook ran;\nsessions closed\n"


# ---------------------------------------------------------------------------
# REPL sessions
# ---------------------------------------------------------------------------

_IMPORT = "import procs::*"


def _drive_repl(
    workdir: Path, steps: list[str], *, marking: bool = False, trace: Path | None = None
) -> list[bool]:
    """Evaluate the entries among *steps*, recording each one's success.

    Directives act on the session directly: ``!close``, ``!reset``, ``!mark X`` (announce X),
    ``!interrupted-reset`` (a reset during which ending the processes is interrupted; records
    whether the interrupt escaped), ``!fork-handle`` (fork and settle a child through the
    session's process host) and ``!handle-known`` (records whether that handle is still known).
    """
    sessions = _MarkingSessions() if marking else None
    session = repl_session_with_root(workdir, session_host=sessions, trace_path=trace)
    roots = session._roots
    outcomes = []
    handle = ""
    try:
        for step in steps:
            if step == "!close":
                session.close()
            elif step == "!reset":
                session.reset()
                # The reset forgets the root the helper installed.
                session._roots = roots
            elif step == "!interrupted-reset":
                with mock.patch.object(session._process_host, "end", side_effect=KeyboardInterrupt):
                    try:
                        session.reset()
                        outcomes.append(False)
                    except KeyboardInterrupt:
                        outcomes.append(True)
                session._roots = roots
            elif step.startswith("!mark "):
                os.write(1, f"--{step.removeprefix('!mark ')}--\n".encode())
            elif step == "!fork-handle":
                handle = session._process_host.fork(lambda: b"", ()).id
                session._process_host.settle(handle)
            elif step == "!handle-known":
                try:
                    session._process_host.settle(handle)
                    outcomes.append(True)
                except ProcessHostError:
                    outcomes.append(False)
            else:
                outcomes.append(session.eval_entry(step).ok)
    finally:
        if sessions is not None:
            sessions.announce = False
        session.close()
    return outcomes


_Plans = dict[str, tuple[list[str], dict[str, Any]]]


def _run_plans(workdir: Path, plans: _Plans, cases: list[str]) -> dict[str, Any]:
    """Drive each plan of *cases* in a session of its own; the results are its entries' successes.

    An isolated run drives few sessions, so that no test's run outgrows the CPU budget.
    """
    _write_library(workdir)
    results: dict[str, Any] = {}
    for name in cases:
        steps, options = plans[name]
        _begin_case(name)
        results[name] = _drive_repl(workdir, steps, **options)
    return results


def _entry_scope_family(workdir: Path, cases: list[str]) -> dict[str, Any]:
    survivor = workdir / "survivor"

    def failing_entry(trigger: str) -> list[str]:
        return [
            _IMPORT,
            'spawn("earlier")',
            f'let failed = spawn("failed")\n{trigger}',
            "!mark after the failed entry",
            "!close",
        ]

    plans: _Plans = {
        "raise": (failing_entry('raise Abort(message = "boom")'), {}),
        "interrupt": (failing_entry("interrupt()"), {}),
        # A SIGINT from another process while the entry waits on its child, as a terminal's Ctrl-C.
        "sigint": (failing_entry("sigint-when-blocked()\nconsume(failed)"), {}),
        "close": (
            [
                _IMPORT,
                "touch()",
                'spawn("one")',
                'spawn("two")',
                f'detach(marker("{survivor}"))',
                "!mark before",
                "!close",
            ],
            {"marking": True},
        ),
        # The first child closes its state at exit; the second entry sets `debug` first, so
        # its child keeps the state. The parent's own state is kept too (`debug` is then on).
        "debug": (
            [_IMPORT, "fork-state()", "import std/config\nstd/config::debug := true\nfork-state()"],
            {},
        ),
    }
    results = _run_plans(workdir, plans, cases)
    if "close" in cases:
        results["survived"] = _wait_for(survivor)
    return results


def _reset_family(workdir: Path, cases: list[str]) -> dict[str, Any]:
    pids = workdir / "pids"
    trace = workdir / "trace.jsonl"
    survivor = workdir / "survivor"
    plans: _Plans = {
        "reset": (
            [
                _IMPORT,
                "touch()",
                'spawn("before")',
                f'detach(marker("{survivor}"))',
                "!fork-handle",
                "!handle-known",
                "!reset",
                "!handle-known",
                "!mark reset done",
                _IMPORT,
                'spawn("after")',
            ],
            {"marking": True},
        ),
        "interrupted_reset": (
            [_IMPORT, "touch()", "let kept = 1", "!interrupted-reset", "kept"],
            {"marking": True},
        ),
        "nested_failure": ([_IMPORT, f'nested-failure("{pids}")', "!close"], {"trace": trace}),
    }
    results = _run_plans(workdir, plans, cases)
    if "reset" in cases:
        results["survived"] = _wait_for(survivor)
    if "nested_failure" in cases:
        results["pids"] = [int(line) for line in pids.read_text().split()]
        results["records"] = _trace_records(trace, "process_unconsumed")
    return results


# One REPL session per isolated run: sessions cost the most.


@pytest.fixture(scope="module")
def closes(tmp_path_factory: pytest.TempPathFactory) -> _Scenarios:
    return _run_scenarios(_entry_scope_family, tmp_path_factory, "closes", ["close"])


@pytest.fixture(scope="module")
def resets(tmp_path_factory: pytest.TempPathFactory) -> _Scenarios:
    return _run_scenarios(_reset_family, tmp_path_factory, "resets", ["reset"])


@pytest.mark.parametrize("name", ["raise", "interrupt", "sigint"])
def test_a_failed_entry_terminates_only_its_own_children(
    tmp_path_factory: pytest.TempPathFactory, name: str
) -> None:
    failed = _run_scenarios(_entry_scope_family, tmp_path_factory, name, [name])
    assert failed.results[name] == [True, True, False]
    assert failed.output[name] == (
        "failed terminated\n--after the failed entry--\nearlier terminated\n"
    )


def test_close_terminates_the_children_of_every_entry_then_closes_sessions_and_state(
    closes: _Scenarios,
) -> None:
    before, *children, sessions, state = closes.output["close"].splitlines()
    assert before == "--before--"
    assert sorted(children) == ["one terminated", "two terminated"]
    assert (sessions, state) == ("sessions closed", "state closed")


def test_a_detached_child_survives_repl_close(closes: _Scenarios) -> None:
    assert closes.results["survived"]


def test_a_repl_child_carries_the_companion_state_hooks_and_the_entrys_debug_setting(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    debug = _run_scenarios(_entry_scope_family, tmp_path_factory, "debug", ["debug"])
    assert debug.results["debug"] == [True, True, True]
    assert debug.output["debug"] == "closed parent>child\n"


def test_reset_ends_processes_then_sessions_then_state_and_the_session_stays_usable(
    resets: _Scenarios,
) -> None:
    assert resets.results["reset"][-2:] == [True, True]
    assert resets.output["reset"] == (
        "before terminated\n" + _RUN_END_CLEANUP + "--reset done--\nafter terminated\n"
    )


def test_reset_forgets_the_handles_of_the_processes_it_ended(resets: _Scenarios) -> None:
    known_before, known_after = resets.results["reset"][4:6]
    assert (known_before, known_after) == (True, False)


def test_a_detached_child_survives_a_reset(resets: _Scenarios) -> None:
    assert resets.results["survived"]


def test_reset_completes_when_ending_the_processes_is_interrupted_again(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    name = "interrupted_reset"
    interrupted = _run_scenarios(_reset_family, tmp_path_factory, name, [name])
    # The interrupt escapes, yet the sessions and companion state closed and the session cleared.
    assert interrupted.results[name] == [True, True, True, True, False]
    assert interrupted.output[name] == _RUN_END_CLEANUP


def test_a_repl_child_traces_the_failure_of_a_grandchild_nobody_consumed(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    name = "nested_failure"
    nested = _run_scenarios(_reset_family, tmp_path_factory, name, [name])
    child_pid, grandchild_pid = nested.results["pids"]
    [record] = nested.results["records"]
    assert record["child_pid"] == grandchild_pid
    assert record["pid"] == child_pid
