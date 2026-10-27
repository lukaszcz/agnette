"""The isolated runner: fresh single-threaded process, children's output, coverage data."""

from __future__ import annotations

import inspect
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import coverage
import pytest

from agnette.agl.self_validation import self_validation_enabled
from agnette.core import http as core_http
from agnette.util.text import format_description_column
from tests import _durations, _zygote
from tests.agl import isolated_runner
from tests.agl.isolated_runner import run_callable, run_scenario

if TYPE_CHECKING:
    from tests.agl.scenario_harness import CompilationCache

FORK_PROGRAM = Path(__file__).parent / "agl" / "programs" / "externs" / "isolated_fork.agl"


def _report_environment(workdir: Path) -> dict[str, Any]:
    return {
        "threads": len(sys._current_frames()),
        "cwd": os.getcwd(),
        "coverage": os.environ.get("COVERAGE_PROCESS_START"),
        "workdir": str(workdir),
        "self_validation": self_validation_enabled(),
        "sigint_default": signal.getsignal(signal.SIGINT) is signal.default_int_handler,
        "session_leader": os.getsid(0) == os.getpid(),
        "stdin_is_null": os.fstat(0).st_rdev == os.stat(os.devnull).st_rdev,
    }


def _parent_pid(workdir: Path) -> int:
    """The pid of the process that started this one: the zygote, or else the pytest process."""
    return os.getppid()


def _burn_cpu(workdir: Path, seconds: float, in_child: bool) -> None:
    """Spend *seconds* of CPU, in a forked child that the caller waits for if *in_child*."""
    if in_child:
        pid = os.fork()
        if pid == 0:
            _burn_cpu(workdir, seconds, False)
            os._exit(0)
        os.waitpid(pid, 0)
        return
    end = time.process_time() + seconds
    while time.process_time() < end:
        pass


def _kill_the_zygote(workdir: Path, worker: int) -> None:
    """Kill the process that started this one, then wait to be killed in turn."""
    zygote = os.getppid()
    assert zygote != worker, "this run was not served by a zygote"
    (workdir / "pid").write_text(str(os.getpid()))
    os.kill(zygote, signal.SIGKILL)
    time.sleep(60)


def _note_and_sleep(workdir: Path) -> None:
    """Record one execution, then wait to be killed."""
    with (workdir / "ran").open("a") as ran:
        ran.write("x\n")
    time.sleep(60)


def _fail(workdir: Path) -> None:
    raise AssertionError("boom from the subprocess")


def _skip(workdir: Path) -> None:
    pytest.skip("skipped in the subprocess")


def _forked_output(workdir: Path, word: str) -> int:
    """Print from the parent and from a forked child that bypasses interpreter teardown."""
    sys.stdout.write("parent says hi\n")
    pid = os.fork()
    if pid == 0:
        os.write(1, f"child says {word}\n".encode())
        os._exit(7)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def _fork_and_return_in_child(workdir: Path) -> str:
    """Fork a child that returns from the callable instead of exiting."""
    pid = os.fork()
    if pid == 0:
        return "child"
    os.waitpid(pid, 0)
    return "parent"


def _leave_a_grandchild(workdir: Path, own_group: bool = False) -> int:
    """Return the pid of a long-lived child, in the same process group or in one of its own."""
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        process_group=0 if own_group else None,
    )
    return sleeper.pid


def _try_real_externals(workdir: Path) -> dict[str, str]:
    """Attempt a real agent CLI, HTTP request, and `httpx2` send; report which were refused."""
    import httpx2

    results = {}
    with tempfile.TemporaryDirectory() as directory:
        cli = Path(directory) / "claude"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)
        try:
            subprocess.run([str(cli)], check=False)
            results["agent"] = "ran"
        except AssertionError:
            results["agent"] = "refused"
    try:
        core_http.open_session().get("http://example.invalid/")
        results["http"] = "sent"
    except AssertionError:
        results["http"] = "refused"
    try:
        httpx2.HTTPTransport().handle_request(httpx2.Request("GET", "http://example.invalid/"))
        results["httpx2"] = "sent"
    except pytest.fail.Exception:
        results["httpx2"] = "refused"
    return results


def _sleep_forever(workdir: Path) -> None:
    time.sleep(60)


def _body_lines(function: Any) -> set[int]:
    """Executable lines of *function*'s own body, excluding its `def` line."""
    code = function.__code__
    return {line for _, _, line in code.co_lines() if line and line != code.co_firstlineno}


def _is_gone(pid: int) -> bool:
    """Whether *pid* no longer runs (a zombie awaiting its reaper counts as gone)."""
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return stat.exists() and stat.read_text().rpartition(")")[2].split()[0] == "Z"
    except (FileNotFoundError, ProcessLookupError):
        return True


def _wait_until_gone(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while not _is_gone(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    return _is_gone(pid)


@pytest.fixture(params=["zygote", "cold"])
def launch(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the test once served by the worker's zygote and once by a cold subprocess."""
    monkeypatch.setattr(isolated_runner, "_zygote_enabled", request.param == "zygote")
    return str(request.param)


@pytest.fixture()
def zygote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let runs use the worker's zygote, whatever ``AGM_TEST_NO_ZYGOTE`` said."""
    monkeypatch.setattr(isolated_runner, "_zygote_enabled", True)


@pytest.fixture()
def own_zygote(monkeypatch: pytest.MonkeyPatch, zygote: None) -> Iterator[None]:
    """Give the test a zygote of its own, started by its first run and ended with the test."""
    monkeypatch.setattr(isolated_runner, "_warm", None)
    yield
    isolated_runner._stop_warm()


@pytest.fixture()
def private_zygote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pytestconfig: pytest.Config, own_zygote: None
) -> Iterator[Path]:
    """Make the suite's coverage one that writes to a file of this test, with a zygote of its own.

    Yields the data file; the zygote, started by the first run, ends with the test.
    """
    data_file = tmp_path / "cov" / ".coverage"
    active = coverage.Coverage(data_file=str(data_file), config_file=False)
    monkeypatch.setattr(coverage.Coverage, "current", classmethod(lambda cls: active))
    session_coverage = isolated_runner._coverage_environment(
        pytestconfig.invocation_params.dir, None
    )
    monkeypatch.setattr(isolated_runner, "_session_coverage", session_coverage)
    yield data_file


def _assert_child_only_lines_recorded(data_file: Path) -> None:
    """Combine *data_file*'s data: `src/` lines that only a forked child ran must be in it."""
    data_dir = data_file.parent
    source = inspect.getsourcefile(format_description_column)
    assert source is not None
    body = _body_lines(format_description_column)

    assert len(list(data_dir.glob(".coverage.*"))) >= 2
    cov = coverage.Coverage(data_file=str(data_file), config_file=False)
    cov.combine()
    combined = cov.get_data().lines(source)
    assert combined is not None
    assert body & set(combined)
    assert not list(data_dir.glob(".coverage.*"))


def test_lines_run_only_by_a_forked_child_are_recorded(tmp_path: Path) -> None:
    """The fixture's parent never calls the `src/` helper; only its forked child does."""
    data_file = tmp_path / "cov" / ".coverage"
    run_scenario(FORK_PROGRAM, "forked_child_work", tmp_path, coverage_data=data_file)
    _assert_child_only_lines_recorded(data_file)


def test_a_zygotes_children_and_grandchildren_save_their_coverage(
    tmp_path: Path, private_zygote: Path
) -> None:
    run_scenario(FORK_PROGRAM, "forked_child_work", tmp_path)
    _assert_child_only_lines_recorded(private_zygote)


def test_a_zygote_records_no_coverage_of_its_own(tmp_path: Path, private_zygote: Path) -> None:
    run_callable(_parent_pid, tmp_path)
    isolated_runner._stop_warm()
    assert len(list(private_zygote.parent.glob(".coverage.*"))) == 1  # the child's


def test_children_output_is_captured_at_fd_level(tmp_path: Path, launch: str) -> None:
    result = run_callable(_forked_output, tmp_path, {"word": "hello"})
    assert result.value == 7
    assert result.stdout == "parent says hi\nchild says hello\n"


def test_a_forked_child_that_returns_does_not_overwrite_the_report(
    tmp_path: Path, launch: str
) -> None:
    assert run_callable(_fork_and_return_in_child, tmp_path).value == "parent"


def test_the_subprocess_runs_in_the_suites_environment(tmp_path: Path, launch: str) -> None:
    value = run_callable(_report_environment, tmp_path).value
    assert value["threads"] == 1
    assert value["workdir"] == str(tmp_path)
    assert Path(value["cwd"]) == Path(__file__).parent.parent
    assert value["self_validation"]
    assert value["session_leader"]
    assert value["stdin_is_null"]


def test_the_subprocess_restores_the_default_interrupt_handler(tmp_path: Path, launch: str) -> None:
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        value = run_callable(_report_environment, tmp_path).value
    finally:
        signal.signal(signal.SIGINT, previous)
    assert value["sigint_default"]


def test_the_subprocess_refuses_real_agent_clis_and_network_requests(
    tmp_path: Path, launch: str
) -> None:
    refused = run_callable(_try_real_externals, tmp_path).value
    assert refused == {"agent": "refused", "http": "refused", "httpx2": "refused"}


def test_without_active_coverage_the_subprocess_is_not_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coverage.Coverage, "current", classmethod(lambda cls: None))
    monkeypatch.setenv("COVERAGE_PROCESS_START", "inherited.toml")
    assert run_callable(_report_environment, tmp_path).value["coverage"] is None


def test_active_coverage_routes_subprocess_data_to_its_data_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = coverage.Coverage(data_file=str(tmp_path / ".active"), config_file=False)
    monkeypatch.setattr(coverage.Coverage, "current", classmethod(lambda cls: active))
    assert run_callable(_report_environment, tmp_path).value["coverage"] is not None
    assert list(tmp_path.glob(".active.*"))


def test_a_relative_coverage_data_file_is_resolved_against_the_invocation_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pytestconfig: pytest.Config
) -> None:
    invocation_dir = pytestconfig.invocation_params.dir
    name = f".relative-{os.getpid()}"
    active = coverage.Coverage(data_file=name, config_file=False)
    monkeypatch.setattr(coverage.Coverage, "current", classmethod(lambda cls: active))
    monkeypatch.chdir(tmp_path)
    try:
        run_callable(_report_environment, tmp_path)
        written = list(invocation_dir.glob(f"{name}.*"))
        assert written
    finally:
        for path in invocation_dir.glob(f"{name}.*"):
            path.unlink()
    assert not list(tmp_path.glob(f"{name}.*"))


def test_a_failure_in_the_subprocess_fails_the_caller_with_its_traceback(
    tmp_path: Path, launch: str
) -> None:
    with pytest.raises(pytest.fail.Exception, match="boom from the subprocess"):
        run_callable(_fail, tmp_path)


def test_a_failing_scenario_reports_the_actual_output(tmp_path: Path) -> None:
    program = tmp_path / "failing.agl"
    program.write_text('program def main() -> unit =\n  print "the actual output"\n')
    sidecar = {"scenarios": [{"name": "mismatch", "expect": {"stdout": "something else\n"}}]}
    program.with_name("failing.scenarios.json").write_text(json.dumps(sidecar))
    with pytest.raises(pytest.fail.Exception, match="the actual output"):
        run_scenario(program, "mismatch", tmp_path)


def test_a_skip_in_the_subprocess_skips_the_caller(tmp_path: Path, launch: str) -> None:
    with pytest.raises(pytest.skip.Exception, match="skipped in the subprocess"):
        run_callable(_skip, tmp_path)


def test_a_subprocess_that_overruns_its_timeout_fails_the_caller(
    tmp_path: Path, launch: str
) -> None:
    with pytest.raises(pytest.fail.Exception, match="timed out"):
        run_callable(_sleep_forever, tmp_path, timeout=0.5)


@pytest.mark.parametrize("own_group", [False, True])
def test_children_left_in_the_session_are_killed_when_the_run_ends(
    tmp_path: Path, own_group: bool, launch: str
) -> None:
    pid = run_callable(_leave_a_grandchild, tmp_path, {"own_group": own_group}).value
    assert _wait_until_gone(pid)


def test_a_run_is_served_by_the_zygote_when_it_may_be(tmp_path: Path, launch: str) -> None:
    parent = run_callable(_parent_pid, tmp_path).value
    assert (parent == os.getpid()) == (launch == "cold")


def test_a_run_with_its_own_coverage_data_gets_a_cold_process(tmp_path: Path, zygote: None) -> None:
    served = run_callable(_parent_pid, tmp_path, coverage_data=tmp_path / ".cov").value
    assert served == os.getpid()


def test_a_zygote_runs_cpu_is_charged_to_the_calling_test(tmp_path: Path, zygote: None) -> None:
    run_callable(_parent_pid, tmp_path)  # starts the zygote, whose own cost is nobody's
    before = _durations.cpu_seconds()
    run_callable(_burn_cpu, tmp_path, {"seconds": 0.3, "in_child": True})
    assert _durations.cpu_seconds() - before >= 0.3


def test_a_zygote_that_died_between_runs_is_replaced(tmp_path: Path, zygote: None) -> None:
    first = run_callable(_parent_pid, tmp_path).value
    os.kill(first, signal.SIGKILL)
    assert _wait_until_gone(first)

    second = run_callable(_parent_pid, tmp_path).value
    assert second not in (first, os.getpid())


def test_a_zygote_that_dies_during_a_run_fails_it_and_is_replaced(
    tmp_path: Path, zygote: None
) -> None:
    first = run_callable(_parent_pid, tmp_path).value
    with pytest.raises(pytest.fail.Exception):
        run_callable(_kill_the_zygote, tmp_path, {"worker": os.getpid()})
    assert _wait_until_gone(first)
    assert _wait_until_gone(int((tmp_path / "pid").read_text()))

    second = run_callable(_parent_pid, tmp_path).value
    assert second not in (first, os.getpid())


_FORK_HARNESS_PROGRAM = """\
import std/ps

def child() -> int = 1

def asking() -> text =
  let out: text = exec "echo hi"
  out

program def main(mode: int) -> unit =
  if mode != 1 =>
    print ps::wait(ps::fork(child))
  if mode != 0 =>
    print ps::wait(ps::fork(asking))
"""

_EMPTY_SECTION: dict[str, Any] = {"expect": {}}
_SHELL_SECTION: dict[str, Any] = {"shell": [{"command": "echo hi", "stdout": "hi\n"}], "expect": {}}


def _write_harness_program(directory: Path, source: str, scenarios: list[dict[str, Any]]) -> Path:
    """Write the program *source* and its sidecar of *scenarios* to *directory*."""
    directory.mkdir()
    program = directory / "harness.agl"
    program.write_text(source)
    program.with_name("harness.scenarios.json").write_text(json.dumps({"scenarios": scenarios}))
    return program


def _harness_accepts(
    program: Path,
    scenario: dict[str, Any],
    workdir: Path,
    compilations: CompilationCache,
    *,
    stdout: str = "",
    observe_stderr: bool = False,
) -> bool:
    """Whether the corpus harness passes *scenario* of *program* (run inside the subprocess).

    Observing stderr reads what this process writes to fd 2, as the isolated runner does.
    """
    from tests.agl.scenario_harness import assert_program_scenario

    stderr = isolated_runner.StderrTail(Path("/proc/self/fd/2")) if observe_stderr else None
    with pytest.MonkeyPatch.context() as patch:
        try:
            assert_program_scenario(
                program,
                scenario,
                lambda: stdout,
                workdir,
                patch,
                compilations,
                None if stderr is None else stderr.read,
            )
        except (AssertionError, pytest.fail.Exception):
            return False
    return True


def test_scripted_forks_are_checked_in_every_process(tmp_path: Path) -> None:
    # The first child makes no calls, the second one runs its scripted shell command.
    scenario = {
        "name": "s",
        "isolated": True,
        "params": {"mode": 2},
        "forks": {"1": _EMPTY_SECTION, "2": _SHELL_SECTION},
        "expect": {},
    }
    program = _write_harness_program(tmp_path / "ok", _FORK_HARNESS_PROGRAM, [scenario])

    run_scenario(program, "s", tmp_path)


_BROKEN_FORKS: dict[str, tuple[int, dict[str, Any]]] = {
    "path-never-forked": (0, {"1": _EMPTY_SECTION, "2": _EMPTY_SECTION}),
    "script-unconsumed": (0, {"1": _SHELL_SECTION}),
    "unscripted-call": (1, {}),
}


def _fork_scripts_accepted(workdir: Path) -> dict[str, bool]:
    """Whether the harness accepts each broken fork script (run inside the subprocess)."""
    scenarios = [
        {"name": name, "params": {"mode": mode}, "forks": forks, "expect": {}}
        for name, (mode, forks) in _BROKEN_FORKS.items()
    ]
    program = _write_harness_program(workdir / "forks", _FORK_HARNESS_PROGRAM, scenarios)
    compilations: CompilationCache = {}
    return {
        scenario["name"]: _harness_accepts(program, scenario, workdir, compilations)
        for scenario in scenarios
    }


def test_a_fork_script_that_is_not_followed_fails_the_scenario(tmp_path: Path) -> None:
    accepted = run_callable(_fork_scripts_accepted, tmp_path).value

    assert accepted == dict.fromkeys(_BROKEN_FORKS, False)


_PROCESS_HARNESS_PROGRAM = """\
import std/ps

exception Boom extends Exception

def failing() -> int = raise Boom(message = "m")

program def main() -> unit =
  print "first"
  let ok = ps::fork(fn() => 1)
  let bad = ps::fork(failing)
  print ps::wait(ok)
"""

_UNCONSUMED = {"kind": "process_unconsumed", "pid": "${root}", "child_pid": "${2}"}
_PROCESS_EXPECT: dict[str, Any] = {
    "stderr": "warning: process ${2} failed: Boom: m\n",
    "stderr_contains": ["process ${2} failed"],
    "stderr_not_contains": ["process ${1}"],
    "trace": [
        {"pid": "${root}", "kind": "print", "rendered": "first"},
        {"pid": "${root}", "kind": "fork", "child_pid": "${1}"},
        {"pid": "${root}", "kind": "exit", "child_pid": "${1}", "raised": False},
        {**_UNCONSUMED, "problem": "failed: Boom: m"},
    ],
    "trace_absent": [{"kind": "process_unconsumed", "child_pid": "${1}"}],
}
_PROCESS_FORKS: dict[str, Any] = {
    "1": {"expect": {"ends": "settle"}},
    "2": {"expect": {"ends": "terminate"}},
}

# Each variant changes the expectations or the forks of the valid scenario above, and the
# harness must reject it. Only the last one cannot observe stderr.
_BROKEN_PROCESS_VARIANTS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "stderr-differs": ({"stderr": "warning: process ${1} failed: Boom: m\n"}, {}),
    "stderr-misses-fragment": ({"stderr_contains": ["process ${1} failed"]}, {}),
    "stderr-has-fragment": ({"stderr_not_contains": ["Boom"]}, {}),
    "trace-entry-missing": ({"trace": [{**_UNCONSUMED, "problem": "other"}]}, {}),
    "trace-entry-of-other-process": (
        {"trace": [{"pid": "${1}", "kind": "fork", "child_pid": "${2}"}]},
        {},
    ),
    "trace-entry-out-of-order": (
        {
            "trace": [
                {"pid": "${root}", "kind": "exit", "child_pid": "${1}"},
                {"pid": "${root}", "kind": "fork", "child_pid": "${1}"},
            ]
        },
        {},
    ),
    "trace-record-present": ({"trace_absent": [{"kind": "fork", "child_pid": "${1}"}]}, {}),
    "trace-entry-without-pid": ({"trace": [{"kind": "fork"}]}, {}),
    "unknown-process": ({"stderr_contains": ["${3}"]}, {}),
    "wrong-ending": ({}, {"2": {"expect": {"ends": "settle"}}}),
    "stderr-unobserved": ({}, {}),
}


_PROCESS_VARIANTS = {"valid": ({}, {}), **_BROKEN_PROCESS_VARIANTS}
# Two isolated runs, so that neither outgrows the CPU budget.
_PROCESS_VARIANT_HALVES = [
    list(_PROCESS_VARIANTS)[: len(_PROCESS_VARIANTS) // 2],
    list(_PROCESS_VARIANTS)[len(_PROCESS_VARIANTS) // 2 :],
]


def _process_expectations_accepted(workdir: Path, names: list[str]) -> dict[str, bool]:
    """Whether the harness accepts each of the variants *names* (run inside the subprocess)."""
    scenarios = [
        {
            "name": name,
            "forks": {**_PROCESS_FORKS, **_PROCESS_VARIANTS[name][1]},
            "expect": {**_PROCESS_EXPECT, **_PROCESS_VARIANTS[name][0]},
        }
        for name in names
    ]
    program = _write_harness_program(workdir / "process", _PROCESS_HARNESS_PROGRAM, scenarios)
    compilations: CompilationCache = {}
    return {
        scenario["name"]: _harness_accepts(
            program,
            scenario,
            workdir,
            compilations,
            stdout="first\n1\n",
            observe_stderr=scenario["name"] != "stderr-unobserved",
        )
        for scenario in scenarios
    }


@pytest.mark.parametrize("names", _PROCESS_VARIANT_HALVES, ids=["first", "second"])
def test_stderr_trace_and_ending_expectations_are_enforced(
    tmp_path: Path, names: list[str]
) -> None:
    accepted = run_callable(_process_expectations_accepted, tmp_path, {"names": names}).value

    assert accepted == {name: name == "valid" for name in names}


def _write_report(reports: Path, path: str, error: str | None) -> None:
    (reports / f"{path}.json").write_text(json.dumps({"error": error}))


def _write_forked(reports: Path, path: str) -> None:
    (reports / f"{path}.forked").touch()


def test_a_scripted_fork_that_never_happened_fails_without_waiting(tmp_path: Path) -> None:
    from tests.agl.scenario_harness import assert_forks_reported

    # Nothing was forked by the root, which is over when the assertion runs.
    started = time.monotonic()
    with pytest.raises(AssertionError):
        assert_forks_reported({"1": _EMPTY_SECTION}, tmp_path, timeout=600.0)
    assert time.monotonic() - started < 60.0


@pytest.mark.parametrize("forker_state", ["reported", "never_forked"])
def test_a_scripted_fork_whose_forker_cannot_fork_any_more_fails_without_waiting(
    tmp_path: Path, forker_state: str
) -> None:
    from tests.agl.scenario_harness import assert_forks_reported

    # "1.2" was never forked by "1", which reported or was never forked (as "2" was not).
    if forker_state == "reported":
        _write_forked(tmp_path, "1")
        _write_report(tmp_path, "1", None)
        path = "1.2"
    else:
        path = "2.1"
    started = time.monotonic()
    with pytest.raises(AssertionError):
        assert_forks_reported({path: _EMPTY_SECTION}, tmp_path, timeout=600.0)
    assert time.monotonic() - started < 60.0


def test_a_forked_scripted_process_that_has_not_reported_is_waited_for(tmp_path: Path) -> None:
    from tests.agl.scenario_harness import assert_forks_reported

    # "1" is detached and still running, and "1.1" can still be forked by it.
    _write_forked(tmp_path, "1")
    _write_forked(tmp_path, "2")
    _write_report(tmp_path, "2", None)
    _write_forked(tmp_path, "3")

    for sections in ({"1": _EMPTY_SECTION}, {"3": _EMPTY_SECTION}, {"1.1": _EMPTY_SECTION}):
        with pytest.raises(AssertionError, match="never reported"):
            assert_forks_reported(sections, tmp_path, timeout=0.05)


def test_a_scripted_process_that_reported_or_is_still_running_is_not_taken_for_unforked(
    tmp_path: Path,
) -> None:
    from tests.agl.scenario_harness import assert_forks_reported

    # A child may report before its forker records the fork.
    _write_report(tmp_path, "1", None)

    assert_forks_reported({"1": _EMPTY_SECTION}, tmp_path, timeout=0.0)


def test_an_unscripted_fork_that_never_reported_does_not_fail_the_scenario(
    tmp_path: Path,
) -> None:
    from tests.agl.scenario_harness import assert_forks_reported

    # Fork "1" was terminated before it reported; "2" reported and passed.
    _write_report(tmp_path, "2", None)

    assert_forks_reported({}, tmp_path, timeout=0.0)


def test_a_scripted_fork_that_never_reported_fails_the_scenario(tmp_path: Path) -> None:
    from tests.agl.scenario_harness import assert_forks_reported

    _write_forked(tmp_path, "1")
    _write_report(tmp_path, "2", None)

    with pytest.raises(AssertionError):
        assert_forks_reported({"1": _EMPTY_SECTION}, tmp_path, timeout=0.0)


def test_reports_that_exist_are_checked_whether_or_not_the_fork_was_scripted(
    tmp_path: Path,
) -> None:
    from tests.agl.scenario_harness import assert_forks_reported

    _write_report(tmp_path, "1", None)
    _write_report(tmp_path, "2", "unscripted call")

    with pytest.raises(AssertionError):
        assert_forks_reported({"1": _EMPTY_SECTION}, tmp_path, timeout=0.0)


def test_a_zygote_that_fails_to_start_ends_zygote_use_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_zygote: None
) -> None:
    # The zygote process dies on start: the flag makes it a cold run of a missing request.
    monkeypatch.setattr(isolated_runner, "ZYGOTE_FLAG", "--no-such-scratch")
    with pytest.warns(UserWarning, match="FileNotFoundError"):
        assert run_callable(_parent_pid, tmp_path).value == os.getpid()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert run_callable(_parent_pid, tmp_path).value == os.getpid()


def test_killing_a_child_that_has_not_yet_started_its_session_kills_it() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL
    )
    try:
        isolated_runner._kill_child(process.pid)
        assert process.wait(timeout=10) == -signal.SIGKILL
    finally:
        process.kill()
        process.wait()


class _Interrupted(BaseException):
    """What an interrupt of the client looks like."""


def _fail_the_client(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, *, lose_pid: Path | None = None
) -> list[int]:
    """Make the client fail after the zygote forked a child; return the pid it reported.

    By default the failure comes while the client waits for the child to end. With
    *lose_pid* it comes instead of the pid frame, once that file has data (the child ran).
    """
    pids: list[int] = []
    receive = _zygote.Launcher._receive

    def failing(self: _zygote.Launcher, *args: Any, **kwargs: Any) -> Any:
        if pids and lose_pid is None:
            raise error
        reply = receive(self, *args, **kwargs)
        pids.append(reply)
        if lose_pid is not None:
            deadline = time.monotonic() + 30
            while (
                not lose_pid.exists() or lose_pid.stat().st_size == 0
            ) and time.monotonic() < deadline:
                time.sleep(0.01)
            raise error
        return reply

    monkeypatch.setattr(_zygote.Launcher, "_receive", failing)
    return pids


def test_a_run_interrupted_while_its_child_runs_kills_the_child_and_frees_the_zygote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_zygote: None
) -> None:
    zygote_pid = run_callable(_parent_pid, tmp_path).value
    with monkeypatch.context() as patch:
        pids = _fail_the_client(patch, _Interrupted())
        with pytest.raises(_Interrupted):
            run_callable(_sleep_forever, tmp_path)
    assert _wait_until_gone(pids[0])

    assert run_callable(_parent_pid, tmp_path).value == zygote_pid


def test_a_zygote_lost_after_the_request_was_sent_does_not_run_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_zygote: None
) -> None:
    run_callable(_parent_pid, tmp_path)
    ran = tmp_path / "ran"
    with monkeypatch.context() as patch:
        pids = _fail_the_client(patch, _zygote.LauncherError("lost"), lose_pid=ran)
        with pytest.raises(pytest.fail.Exception):
            run_callable(_note_and_sleep, tmp_path)
    assert ran.read_text() == "x\n"
    assert _wait_until_gone(pids[0])


def test_a_request_that_never_reached_the_zygote_runs_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_zygote: None
) -> None:
    run_callable(_parent_pid, tmp_path)
    monkeypatch.setattr(isolated_runner._warm, "_address", "\0no-such-zygote")
    assert run_callable(_parent_pid, tmp_path).value == os.getpid()


def test_a_run_with_another_interpreter_start_environment_gets_a_cold_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_zygote: None
) -> None:
    zygote_pid = run_callable(_parent_pid, tmp_path).value
    assert zygote_pid != os.getpid()
    with monkeypatch.context() as patch:
        patch.setenv("PYTHONHASHSEED", "12345")
        assert run_callable(_parent_pid, tmp_path).value == os.getpid()
    assert run_callable(_parent_pid, tmp_path).value == zygote_pid
