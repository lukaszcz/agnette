"""The process host: forking, outcome transport, waiting, ownership, participants, lifetime.

Forking scenarios run in the isolated runner, since the suite's own processes are
multi-threaded. Children `setpgid` into groups of their own, which the runner's group kill does
not reach, so every scenario's host fixture reaps its children and fails on a leftover one.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import io
import json
import os
import resource
import select
import signal
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import FrameType
from typing import Any, cast
from unittest import mock

import pytest

from agnette.agent.session.protocol import (
    BackendSettings,
    PortableBackend,
    PortableSession,
    SessionAskRequest,
    SessionAskResponse,
    SessionOperations,
)
from agnette.agent.session.rpc import PiRpcSessionBackend
from agnette.agent.session.service import AglSessionHost, SessionService
from agnette.agent.spec import AgentCommand, AgentPi
from agnette.agl.modules.ids import ENTRY_ID
from agnette.agl.runtime.externs import ExternRegistry, ExternRuntimeState
from agnette.agl.runtime.fork_participants import (
    CompanionStateParticipant,
    SessionParticipant,
    StdioFlushParticipant,
)
from agnette.agl.runtime.process_records import parse_record
from agnette.agl.runtime.processes import (
    _READ_CHUNK,
    ChildResult,
    Delivered,
    Disposition,
    Ended,
    Failed,
    ForkParticipant,
    ForkRequest,
    Outcome,
    ProcessHost,
    ProcessHostError,
    ProcessInfo,
    _interrupt_end,
    _leave,
)
from agnette.agl.runtime.request import AgentResponse
from agnette.agl.runtime.sessions import AgentDispatcherSessionHost, SessionHostError
from agnette.core.process import kill_group
from agnette.core.signals import SignalWakeup
from tests._agl_helpers import unavailable_sandbox_context
from tests._proc_helpers import open_fd_count, read_until_signalled
from tests._signal_helpers import before_each_poll, trip_signal_off_thread
from tests.agl.ir_harness import write_companion_file
from tests.agl.isolated_runner import run_callable
from tests.test_agent_rpc import RpcStub, option_value

RUN_TIMEOUT = 30.0
_REPEATS = 20


# ---------------------------------------------------------------------------
# Scenario helpers (these run inside the isolated subprocess)
# ---------------------------------------------------------------------------


class _TrackedHost(ProcessHost):
    """A host that remembers its attached children, for the fixture's cleanup."""

    def __init__(self, grace: float, reporter: Callable[[ProcessInfo, str], None] | None) -> None:
        super().__init__(grace=grace, reporter=reporter)
        self.attached: list[ProcessInfo] = []

    def fork(self, child: Callable[[], ChildResult], sessions: Sequence[str]) -> ProcessInfo:
        info = super().fork(child, sessions)
        self.attached.append(info)
        return info

    def detach(self, id: str) -> None:
        super().detach(id)
        self.attached = [info for info in self.attached if info.id != id]


def _is_reaped(pid: int) -> bool:
    """Whether *pid* is no longer an unreaped child of this process."""
    try:
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    return False


@contextlib.contextmanager
def _host(
    *participants: ForkParticipant[Any],
    grace: float = 30.0,
    reporter: Callable[[ProcessInfo, str], None] | None = None,
) -> Iterator[ProcessHost]:
    """A host that reaps its children on exit and fails if any child is left behind.

    *grace* is its termination grace period; tests that wait it out pass a short one.
    """
    host = _TrackedHost(grace, reporter)
    for participant in participants:
        host.register(participant)
    try:
        yield host
    finally:
        for info in host.attached:
            if not _is_reaped(info.pid):
                os.kill(info.pid, signal.SIGKILL)
                host.settle(info.id)
    with pytest.raises(ChildProcessError):
        os.waitpid(-1, os.WNOHANG)


@contextlib.contextmanager
def _recorded_forks() -> Iterator[list[int]]:
    """The pids that ``os.fork`` produces in this process while active."""
    real = os.fork
    pids: list[int] = []

    def fork() -> int:
        pid = real()
        if pid:
            pids.append(pid)
        return pid

    with mock.patch.object(os, "fork", fork):
        yield pids


class _Gate:
    """A pipe a child blocks on until the parent opens it."""

    def __init__(self) -> None:
        self.read_fd, self.write_fd = os.pipe()

    def pass_through(self) -> None:
        """Block in a child until the gate opens, or the parent is gone.

        The child drops its copy of the write end, so the parent's death ends the wait.
        """
        os.close(self.write_fd)
        read_until_signalled(self.read_fd, 1)

    def wait_then(self, payload: bytes) -> Callable[[], bytes]:
        def child() -> bytes:
            self.pass_through()
            return payload

        return child

    def open(self) -> None:
        os.write(self.write_fd, b"x")

    def wait(self) -> None:
        """Block in the parent until a child (or the parent) has opened the gate."""
        os.read(self.read_fd, 1)


def _show(outcome: Outcome) -> list[Any]:
    match outcome:
        case Delivered(payload, None):
            return ["D", payload.decode()]
        case Delivered(payload, failure):
            return ["DF", payload.decode(), failure]
        case Failed(message):
            return ["F", message]
        case Ended(exit_code, signal_number):
            return ["E", exit_code, signal_number]


def _kill_self(number: signal.Signals) -> bytes:
    os.kill(os.getpid(), number)
    return b"unreachable"


def _raise_value_error() -> bytes:
    raise ValueError("boom")


def _raise_generator_exit() -> bytes:
    raise GeneratorExit("stop")


def _survive_interrupt(number: signal.Signals) -> Callable[[], bytes]:
    def child() -> bytes:
        try:
            os.kill(os.getpid(), number)
        except KeyboardInterrupt:
            return b"interrupted"
        return b"not interrupted"

    return child


def _describe_child() -> bytes:
    return json.dumps(
        {
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "stdin": os.read(0, 16).decode(),
            "sigterm_raises": _raises_keyboard_interrupt(signal.SIGTERM),
        }
    ).encode()


def _raises_keyboard_interrupt(number: signal.Signals) -> bool:
    try:
        os.kill(os.getpid(), number)
    except KeyboardInterrupt:
        return True
    return False


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def _outcome_kinds(workdir: Path) -> dict[str, Any]:
    with _host() as host:
        children = {
            "delivered": host.fork(lambda: b"payload", ()),
            "failed": host.fork(_raise_value_error, ()),
            "exit": host.fork(lambda: sys.exit(3), ()),
            "exit none": host.fork(sys.exit, ()),
            "exit text": host.fork(lambda: sys.exit("gone"), ()),
            "hard exit": host.fork(lambda: os._exit(4), ()),
            "kill": host.fork(lambda: _kill_self(signal.SIGKILL), ()),
            "term": host.fork(lambda: _kill_self(signal.SIGTERM), ()),
            "hup": host.fork(lambda: _kill_self(signal.SIGHUP), ()),
            "interrupt": host.fork(lambda: _kill_self(signal.SIGINT), ()),
            "survived term": host.fork(_survive_interrupt(signal.SIGTERM), ()),
            "survived hup": host.fork(_survive_interrupt(signal.SIGHUP), ()),
        }
        outcomes = {name: _show(host.outcome(info.id)) for name, info in children.items()}
        again = {name: _show(host.outcome(info.id)) for name, info in children.items()}
        assert again == outcomes
    return outcomes


def test_outcomes_are_classified_as_delivered_failed_or_ended(tmp_path: Path) -> None:
    outcomes = run_callable(_outcome_kinds, tmp_path, timeout=RUN_TIMEOUT).value

    assert outcomes["delivered"] == ["D", "payload"]
    failed = outcomes["failed"]
    assert failed[0] == "F"
    assert "ValueError" in failed[1] and "boom" in failed[1]
    assert outcomes["exit"] == ["E", 3, None]
    assert outcomes["exit none"] == ["E", 0, None]
    assert outcomes["exit text"] == ["E", 1, None]
    assert outcomes["hard exit"] == ["E", 4, None]
    assert outcomes["kill"] == ["E", None, signal.SIGKILL]
    assert outcomes["term"] == ["E", None, signal.SIGTERM]
    assert outcomes["hup"] == ["E", None, signal.SIGHUP]
    assert outcomes["interrupt"] == ["E", None, signal.SIGINT]
    assert outcomes["survived term"] == ["D", "interrupted"]
    assert outcomes["survived hup"] == ["D", "interrupted"]


def _child_environment(workdir: Path) -> dict[str, Any]:
    reader, writer = os.pipe()
    os.write(writer, b"typed")
    os.dup2(reader, 0)
    inherited_stdin = sys.stdin
    sys.stdin = None
    sys.stdout.write("before-")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            with _host(StdioFlushParticipant()) as host:
                info = host.fork(_describe_child, ())
                outcome = host.outcome(info.id)
        finally:
            sys.stdin = inherited_stdin
    assert isinstance(outcome, Delivered)
    return {
        "info": [info.pid, info.id != ""],
        "described": json.loads(outcome.payload),
        "parent": os.getpid(),
        "warnings": [str(w.message) for w in caught],
    }


def test_a_child_is_a_detached_group_leader_with_null_stdin_and_flushed_stdio(
    tmp_path: Path,
) -> None:
    result = run_callable(_child_environment, tmp_path, timeout=RUN_TIMEOUT)

    described = result.value["described"]
    assert described["pid"] == result.value["info"][0] != result.value["parent"]
    assert described["pgid"] == described["pid"]
    assert described["stdin"] == ""
    assert described["sigterm_raises"]
    assert result.value["warnings"] == []
    assert result.stdout == "before-"


def _child_python_stdin(workdir: Path) -> dict[str, str]:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"first\nsecond\n")
    os.close(write_fd)
    original_stdin = sys.stdin
    saved_stdin_fd = os.dup(0)
    os.dup2(read_fd, 0)
    os.close(read_fd)
    sys.stdin = os.fdopen(0, "r")
    first = sys.stdin.readline()

    def child() -> bytes:
        return json.dumps(
            {"python": sys.stdin.readline(), "descriptor": os.read(0, 16).decode()}
        ).encode()

    with _host() as host:
        outcome = host.outcome(host.fork(child, ()).id)
    assert isinstance(outcome, Delivered)
    second = sys.stdin.readline()
    sys.stdin.close()
    os.dup2(saved_stdin_fd, 0)
    os.close(saved_stdin_fd)
    sys.stdin = original_stdin
    return {"first": first, "second": second, **json.loads(outcome.payload)}


def test_a_child_does_not_read_prefetched_python_stdin(tmp_path: Path) -> None:
    result = run_callable(_child_python_stdin, tmp_path, timeout=RUN_TIMEOUT).value

    assert result == {"first": "first\n", "second": "second\n", "python": "", "descriptor": ""}


def _child_default_session_environment(workdir: Path) -> str:
    from agnette.agl import PipelineDriver

    environment = {"PATH": "/child/path", "TOKEN": "credential"}

    def dispatch(request: Any) -> AgentResponse:
        return AgentResponse(json.dumps(request.env, sort_keys=True), {})

    runtime = PipelineDriver(
        session_host=AgentDispatcherSessionHost(dispatch),
        get_sandbox_context=None,
        resolve_agent_spec=None,
    )
    source = (
        "import std/config\n"
        "import std/ps\n"
        "program def main() -> unit =\n"
        '  std/config::agent := AgentCommand("worker")\n'
        '  let parent: text = ask("parent")\n'
        '  print ps::wait(ps::fork(fn() => ask("child")))\n'
        "  ()\n"
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = runtime.run(source, process_environment=environment)
    assert result.ok, (result.diagnostics, result.error)
    return output.getvalue().strip()


def test_a_child_reopens_the_default_session_with_the_current_environment(
    tmp_path: Path,
) -> None:
    environment = run_callable(
        _child_default_session_environment, tmp_path, timeout=RUN_TIMEOUT
    ).value

    assert json.loads(environment) == {"PATH": "/child/path", "TOKEN": "credential"}


def _child_stdout_is_flushed(workdir: Path) -> None:
    def child() -> bytes:
        sys.stdout.write("child-tail")
        return b""

    sys.stdout.write("before-")
    with _host(StdioFlushParticipant()) as host:
        host.outcome(host.fork(child, ()).id)


def test_stdio_is_flushed_before_the_fork_and_at_child_exit(tmp_path: Path) -> None:
    result = run_callable(_child_stdout_is_flushed, tmp_path, timeout=RUN_TIMEOUT)

    assert result.stdout == "before-child-tail"


# ---------------------------------------------------------------------------
# Thread check and fork refusal
# ---------------------------------------------------------------------------


class _Recorder(ForkParticipant[str]):
    """Records every hook; child-side hooks append to the shared log file."""

    def __init__(
        self,
        name: str,
        log: Path,
        *,
        fail_prepare: bool = False,
        fail_child: bool = False,
        fail_child_exit: bool = False,
        exit_hook: BaseException | signal.Signals | None = None,
    ) -> None:
        self.name = name
        self.log = log
        self.parent_pid = os.getpid()
        self.fail_prepare = fail_prepare
        self.fail_child = fail_child
        self.fail_child_exit = fail_child_exit
        self.exit_hook = exit_hook

    def _record(self, event: str) -> None:
        side = "P" if os.getpid() == self.parent_pid else "C"
        with self.log.open("a") as handle:
            handle.write(f"{side} {event}:{self.name}\n")

    def prepare(self, request: ForkRequest) -> str:
        self._record("prepare " + ",".join(request.sessions))
        if self.fail_prepare:
            raise RuntimeError("prepare failed")
        return self.name

    def abort(self, state: str) -> None:
        self._record(f"abort {state}")

    def parent(self, state: str) -> None:
        self._record(f"parent {state}")

    def child(self, state: str) -> None:
        self._record(f"child {state}")
        if self.fail_child:
            raise RuntimeError("child hook failed")

    def child_exit(self, disposition: Disposition) -> None:
        self._record(f"exit {disposition.name}")
        if self.fail_child_exit:
            raise RuntimeError("child_exit failed")
        if isinstance(self.exit_hook, BaseException):
            raise self.exit_hook
        if self.exit_hook is not None:
            os.kill(os.getpid(), self.exit_hook)


def _lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def _thread_check(workdir: Path) -> dict[str, Any]:
    log = workdir / "log"
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    thread.start()
    with _host(_Recorder("a", log)) as host:
        try:
            host.fork(lambda: b"x", ())
        except ProcessHostError:
            refused = True
        else:
            refused = False
        release.set()
        thread.join()
        accepted = _show(host.outcome(host.fork(lambda: b"x", ()).id))
    return {"refused": refused, "accepted": accepted, "log": _lines(log)}


def test_fork_is_refused_while_another_python_thread_exists(tmp_path: Path) -> None:
    result = run_callable(_thread_check, tmp_path, timeout=RUN_TIMEOUT).value

    assert result["refused"]
    assert result["accepted"] == ["D", "x"]
    # The refused attempt prepared nothing.
    assert [line for line in result["log"] if line.startswith("P prepare")] == ["P prepare :a"]


def _fork_failure(workdir: Path) -> dict[str, Any]:
    log = workdir / "log"
    before = open_fd_count()
    with _host(_Recorder("a", log), _Recorder("b", log)) as host:
        with mock.patch.object(os, "fork", side_effect=OSError("no processes")):
            try:
                host.fork(lambda: b"x", ())
            except ProcessHostError:
                refused = True
            else:
                refused = False
    return {"refused": refused, "log": _lines(log), "leaked_fds": open_fd_count() - before}


def test_a_refused_os_fork_aborts_the_prepared_states_and_leaks_no_descriptor(
    tmp_path: Path,
) -> None:
    result = run_callable(_fork_failure, tmp_path, timeout=RUN_TIMEOUT).value

    assert result["refused"]
    assert result["log"] == ["P prepare :a", "P prepare :b", "P abort b:b", "P abort a:a"]
    assert result["leaked_fds"] == 0


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------


def _participant_hooks(workdir: Path) -> list[str]:
    log = workdir / "log"
    with _host(_Recorder("a", log), _Recorder("b", log)) as host:
        host.outcome(host.fork(lambda: b"ok", ("s1", "s2")).id)
        host.outcome(host.fork(_raise_value_error, ()).id)
        host.outcome(host.fork(lambda: sys.exit(2), ()).id)
    return _lines(log)


def test_participants_run_in_order_with_the_disposition_of_the_child_outcome(
    tmp_path: Path,
) -> None:
    lines = run_callable(_participant_hooks, tmp_path, timeout=RUN_TIMEOUT).value

    parent = [line[2:] for line in lines if line.startswith("P ")]
    assert parent == [
        "prepare s1,s2:a",
        "prepare s1,s2:b",
        "parent a:a",
        "parent b:b",
        "prepare :a",
        "prepare :b",
        "parent a:a",
        "parent b:b",
        "prepare :a",
        "prepare :b",
        "parent a:a",
        "parent b:b",
    ]
    child = [line[2:] for line in lines if line.startswith("C ")]
    assert child == [
        "child a:a",
        "child b:b",
        "exit SETTLE:b",
        "exit SETTLE:a",
        "child a:a",
        "child b:b",
        "exit TERMINATE:b",
        "exit TERMINATE:a",
        "child a:a",
        "child b:b",
        "exit TERMINATE:b",
        "exit TERMINATE:a",
    ]


def _prepare_failure(workdir: Path) -> list[str]:
    log = workdir / "log"
    with _host(
        _Recorder("a", log), _Recorder("b", log, fail_prepare=True), _Recorder("c", log)
    ) as host:
        try:
            host.fork(lambda: b"x", ())
        except RuntimeError as error:
            assert str(error) == "prepare failed"
    return _lines(log)


def test_a_failing_prepare_aborts_only_the_earlier_states_and_forks_nothing(
    tmp_path: Path,
) -> None:
    lines = run_callable(_prepare_failure, tmp_path, timeout=RUN_TIMEOUT).value

    assert lines == ["P prepare :a", "P prepare :b", "P abort a:a"]


def _failing_child_hooks(workdir: Path) -> dict[str, Any]:
    outcomes: dict[str, Any] = {}
    for name, flags in {
        "child hook": {"fail_child": True},
        "child_exit": {"fail_child_exit": True},
    }.items():
        marker = workdir / f"{name}.marker"
        with _host(_Recorder("a", workdir / f"{name}.log", **flags)) as host:
            info = host.fork(lambda: b"ok", ())
            with marker.open("a") as handle:
                handle.write("x")
            outcomes[name] = [_show(host.outcome(info.id)), marker.read_text()]
    return outcomes


def test_failing_child_hooks_never_return_into_the_callers_stack_and_fail_the_child(
    tmp_path: Path,
) -> None:
    outcomes = run_callable(_failing_child_hooks, tmp_path, timeout=RUN_TIMEOUT).value

    outcome, marker = outcomes["child hook"]
    assert outcome[0] == "E"
    assert outcome[1] not in (None, 0)
    assert outcome[2] is None
    # Only the parent continues after `fork` returns.
    assert marker == "x"
    # A failing `child_exit` after a delivered result becomes the failure.
    outcome, marker = outcomes["child_exit"]
    assert marker == "x"
    assert outcome[0] == "F"
    assert "child_exit failed" in outcome[1]


def _teardown_hooks(workdir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    log = workdir / "log"
    with _host(
        _Recorder("a", log),
        _Recorder("b", log, fail_child=True),
        _Recorder("c", log),
    ) as host:
        result["hook failure"] = _show(host.outcome(host.fork(lambda: b"ok", ()).id))
    result["log"] = [line[2:] for line in _lines(log) if line.startswith("C ")]
    with _host(_Recorder("a", workdir / "failed.log", fail_child_exit=True)) as host:
        result["failed child"] = _show(host.outcome(host.fork(_raise_value_error, ()).id))
    return result


def test_child_teardown_exits_the_entered_participants_and_keeps_the_first_failure(
    tmp_path: Path,
) -> None:
    result = run_callable(_teardown_hooks, tmp_path, timeout=RUN_TIMEOUT).value

    # Only `a` completed its entry: `b` failed it and `c` never began.
    assert result["log"] == ["child a:a", "child b:b", "exit TERMINATE:a"]
    assert result["hook failure"][0] == "E"
    assert result["hook failure"][1] not in (None, 0)
    # A failing `child_exit` keeps the error of an already failed callable.
    assert result["failed child"][0] == "F"
    assert "boom" in result["failed child"][1]


def _base_exception_teardown(workdir: Path) -> dict[str, Any]:
    log = workdir / "log"
    with _host(_Recorder("a", log)) as host:
        outcome = _show(host.outcome(host.fork(_raise_generator_exit, ()).id))
    return {
        "outcome": outcome,
        "child": [line[2:] for line in _lines(log) if line.startswith("C ")],
    }


def test_child_exit_hooks_run_when_the_callable_raises_generator_exit(tmp_path: Path) -> None:
    result = run_callable(_base_exception_teardown, tmp_path, timeout=RUN_TIMEOUT).value

    assert result == {
        "outcome": ["E", 70, None],
        "child": ["child a:a", "exit TERMINATE:a"],
    }


class _UnprintableText:
    def __str__(self) -> str:
        raise ZeroDivisionError


class _UnformattableError(SyntaxError):
    """An exception whose standard formatting raises."""

    def __init__(self) -> None:
        super().__init__("syntax")
        self.msg = cast(str, _UnprintableText())


def _raise_unformattable() -> bytes:
    raise _UnformattableError


def _failure_messages(workdir: Path) -> dict[str, str]:
    with _host() as host:
        raised = {
            "framed": host.fork(_raise_value_error, ()),
            "unformattable": host.fork(_raise_unformattable, ()),
        }
        messages = {}
        for name, info in raised.items():
            outcome = host.outcome(info.id)
            assert isinstance(outcome, Failed)
            messages[name] = outcome.message
    return messages


def test_a_failure_message_names_the_exception_type_even_if_formatting_raises(
    tmp_path: Path,
) -> None:
    messages = run_callable(_failure_messages, tmp_path, timeout=RUN_TIMEOUT).value

    assert messages["framed"].startswith("ValueError")
    assert "boom" in messages["framed"]
    assert "UnformattableError" in messages["unformattable"]


# ---------------------------------------------------------------------------
# Companion state across a real fork
# ---------------------------------------------------------------------------


class _FailingPrepare(ForkParticipant[None]):
    def prepare(self, request: ForkRequest) -> None:
        raise RuntimeError("later participant failed")


def _companion_state(workdir: Path) -> dict[str, Any]:
    log = workdir / "closed"

    def closer(label: str) -> Callable[[str], None]:
        def close(value: str) -> None:
            with log.open("a") as handle:
                handle.write(f"{'P' if os.getpid() == parent else 'C'} {label}:{value}\n")

        return close

    parent = os.getpid()
    state = ExternRuntimeState()
    debug = [False]
    state.get_or_create(
        "forked", lambda: "p", closer("forked"), fork=lambda value: f"{value}>child"
    )
    state.get_or_create("plain", lambda: "plain", closer("plain"))
    state.get_or_create(
        "kept", lambda: "k", closer("kept"), keep_in_debug=True, fork=lambda value: f"{value}>c"
    )

    def child() -> bytes:
        seen = [
            state.get_or_create("forked", lambda: "fresh"),
            state.get_or_create("plain", lambda: "fresh-plain", closer("plain")),
            state.get_or_create("kept", lambda: "fresh"),
        ]
        return json.dumps(seen).encode()

    seen: list[Any] = []
    with _host(CompanionStateParticipant(state, lambda: debug[0])) as host:
        seen.append(json.loads(_delivered(host.outcome(host.fork(child, ()).id))))
        debug[0] = True
        seen.append(json.loads(_delivered(host.outcome(host.fork(child, ()).id))))
        debug[0] = False
        parent_view = [
            state.get_or_create("forked", lambda: "fresh"),
            state.get_or_create("plain", lambda: "fresh"),
        ]
    closed_by_children = _lines(log)
    with _host(CompanionStateParticipant(state, lambda: False), _FailingPrepare()) as host:
        try:
            host.fork(child, ())
        except RuntimeError:
            pass
    return {
        "seen": seen,
        "parent_view": parent_view,
        "closed_by_children": closed_by_children,
        "closed_after_abort": _lines(log)[len(closed_by_children) :],
    }


def _delivered(outcome: Outcome) -> bytes:
    assert isinstance(outcome, Delivered)
    return outcome.payload


def test_the_companion_state_participant_gives_a_child_only_its_hook_made_entries(
    tmp_path: Path,
) -> None:
    result = run_callable(_companion_state, tmp_path, timeout=RUN_TIMEOUT).value

    assert result["seen"] == [["p>child", "fresh-plain", "k>c"]] * 2
    # The parent's own entries are untouched by its children.
    assert result["parent_view"] == ["p", "plain"]
    # A child closes its own entries at exit, none inherited; debug keeps the exempt one.
    child_lines = [line[2:] for line in result["closed_by_children"] if line.startswith("C ")]
    assert child_lines == [
        "plain:fresh-plain",
        "kept:k>c",
        "forked:p>child",
        "plain:fresh-plain",
        "forked:p>child",
    ]
    assert not [line for line in result["closed_by_children"] if line.startswith("P ")]
    # A fork aborted by a later participant closes the produced values in the parent.
    assert result["closed_after_abort"] == ["P kept:k>c", "P forked:p>child"]


def _forked_sessions(workdir: Path) -> dict[str, Any]:
    sessions = AgentDispatcherSessionHost(lambda _request: AgentResponse("answer", {}))
    agent = AgentCommand(command="worker")
    inherited = sessions.default(agent, "Cli", env={})

    def child() -> bytes:
        try:
            sessions.ask(inherited, "question")
            rejected = False
        except SessionHostError:
            rejected = True
        fresh = sessions.default(agent, "Cli", env={})
        reopened = fresh != inherited and sessions.ask(fresh, "question") == "answer"
        return json.dumps([rejected, reopened]).encode()

    with _host(SessionParticipant(sessions)) as host:
        seen = json.loads(_delivered(host.outcome(host.fork(child, ()).id)))
    return {"child": seen, "parent": sessions.ask(inherited, "question")}


def test_the_session_participant_makes_a_childs_inherited_sessions_inert(tmp_path: Path) -> None:
    result = run_callable(_forked_sessions, tmp_path, timeout=RUN_TIMEOUT).value

    assert result == {"child": [True, True], "parent": "answer"}


class _LoggedBackend:
    """A session backend that logs, with its process, when it is released or closed."""

    continues_conversation = True
    operations = SessionOperations()

    def __init__(self, log: Path, label: str) -> None:
        self._log = log
        self._label = label

    def ask(self, request: SessionAskRequest) -> SessionAskResponse:
        return SessionAskResponse(content="answer")

    def reset(self) -> None:
        pass

    def close(self) -> None:
        self._write("close")

    def portable(self) -> PortableSession:
        return PortableBackend(self)

    def release_inherited(self) -> None:
        self._write("release")

    def _write(self, event: str) -> None:
        with self._log.open("a") as handle:
            handle.write(f"{os.getpid()} {self._label} {event}\n")


def _session_exit(workdir: Path) -> dict[str, Any]:
    log = workdir / "log"
    labels = iter(("parent", "child"))
    sessions = AglSessionHost(SessionService(lambda _request: _LoggedBackend(log, next(labels))))
    agent = AgentCommand(command="worker")
    inherited = sessions.open(agent, "Cli", env={})
    parent = os.getpid()

    def child() -> bytes:
        sessions.ask(sessions.open(agent, "Cli", env={}), "question")
        return b""

    with _host(SessionParticipant(sessions)) as host:
        host.outcome(host.fork(child, ()).id)
    after = sessions.ask(inherited, "question")
    events = [line.split() for line in _lines(log)]
    return {
        "after": after,
        "parent_events": [event[1:] for event in events if int(event[0]) == parent],
        "child_events": [event[1:] for event in events if int(event[0]) != parent],
    }


def test_a_child_closes_the_sessions_it_opened_and_leaves_its_inherited_ones(
    tmp_path: Path,
) -> None:
    result = run_callable(_session_exit, tmp_path, timeout=RUN_TIMEOUT).value

    assert result["child_events"] == [["parent", "release"], ["child", "close"]]
    assert result["parent_events"] == []
    assert result["after"] == "answer"


class _Conversation:
    """A session backend whose answer is its whole conversation, so a snapshot is visible."""

    continues_conversation = True
    operations = SessionOperations()

    def __init__(self, history: Sequence[str] = ()) -> None:
        self._history = list(history)

    def ask(self, request: SessionAskRequest) -> SessionAskResponse:
        self._history.append(request.prompt)
        return SessionAskResponse(content=" ".join(self._history))

    def reset(self) -> None:
        self._history.clear()

    def portable(self) -> PortableSession:
        return PortableBackend(_Conversation(self._history))

    def close(self) -> None:
        pass

    def release_inherited(self) -> None:
        pass


def _snapshotted_sessions(workdir: Path) -> dict[str, Any]:
    sessions = AglSessionHost(SessionService(lambda _request: _Conversation()))
    agent = AgentCommand(command="worker")
    listed = sessions.open(agent, "Cli", env={})
    unlisted = sessions.open(agent, "Cli", env={})
    default = sessions.default(agent, "Cli", env={})
    for handle in (listed, unlisted, default):
        sessions.ask(handle, handle[:2])

    def child() -> bytes:
        try:
            sessions.ask(unlisted, "child")
            rejected = False
        except SessionHostError:
            rejected = True
        answers = [
            sessions.ask(listed, "child"),
            sessions.ask(sessions.default(agent, "Cli", env={}), "child"),
        ]
        return json.dumps([rejected, answers]).encode()

    results = {}
    with _host(SessionParticipant(sessions)) as host:
        for name, listing in (("with-default", (listed, default)), ("fresh-default", (listed,))):
            results[name] = json.loads(_delivered(host.outcome(host.fork(child, listing).id)))
    results["parent"] = [sessions.ask(handle, "parent") for handle in (listed, default)]
    results["prefixes"] = [handle[:2] for handle in (listed, unlisted, default)]
    return results


def test_a_forked_child_continues_the_listed_sessions_from_their_snapshots(
    tmp_path: Path,
) -> None:
    result = run_callable(_snapshotted_sessions, tmp_path, timeout=RUN_TIMEOUT).value

    listed, _unlisted, default = result["prefixes"]
    # The default session is the snapshot when listed, else a fresh one; an unlisted
    # inherited session stays inert.
    assert result["with-default"] == [True, [f"{listed} child", f"{default} child"]]
    assert result["fresh-default"] == [True, [f"{listed} child", "child"]]
    # Neither child's conversation reached the parent's.
    assert result["parent"] == [f"{listed} parent", f"{default} parent"]


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _pi_snapshotted_into_a_child(workdir: Path, stub_root: str) -> dict[str, Any]:
    os.environ["PI_RPC_STUB_ROOT"] = stub_root
    os.environ["PATH"] = f"{stub_root}{os.pathsep}{os.environ['PATH']}"
    sessions = AglSessionHost(
        SessionService(
            lambda request: PiRpcSessionBackend.open(
                request.agent,
                BackendSettings(get_sandbox_context=unavailable_sandbox_context, env=request.env),
            )
        )
    )
    listed = sessions.open(AgentPi("provider", "model", "high"), "Rpc", env=dict(os.environ))
    sessions.ask(listed, "before")
    with _host(SessionParticipant(sessions)) as host:
        outcome = host.outcome(
            host.fork(lambda: sessions.ask(listed, "child").encode(), (listed,)).id
        )
        starts = [
            json.loads(line) for line in (Path(stub_root) / "starts.jsonl").read_text().splitlines()
        ]
        result = {
            "child": _delivered(outcome).decode(),
            "argv": [start["argv"] for start in starts],
            "alive": [_is_alive(start["pid"]) for start in starts],
            "parent": sessions.ask(listed, "parent"),
        }
        sessions.close_all()
    return result


def test_a_forked_child_spawns_and_reaps_its_own_pi_from_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = RpcStub(tmp_path, monkeypatch)

    result = run_callable(
        _pi_snapshotted_into_a_child, tmp_path, {"stub_root": str(stub.root)}, timeout=RUN_TIMEOUT
    ).value

    assert result["child"] == "answer"
    assert result["parent"] == "answer"
    # The parent's Pi, a temporary clone process, and the child's own Pi resuming the clone.
    assert len(result["argv"]) == 3
    assert option_value(result["argv"][2], "--session-id") == "child"
    # The child closed its own Pi and left the parent's, which it inherited, running.
    assert result["alive"] == [True, False, False]


def _refused_session_forks(workdir: Path) -> list[bool]:
    refusals = []
    for sessions in (
        AgentDispatcherSessionHost(None),
        AglSessionHost(SessionService(lambda _request: _Conversation())),
    ):
        refused = False
        with _host(SessionParticipant(sessions)) as host:
            try:
                host.fork(lambda: b"", ("handle",))
            except SessionHostError:
                refused = True
            assert not host.attached
        refusals.append(refused)
    return refusals


def test_forking_with_unavailable_sessions_is_refused_without_leaving_a_child(
    tmp_path: Path,
) -> None:
    assert run_callable(_refused_session_forks, tmp_path, timeout=RUN_TIMEOUT).value == [
        True,
        True,
    ]


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------


def _blocked_on_a_sibling_result(workdir: Path) -> dict[str, bool]:
    """`waiting` can finish only after `big` exits, which needs its result drained."""
    size = 3 * 1024 * 1024
    big_payload = bytes(range(256)) * (size // 256)
    waiting_payload = bytes(reversed(range(256))) * (size // 256)
    alive_read, alive_write = os.pipe()
    with _host() as host:
        big = host.fork(lambda: big_payload, ())
        # `big` was forked while the test still held `alive_write`: it inherited it.
        os.close(alive_write)

        def waiting() -> bytes:
            os.read(alive_read, 1)
            return waiting_payload

        second = host.fork(waiting, ())
        os.close(alive_read)
        host.settle(second.id)
        intact = {
            "waiting": _delivered(host.outcome(second.id)) == waiting_payload,
            "big": _delivered(host.outcome(big.id)) == big_payload,
        }
    return intact


def test_a_blocking_wait_drains_the_result_pipes_of_all_attached_children(
    tmp_path: Path,
) -> None:
    intact = run_callable(_blocked_on_a_sibling_result, tmp_path, timeout=RUN_TIMEOUT).value

    assert intact == {"waiting": True, "big": True}


def _settle_any(workdir: Path) -> dict[str, Any]:
    gate = _Gate()
    with _host() as host:
        slow = host.fork(gate.wait_then(b"slow"), ())
        fast = host.fork(lambda: b"fast", ())
        first = host.settle_any([slow.id, fast.id])
        gate.open()
        host.settle(slow.id)
        result: dict[str, Any] = {
            "first": first,
            "both": host.settle_any([slow.id, fast.id]),
            "reversed": host.settle_any([fast.id, slow.id]),
            "fast": _show(host.outcome(fast.id)),
            "slow": _show(host.outcome(slow.id)),
        }
        try:
            host.settle_any([])
        except ProcessHostError:
            result["empty"] = "error"
    return result


def test_settle_any_returns_the_lowest_finished_index_without_consuming_outcomes(
    tmp_path: Path,
) -> None:
    result = run_callable(_settle_any, tmp_path, timeout=RUN_TIMEOUT).value

    assert result == {
        "first": 1,
        "both": 0,
        "reversed": 0,
        "fast": ["D", "fast"],
        "slow": ["D", "slow"],
        "empty": "error",
    }


def _many_descriptors(workdir: Path) -> list[Any]:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 4096), hard))
    null = os.open(os.devnull, os.O_RDONLY)
    held = [fcntl.fcntl(null, fcntl.F_DUPFD, 3) for _ in range(1100)]
    try:
        probe = os.pipe()
        assert min(probe) >= 1024
        os.close(probe[0])
        os.close(probe[1])
        with _host() as host:
            infos = [host.fork(lambda n=n: str(n).encode(), ()) for n in range(3)]
            return [_show(host.outcome(info.id)) for info in infos]
    finally:
        for fd in held:
            os.close(fd)
        os.close(null)


def test_waiting_works_with_descriptors_above_the_select_limit(tmp_path: Path) -> None:
    outcomes = run_callable(_many_descriptors, tmp_path, timeout=RUN_TIMEOUT).value

    assert outcomes == [["D", "0"], ["D", "1"], ["D", "2"]]


# ---------------------------------------------------------------------------
# Ownership and detach
# ---------------------------------------------------------------------------


def _refused(operation: Callable[[], object]) -> bool:
    try:
        operation()
    except ProcessHostError:
        return True
    return False


def _detach(workdir: Path) -> dict[str, Any]:
    gate, other_gate = _Gate(), _Gate()
    with _host() as host:
        finished = host.fork(lambda: b"x", ())
        host.settle(finished.id)
        host.detach(finished.id)
        misuse = {
            "settle": _refused(lambda: host.settle(finished.id)),
            "outcome": _refused(lambda: host.outcome(finished.id)),
            "detach": _refused(lambda: host.detach(finished.id)),
            "settle_any": _refused(lambda: host.settle_any([finished.id])),
            "unknown": _refused(lambda: host.settle("no-such-process")),
            "terminate": _refused(lambda: host.terminate(finished.id)),
        }
        # A running child detached before it writes: its closed pipe is not a failure.
        running = host.fork(gate.wait_then(b"late"), ())
        host.detach(running.id)
        _refused(lambda: host.settle(running.id))  # a host operation: the child still runs
        gate.open()
        _, status = os.waitpid(running.pid, 0)
        # Detached pids are reaped by later host operations, also when already gone.
        exited = host.fork(other_gate.wait_then(b"x"), ())
        host.detach(exited.id)
        other_gate.open()
        os.waitid(os.P_PID, exited.pid, os.WEXITED | os.WNOWAIT)
        _refused(lambda: host.settle(exited.id))
        try:
            os.waitpid(exited.pid, os.WNOHANG)
        except ChildProcessError:
            reaped = True
        else:
            reaped = False
    return {"misuse": misuse, "detached_status": status, "reaped": reaped}


def test_detached_and_unknown_ids_are_refused_and_detached_children_are_reaped(
    tmp_path: Path,
) -> None:
    result = run_callable(_detach, tmp_path, timeout=RUN_TIMEOUT).value

    assert all(result["misuse"].values())
    assert result["detached_status"] == 0
    assert result["reaped"]


def _nested(workdir: Path) -> dict[str, Any]:
    sibling_gate, grandchild_gate = _Gate(), _Gate()
    with _host() as host:
        sibling = host.fork(sibling_gate.wait_then(b"sibling"), ())
        # Held by the child and its grandchild only: EOF means both are gone.
        alive_read, alive_write = os.pipe()

        def middle() -> bytes:
            foreign = _refused(lambda: host.settle(sibling.id))
            awaited = host.fork(lambda: b"awaited", ())
            result = _show(host.outcome(awaited.id))
            lingering = host.fork(grandchild_gate.wait_then(b"lingering"), ())
            host.detach(lingering.id)
            return json.dumps([foreign, result]).encode()

        child = host.fork(middle, ())
        os.close(alive_write)
        # The grandchild outlives the child; the child's result still reaches EOF.
        outcome = _show(host.outcome(child.id))
        grandchild_gate.open()
        assert os.read(alive_read, 1) == b""
        sibling_gate.open()
        sibling_outcome = _show(host.outcome(sibling.id))
    os.close(alive_read)
    return {"outcome": outcome, "sibling": sibling_outcome}


def test_a_child_has_its_own_process_table_and_its_grandchildren_do_not_hold_its_pipe(
    tmp_path: Path,
) -> None:
    result = run_callable(_nested, tmp_path, timeout=RUN_TIMEOUT).value

    assert result["outcome"][0] == "D"
    assert json.loads(result["outcome"][1]) == [True, ["D", "awaited"]]
    assert result["sibling"] == ["D", "sibling"]


def _tolerated_setpgid_races(workdir: Path) -> list[Any]:
    real = os.setpgid

    def racing(pid: int, group: int) -> None:
        if pid != 0:
            raise next(errors)
        real(pid, group)

    errors = iter([ProcessLookupError(), PermissionError()])
    with _host() as host, mock.patch.object(os, "setpgid", racing):
        return [_show(host.outcome(host.fork(lambda: b"ok", ()).id)) for _ in range(2)]


def test_setpgid_races_in_the_parent_are_tolerated(tmp_path: Path) -> None:
    outcomes = run_callable(_tolerated_setpgid_races, tmp_path, timeout=RUN_TIMEOUT).value

    assert outcomes == [["D", "ok"]] * 2


def _exit_codes_with_ignorable_signal(workdir: Path) -> list[int]:
    """The statuses of children whose leaving signal does not terminate them."""
    host = ProcessHost()
    leaving = {
        "leave": lambda: _leave(156, signal.SIGWINCH),
        "handler": lambda: host._on_terminate(signal.SIGWINCH, None),
    }
    host._leaving = True
    codes = []
    for leave in leaving.values():
        pid = os.fork()
        if pid == 0:
            leave()
        _, status = os.waitpid(pid, 0)
        codes.append(os.waitstatus_to_exitcode(status))
    return codes


def test_a_child_leaves_with_the_exit_code_if_its_signal_does_not_terminate_it(
    tmp_path: Path,
) -> None:
    codes = run_callable(_exit_codes_with_ignorable_signal, tmp_path, timeout=RUN_TIMEOUT).value

    assert codes == [156, 128 + signal.SIGWINCH]


# ---------------------------------------------------------------------------
# Interrupts and failures around the fork
# ---------------------------------------------------------------------------


class _ParentHook(ForkParticipant[None]):
    """Records its parent hook, then runs *action* in the parent; records its abort."""

    def __init__(self, log: Path, name: str, action: Callable[[], None] | None = None) -> None:
        self.log = log
        self.name = name
        self.action = action

    def prepare(self, request: ForkRequest) -> None:
        return None

    def abort(self, state: None) -> None:
        with self.log.open("a") as handle:
            handle.write(f"abort {self.name}\n")

    def parent(self, state: None) -> None:
        with self.log.open("a") as handle:
            handle.write(f"{self.name}\n")
        if self.action is not None:
            self.action()


def _failing_parent_hook(workdir: Path) -> dict[str, Any]:
    log = workdir / "log"
    gate = _Gate()
    failures = [RuntimeError("parent hook failed")]

    def fail_once() -> None:
        if failures:
            raise failures.pop()

    with (
        _host(_ParentHook(log, "first", fail_once), _ParentHook(log, "second")) as host,
        _recorded_forks() as pids,
    ):
        try:
            host.fork(gate.wait_then(b"x"), ())
        except ProcessHostError as error:
            cause = type(error.__cause__).__name__
        else:
            cause = "none"
        report = {"hooks": _lines(log), "cause": cause, "reaped": _is_reaped(pids[0])}
        # The refused fork left nothing attached.
        report["next"] = _show(host.outcome(host.fork(lambda: b"next", ()).id))
    return report


def test_a_failing_parent_hook_runs_the_rest_and_kills_the_refused_child(
    tmp_path: Path,
) -> None:
    report = run_callable(_failing_parent_hook, tmp_path, timeout=RUN_TIMEOUT).value

    # A refused fork aborts every prepared state once its child is gone.
    assert report["hooks"] == ["first", "second", "abort second", "abort first"]
    assert report["cause"] == "RuntimeError"
    assert report["reaped"]
    assert report["next"] == ["D", "next"]


def _unexpected_setpgid_error(workdir: Path) -> dict[str, Any]:
    real = os.setpgid

    def failing(pid: int, group: int) -> None:
        if pid == 0:
            real(pid, group)
        else:
            raise OSError(errno.EINVAL, "invalid")

    gate = _Gate()
    log = workdir / "log"
    with (
        _host(_ParentHook(log, "only")) as host,
        _recorded_forks() as pids,
        mock.patch.object(os, "setpgid", failing),
    ):
        try:
            host.fork(gate.wait_then(b"x"), ())
        except ProcessHostError:
            refused = True
        else:
            refused = False
    return {"refused": refused, "reaped": _is_reaped(pids[0]), "hooks": _lines(log)}


def test_an_unexpected_setpgid_failure_refuses_the_fork_without_orphaning_the_child(
    tmp_path: Path,
) -> None:
    report = run_callable(_unexpected_setpgid_error, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"refused": True, "reaped": True, "hooks": ["abort only"]}


def _interrupt_after_fork(workdir: Path) -> dict[str, Any]:
    log = workdir / "log"
    gate = _Gate()

    def interrupt() -> None:
        os.kill(os.getpid(), signal.SIGINT)

    with _host(_ParentHook(log, "first", interrupt), _ParentHook(log, "second")) as host:
        with _recorded_forks() as pids:
            try:
                host.fork(gate.wait_then(b"x"), ())
            except KeyboardInterrupt:
                interrupted = True
            else:
                interrupted = False
        report = {"interrupted": interrupted, "hooks": _lines(log), "reaped": _is_reaped(pids[0])}
    return report


def test_an_interrupt_during_a_fork_is_delivered_once_the_fork_is_consistent(
    tmp_path: Path,
) -> None:
    report = run_callable(_interrupt_after_fork, tmp_path, timeout=RUN_TIMEOUT).value

    # The interrupt waited for every hook, then aborted the fork without leaving a child.
    assert report == {
        "interrupted": True,
        "hooks": ["first", "second", "abort second", "abort first"],
        "reaped": True,
    }


def _interrupted_wait(workdir: Path) -> dict[str, Any]:
    real = os.read
    interrupts = [KeyboardInterrupt()]

    def flaky(fd: int, size: int) -> bytes:
        if interrupts:
            raise interrupts.pop()
        return real(fd, size)

    gate = _Gate()
    with _host() as host:
        fds = open_fd_count()
        info = host.fork(gate.wait_then(b"done"), ())
        gate.open()
        # Patched after the fork, so only the parent's reads are interrupted.
        with mock.patch.object(os, "read", flaky), pytest.raises(KeyboardInterrupt):
            host.outcome(info.id)
        outcome = _show(host.outcome(info.id))
        return {"outcome": outcome, "leaked": open_fd_count() - fds}


def test_an_interrupted_wait_propagates_and_leaves_the_host_consistent(tmp_path: Path) -> None:
    report = run_callable(_interrupted_wait, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"outcome": ["D", "done"], "leaked": 0}


def _signal_before_the_wait_blocks(workdir: Path) -> dict[str, Any]:
    seen: list[int] = []
    signal.signal(signal.SIGUSR1, lambda number, frame: seen.append(number))
    gate = _Gate()

    def before(call: int) -> None:
        # The first poll starts with a signal tripped but the system call not interrupted.
        if call == 0:
            trip_signal_off_thread(signal.SIGUSR1)
        else:
            gate.open()

    with _host() as host:
        info = host.fork(gate.wait_then(b"done"), ())
        with before_each_poll(before):
            return {"outcome": _show(host.outcome(info.id)), "seen": seen}


def test_a_signal_that_did_not_interrupt_the_blocking_poll_still_wakes_the_wait(
    tmp_path: Path,
) -> None:
    report = run_callable(_signal_before_the_wait_blocks, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"outcome": ["D", "done"], "seen": [signal.SIGUSR1]}


def _signal_during_the_wait(workdir: Path) -> dict[str, Any]:
    def interrupt(number: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGUSR1, interrupt)
    parent = os.getpid()
    start, gate = _Gate(), _Gate()

    def child() -> bytes:
        start.pass_through()
        os.kill(parent, signal.SIGUSR1)
        gate.pass_through()
        return b"done"

    outcomes = []
    with _host() as host:
        fds = open_fd_count()
        for _ in range(_REPEATS):
            info = host.fork(child, ())
            with pytest.raises(KeyboardInterrupt):
                start.open()
                host.outcome(info.id)
            gate.open()
            outcomes.append(_show(host.outcome(info.id)))
        return {"outcomes": outcomes, "leaked": open_fd_count() - fds}


def test_a_signal_during_a_wait_interrupts_it_without_losing_the_child(tmp_path: Path) -> None:
    report = run_callable(_signal_during_the_wait, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"outcomes": [["D", "done"]] * _REPEATS, "leaked": 0}


def _signal_as_the_wait_acquires_its_wakeup(workdir: Path) -> dict[str, Any]:
    def interrupt(number: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGUSR1, interrupt)
    gate = _Gate()
    tool = 3

    def acquired(code: object, offset: int, result: object) -> None:
        # A real signal at the resource handoff, not an exception from the wait itself.
        os.kill(os.getpid(), signal.SIGUSR1)

    with _host() as host:
        fds = open_fd_count()
        info = host.fork(gate.wait_then(b"done"), ())
        sys.monitoring.use_tool_id(tool, "wakeup acquisition signal")
        try:
            sys.monitoring.register_callback(tool, sys.monitoring.events.PY_RETURN, acquired)
            sys.monitoring.set_local_events(
                tool, SignalWakeup.__enter__.__code__, sys.monitoring.events.PY_RETURN
            )
            with pytest.raises(KeyboardInterrupt):
                host.outcome(info.id)
        finally:
            sys.monitoring.set_local_events(tool, SignalWakeup.__enter__.__code__, 0)
            sys.monitoring.register_callback(tool, sys.monitoring.events.PY_RETURN, None)
            sys.monitoring.free_tool_id(tool)
        gate.open()
        return {
            "outcome": _show(host.outcome(info.id)),
            "reaped": _is_reaped(info.pid),
            "leaked": open_fd_count() - fds,
        }


def test_a_signal_at_the_waits_resource_handoff_leaves_no_descriptors(tmp_path: Path) -> None:
    report = run_callable(
        _signal_as_the_wait_acquires_its_wakeup, tmp_path, timeout=RUN_TIMEOUT
    ).value

    assert report == {"outcome": ["D", "done"], "reaped": True, "leaked": 0}


def _signal_after_waitpid_returns(workdir: Path) -> list[Any]:
    real = os.waitpid

    def waitpid(pid: int, options: int) -> tuple[int, int]:
        result = real(pid, options)
        os.kill(os.getpid(), signal.SIGINT)
        return result

    with _host() as host:
        info = host.fork(lambda: b"payload", ())
        with mock.patch.object(os, "waitpid", waitpid), pytest.raises(KeyboardInterrupt):
            host.outcome(info.id)
        return [_show(host.outcome(info.id)), _show(host.outcome(info.id))]


def test_an_interrupt_right_after_reaping_does_not_lose_the_exit_status(tmp_path: Path) -> None:
    outcomes = run_callable(_signal_after_waitpid_returns, tmp_path, timeout=RUN_TIMEOUT).value

    assert outcomes == [["D", "payload"]] * 2


def _interrupt_once_in_waitpid() -> contextlib.AbstractContextManager[object]:
    """Make the next blocking ``os.waitpid`` raise ``KeyboardInterrupt``."""
    real = os.waitpid
    interrupts = [KeyboardInterrupt()]

    def flaky(pid: int, options: int) -> tuple[int, int]:
        if interrupts:
            raise interrupts.pop()
        return real(pid, options)

    return mock.patch.object(os, "waitpid", flaky)


def _interrupted_reap(workdir: Path) -> dict[str, Any]:
    with _host() as host:
        fds = open_fd_count()
        dropped = host.fork(lambda: b"dropped", ())
        with _interrupt_once_in_waitpid(), pytest.raises(KeyboardInterrupt):
            host.outcome(dropped.id)
        # Detached before it was reaped: a later operation reaps it.
        host.detach(dropped.id)
        os.waitid(os.P_PID, dropped.pid, os.WEXITED | os.WNOWAIT)
        kept = host.fork(lambda: b"kept", ())
        report = {"kept": _show(host.outcome(kept.id)), "reaped": _is_reaped(dropped.pid)}
        report["leaked"] = open_fd_count() - fds
    return report


def test_an_interrupt_while_reaping_leaves_a_child_that_can_be_detached_and_reaped(
    tmp_path: Path,
) -> None:
    report = run_callable(_interrupted_reap, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"kept": ["D", "kept"], "reaped": True, "leaked": 0}


def _completed_after_interrupted_reap(workdir: Path) -> list[Any]:
    with _host() as host:
        info = host.fork(lambda: b"payload", ())
        with _interrupt_once_in_waitpid(), pytest.raises(KeyboardInterrupt):
            host.outcome(info.id)
        return [_show(host.outcome(info.id)), _show(host.outcome(info.id))]


def test_a_wait_completes_the_reaping_an_interrupt_cut_short(tmp_path: Path) -> None:
    outcomes = run_callable(_completed_after_interrupted_reap, tmp_path, timeout=RUN_TIMEOUT).value

    assert outcomes == [["D", "payload"]] * 2


def _teardown_interrupts(workdir: Path) -> dict[str, Any]:
    result = {}
    cases: dict[str, BaseException | signal.Signals] = {
        "exit": SystemExit(5),
        "term": signal.SIGTERM,
        "int": signal.SIGINT,
    }
    for name, hook in cases.items():
        log = workdir / f"{name}.log"
        with _host(_Recorder("a", log), _Recorder("b", log, exit_hook=hook)) as host:
            result[name] = [
                _show(host.outcome(host.fork(lambda: b"delivered", ()).id)),
                [line[2:] for line in _lines(log) if line.startswith("C exit")],
            ]
    return result


def test_an_interrupt_during_child_teardown_ends_the_child_like_any_other_interrupt(
    tmp_path: Path,
) -> None:
    result = run_callable(_teardown_interrupts, tmp_path, timeout=RUN_TIMEOUT).value

    assert result["exit"][0] == ["E", 5, None]
    assert result["term"][0] == ["E", None, signal.SIGTERM]
    assert result["int"][0] == ["E", None, signal.SIGINT]
    # The remaining participants still tear down in order.
    assert result["term"][1] == ["exit SETTLE:b", "exit SETTLE:a"]


def _stale_signals(workdir: Path) -> dict[str, Any]:
    def survive_term() -> None:
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except KeyboardInterrupt:
            pass

    def with_grandchild() -> bytes:
        survive_term()
        # The grandchild must not inherit the signal this child survived.
        grandchild = host.fork(lambda: _kill_self(signal.SIGINT), ())
        return json.dumps(_show(host.outcome(grandchild.id))).encode()

    def survives() -> bytes:
        survive_term()
        return b"survived"

    with _host() as host:
        grandchild = _show(host.outcome(host.fork(with_grandchild, ()).id))
    with _host(_Recorder("a", workdir / "log", exit_hook=signal.SIGINT)) as host:
        later = _show(host.outcome(host.fork(survives, ()).id))
    return {"grandchild": grandchild, "later": later}


def test_a_survived_termination_is_not_reported_for_later_interrupts(tmp_path: Path) -> None:
    result = run_callable(_stale_signals, tmp_path, timeout=RUN_TIMEOUT).value

    assert json.loads(result["grandchild"][1]) == ["E", None, signal.SIGINT]
    assert result["later"] == ["E", None, signal.SIGINT]


class _InterruptingPrepare(ForkParticipant[None]):
    def prepare(self, request: ForkRequest) -> None:
        os.kill(os.getpid(), signal.SIGINT)


def _interrupt_during_prepare(workdir: Path) -> dict[str, Any]:
    log = workdir / "log"
    with (
        _host(_Recorder("a", log), _InterruptingPrepare(), _Recorder("c", log)) as host,
        _recorded_forks() as pids,
    ):
        try:
            host.fork(lambda: b"x", ())
        except KeyboardInterrupt:
            interrupted = True
        else:
            interrupted = False
    return {"interrupted": interrupted, "log": _lines(log), "forks": len(pids)}


def test_an_interrupt_during_a_prepare_hook_propagates_at_once_and_aborts_earlier_states(
    tmp_path: Path,
) -> None:
    report = run_callable(_interrupt_during_prepare, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {
        "interrupted": True,
        "log": ["P prepare :a", "P abort a:a"],
        "forks": 0,
    }


def _interrupt_again_while_destroying(workdir: Path) -> dict[str, Any]:
    real_killpg = os.killpg

    def killpg(pgid: int, number: int) -> None:
        if number == signal.SIGKILL and not second:
            second.append(number)
            os.kill(os.getpid(), signal.SIGINT)
        real_killpg(pgid, number)

    def interrupt() -> None:
        os.kill(os.getpid(), signal.SIGINT)

    second: list[int] = []
    gate = _Gate()
    with _host(_ParentHook(workdir / "log", "hook", interrupt)) as host:
        with _recorded_forks() as pids, mock.patch.object(os, "killpg", killpg):
            try:
                host.fork(gate.wait_then(b"x"), ())
            except KeyboardInterrupt:
                interrupted = True
            else:
                interrupted = False
        return {"interrupted": interrupted, "second": len(second), "reaped": _is_reaped(pids[0])}


def test_a_second_interrupt_does_not_cut_short_the_teardown_of_a_refused_fork(
    tmp_path: Path,
) -> None:
    report = run_callable(_interrupt_again_while_destroying, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"interrupted": True, "second": 1, "reaped": True}


def _kill_if_escaped(parent: int) -> None:
    """Kill a child that came out of ``fork`` into the caller's code; a no-op in the parent."""
    os.kill(os.getpid(), signal.SIGKILL if os.getpid() != parent else 0)


def _signal_while_leaving(workdir: Path, kind: str, number: int) -> dict[str, Any]:
    """The child gets *number* just as it starts to leave (its ``_leave`` starts)."""
    marker = workdir / "marker"
    parent = os.getpid()
    sent: list[int] = []
    tool = 3

    def on_leave(code: object, offset: int) -> None:
        if os.getpid() != parent and not sent:
            sent.append(number)
            os.kill(os.getpid(), number)

    body = {"delivered": lambda: b"x", "term": lambda: _kill_self(signal.SIGTERM)}[kind]
    sys.monitoring.use_tool_id(tool, "late signal")
    try:
        sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, on_leave)
        sys.monitoring.set_local_events(tool, _leave.__code__, sys.monitoring.events.PY_START)
        with _host() as host:
            try:
                info = host.fork(body, ())
            finally:
                # Whether it returns or raises, `fork` must come out only in the parent.
                with marker.open("a") as handle:
                    handle.write("x")
                _kill_if_escaped(parent)
            outcome = _show(host.outcome(info.id))
    finally:
        sys.monitoring.set_local_events(tool, _leave.__code__, 0)
        sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, None)
        sys.monitoring.free_tool_id(tool)
    return {"outcome": outcome, "marker": marker.read_text()}


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
@pytest.mark.parametrize(
    ("kind", "expected"),
    [("delivered", lambda number: ["D", "x"]), ("term", lambda number: ["E", None, number])],
    ids=["delivered", "terminated"],
)
def test_a_signal_arriving_as_a_child_leaves_never_unwinds_into_the_callers_stack(
    tmp_path: Path, kind: str, expected: Callable[[int], list[Any]], number: signal.Signals
) -> None:
    report = run_callable(
        _signal_while_leaving, tmp_path, {"kind": kind, "number": int(number)}, timeout=RUN_TIMEOUT
    ).value

    # Only the parent comes out of `fork`; a late signal ends the child as that signal.
    assert report == {"outcome": expected(int(number)), "marker": "x"}


def _interrupt_between_read_and_append(workdir: Path) -> list[Any]:
    real = os.read
    interrupts = [signal.SIGINT]

    def read(fd: int, size: int) -> bytes:
        data = real(fd, size)
        if size == _READ_CHUNK and interrupts:
            os.kill(os.getpid(), interrupts.pop())
        return data

    gate = _Gate()
    with _host() as host:
        info = host.fork(gate.wait_then(b"done"), ())
        gate.open()
        # Patched after the fork, so only the parent's reads are affected.
        with mock.patch.object(os, "read", read), pytest.raises(KeyboardInterrupt):
            host.outcome(info.id)
        return [_show(host.outcome(info.id)), len(interrupts)]


def test_an_interrupt_right_after_a_read_does_not_lose_the_chunk(tmp_path: Path) -> None:
    report = run_callable(_interrupt_between_read_and_append, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == [["D", "done"], 0]


def _wait_for_the_exit_unheld(workdir: Path) -> dict[str, Any]:
    real = os.waitid
    interrupts = [signal.SIGINT]
    held: list[bool] = []

    def waitid(*args: Any) -> Any:
        held.append(signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, []))
        if interrupts:
            os.kill(os.getpid(), interrupts.pop())
        return real(*args)

    with _host() as host:
        info = host.fork(lambda: b"payload", ())
        with mock.patch.object(os, "waitid", waitid), pytest.raises(KeyboardInterrupt):
            host.outcome(info.id)
        return {"held": held, "outcome": _show(host.outcome(info.id))}


def test_waiting_for_a_child_to_exit_after_its_pipe_closed_is_interruptible(
    tmp_path: Path,
) -> None:
    report = run_callable(_wait_for_the_exit_unheld, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"held": [False], "outcome": ["D", "payload"]}


def _interrupt_during_detach(workdir: Path) -> dict[str, Any]:
    real = os.close
    gate = _Gate()
    with _host() as host:
        info = host.fork(gate.wait_then(b"x"), ())
        pipe_fd = host._entries[info.id].fd
        fds = open_fd_count()
        interrupts = [signal.SIGINT]

        def close(fd: int) -> None:
            if fd == pipe_fd and interrupts:
                os.kill(os.getpid(), interrupts.pop())
            real(fd)

        with mock.patch.object(os, "close", close), pytest.raises(KeyboardInterrupt):
            host.detach(info.id)
        released = fds - open_fd_count()
        refused = _refused(lambda: host.settle(info.id))
        gate.open()
        os.waitid(os.P_PID, info.pid, os.WEXITED | os.WNOWAIT)
        host.settle(host.fork(lambda: b"k", ()).id)
        return {"released": released, "refused": refused, "reaped": _is_reaped(info.pid)}


def test_an_interrupt_during_detach_leaves_the_child_released_and_reaped_later(
    tmp_path: Path,
) -> None:
    report = run_callable(_interrupt_during_detach, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"released": 1, "refused": True, "reaped": True}


def _wakeups_for_repeated_outcomes(workdir: Path) -> int:
    real = signal.set_wakeup_fd
    calls: list[int] = []

    def counting(fd: int, **kwargs: Any) -> int:
        calls.append(fd)
        return real(fd, **kwargs)

    with _host() as host:
        info = host.fork(lambda: b"x", ())
        host.outcome(info.id)
        before = len(calls)
        with mock.patch.object(signal, "set_wakeup_fd", counting):
            for _ in range(3):
                host.outcome(info.id)
        return len(calls) - before


def test_an_outcome_that_is_already_known_waits_on_nothing(tmp_path: Path) -> None:
    assert run_callable(_wakeups_for_repeated_outcomes, tmp_path, timeout=RUN_TIMEOUT).value == 0


def _inherited_wakeup_fd(workdir: Path) -> list[int]:
    real = signal.set_wakeup_fd

    def set_wakeup_fd(fd: int, **kwargs: Any) -> int:
        # asyncio, once imported, resets the wakeup fd in every forked child; the host must too.
        if sys._getframe(1).f_globals["__name__"] == "asyncio.events":
            return -1
        return real(fd, **kwargs)

    seen: list[int] = []
    signal.signal(signal.SIGUSR1, lambda number, frame: seen.append(number))
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)

    def child() -> bytes:
        os.kill(os.getpid(), signal.SIGUSR1)
        return bytes(seen)

    real(write_fd)
    try:
        with mock.patch.object(signal, "set_wakeup_fd", set_wakeup_fd), _host() as host:
            assert _delivered(host.outcome(host.fork(child, ()).id)) == bytes([signal.SIGUSR1])
        try:
            return list(os.read(read_fd, 16))
        except BlockingIOError:
            return []
    finally:
        real(-1)


def test_a_child_does_not_write_to_the_wakeup_pipe_of_its_parent(tmp_path: Path) -> None:
    assert run_callable(_inherited_wakeup_fd, tmp_path, timeout=RUN_TIMEOUT).value == []


def _surrogate_failures(workdir: Path) -> list[Any]:
    def raise_surrogate() -> bytes:
        raise ValueError("\ud800")

    with _host(_Recorder("a", workdir / "log", exit_hook=ValueError("\ud800"))) as host:
        return [_show(host.outcome(host.fork(fn, ()).id)) for fn in (raise_surrogate, lambda: b"")]


def test_a_failure_message_that_is_not_valid_unicode_is_still_delivered(tmp_path: Path) -> None:
    outcomes = run_callable(_surrogate_failures, tmp_path, timeout=RUN_TIMEOUT).value

    assert [outcome[0] for outcome in outcomes] == ["F", "F"]
    assert all("\\ud800" in outcome[1] for outcome in outcomes)


# ---------------------------------------------------------------------------
# Termination, scopes, and session end
# ---------------------------------------------------------------------------


def _duplicate_termination(workdir: Path) -> bool:
    ready, cleanup, release = _Gate(), _Gate(), _Gate()
    marker = workdir / "cleaned"

    def child() -> bytes:
        try:
            ready.open()
            signal.pause()
        finally:
            cleanup.open()
            release.pass_through()
            marker.touch()
        return b"unreachable"

    with _host() as host:
        info = host.fork(child, ())
        ready.wait()
        real_killpg = os.killpg
        sent = False

        def killpg(pgid: int, number: int) -> None:
            nonlocal sent
            real_killpg(pgid, number)
            if pgid == info.pid and number == signal.SIGTERM and not sent:
                sent = True
                # Hold the parent until the child is unwinding its first interrupt.
                cleanup.wait()

        with (
            mock.patch.object(os, "killpg", killpg),
            before_each_poll(lambda call: release.open()),
        ):
            host.terminate(info.id, info.id)
        return marker.exists()


def test_duplicate_termination_allows_child_cleanup_to_finish(tmp_path: Path) -> None:
    assert run_callable(_duplicate_termination, tmp_path, timeout=RUN_TIMEOUT).value


@pytest.mark.parametrize("how", ["terminate", "end", "settle"])
def test_cleanup_rechecks_groups_before_the_grace_deadline(tmp_path: Path, how: str) -> None:
    assert (
        run_callable(
            _group_disappears_during_poll, tmp_path, {"how": how}, timeout=RUN_TIMEOUT
        ).value
        < 1.0
    )


def _group_disappears_during_poll(workdir: Path, how: str) -> float:
    with _host() as host:
        info = host.fork(lambda: b"done", ())
        elapsed = 0.0
        group_alive = True
        poller = mock.Mock()

        def poll(timeout: int) -> list[tuple[int, int]]:
            nonlocal elapsed, group_alive
            elapsed += timeout / 1000
            group_alive = False
            return []

        def killpg(pgid: int, number: int) -> None:
            if not group_alive:
                raise ProcessLookupError

        poller.poll.side_effect = poll
        with mock.patch.object(os, "killpg", killpg):
            host.settle(info.id)
            with (
                mock.patch.object(time, "monotonic", lambda: elapsed),
                mock.patch.object(select, "poll", return_value=poller),
            ):
                if how == "terminate":
                    host.terminate(info.id)
                else:
                    host.end(Disposition.TERMINATE if how == "end" else Disposition.SETTLE)
        return elapsed


def _stubborn(ready: _Gate, gate: _Gate, termed: _Gate | None = None) -> Callable[[], bytes]:
    """A child that survives SIGTERM, then announces it on *ready* and blocks on *gate*.

    If *termed* is given, each SIGTERM opens it; otherwise SIGTERM is ignored.
    """

    def child() -> bytes:
        if termed is None:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        else:
            signal.signal(signal.SIGTERM, lambda number, frame: termed.open())
        ready.open()
        gate.pass_through()
        return b"stubborn"

    return child


def _pid_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _terminate_one(workdir: Path) -> dict[str, Any]:
    gate, ready = _Gate(), _Gate()
    report = {}
    # Cooperative processes get a grace period no test comes near.
    with _host() as host:
        cooperative = host.fork(gate.wait_then(b"unreachable"), ())
        finished = host.fork(lambda: b"done", ())
        host.settle(finished.id)
        host.terminate(cooperative.id)
        host.terminate(finished.id)
        report["cooperative"] = _show(host.outcome(cooperative.id))
        report["finished"] = _show(host.outcome(finished.id))
    # Every target of a short grace period is stubborn: it is killed whatever the timing.
    with _host(grace=0.3) as host:
        stubborn = host.fork(_stubborn(ready, gate), ())
        ready.wait()
        host.terminate(stubborn.id)
        report["stubborn"] = _show(host.outcome(stubborn.id))
    return report


def _surviving_process_group(workdir: Path, how: str, completed: bool) -> dict[str, Any]:
    announce, writer = os.pipe()
    gate = _Gate()

    def child() -> bytes:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "os.write(1, b'r'); time.sleep(60)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=writer,
            stderr=subprocess.DEVNULL,
        )
        os.close(writer)
        gate.pass_through()
        # Keep the Popen alive until the callable returns.
        return str(process.pid).encode()

    with _host(grace=600.0 if how == "interrupt" else 0.1) as host:
        info = host.fork(child, ())
        os.close(writer)
        assert os.read(announce, 1) == b"r"
        if completed or how == "settle":
            gate.open()
        if completed:
            host.decoded(info.id, _show)

        interrupted = False

        def interrupt_after_reaping(call: int) -> None:
            if how == "interrupt" and _is_reaped(info.pid):
                raise KeyboardInterrupt

        try:
            with before_each_poll(interrupt_after_reaping):
                if how in ("terminate", "interrupt"):
                    host.terminate(info.id)
                else:
                    host.end(Disposition.TERMINATE if how == "end" else Disposition.SETTLE)
        except KeyboardInterrupt:
            interrupted = True
        poller = select.poll()
        poller.register(announce, select.POLLHUP)
        try:
            gone = bool(poller.poll(5000))
        finally:
            kill_group(info.pid)
            os.close(announce)
        return {"gone": gone, "interrupted": interrupted, "reaped": _is_reaped(info.pid)}


@pytest.mark.parametrize(
    ("how", "completed"),
    [
        ("terminate", False),
        ("terminate", True),
        ("end", False),
        ("end", True),
        ("settle", False),
        ("settle", True),
        ("interrupt", False),
    ],
)
def test_cleanup_ends_a_process_group_after_its_leader_exits(
    tmp_path: Path, how: str, completed: bool
) -> None:
    report = run_callable(
        _surviving_process_group,
        tmp_path,
        {"how": how, "completed": completed},
        timeout=RUN_TIMEOUT,
    ).value

    assert report == {"gone": True, "interrupted": how == "interrupt", "reaped": True}


def _kill_the_stubborn_among_the_finished(workdir: Path) -> list[Any]:
    gate, ready = _Gate(), _Gate()

    with _host(grace=0.3) as host:
        cooperative = host.fork(gate.wait_then(b"unreachable"), ())
        stubborn = host.fork(_stubborn(ready, gate), ())
        ready.wait()

        def cooperative_has_ended(call: int) -> None:
            # Only the stubborn one is left for the deadline, whatever the timing.
            if call == 0:
                os.waitid(os.P_PID, cooperative.pid, os.WEXITED | os.WNOWAIT)

        with before_each_poll(cooperative_has_ended):
            host.end(Disposition.TERMINATE)
        return [_show(host.outcome(info.id)) for info in (cooperative, stubborn)]


def test_a_termination_kills_only_the_processes_that_outlive_the_grace_period(
    tmp_path: Path,
) -> None:
    outcomes = run_callable(_kill_the_stubborn_among_the_finished, tmp_path, timeout=RUN_TIMEOUT)

    assert outcomes.value == [["E", None, signal.SIGTERM], ["E", None, signal.SIGKILL]]


def test_terminating_a_process_ends_it_by_sigterm_then_sigkill_and_spares_finished_ones(
    tmp_path: Path,
) -> None:
    outcomes = run_callable(_terminate_one, tmp_path, timeout=RUN_TIMEOUT).value

    assert outcomes == {
        "cooperative": ["E", None, signal.SIGTERM],
        "stubborn": ["E", None, signal.SIGKILL],
        "finished": ["D", "done"],
    }


def _concurrent_termination(workdir: Path) -> dict[str, list[Any]]:
    """Each child leaves only after the other has been told to terminate."""
    hold = _Gate()

    def handshaking(index: int, termed: list[_Gate], ready: list[_Gate]) -> Callable[[], bytes]:
        def child() -> bytes:
            def on_terminate(number: int, frame: object) -> None:
                termed[index].open()
                termed[1 - index].wait()
                raise SystemExit(7 + index)

            signal.signal(signal.SIGTERM, on_terminate)
            ready[index].open()
            hold.pass_through()
            return b"unreachable"

        return child

    report: dict[str, list[Any]] = {}
    # A sequential termination would wait out the grace period for the first child.
    for how in ("end", "terminate"):
        termed, ready = [_Gate(), _Gate()], [_Gate(), _Gate()]
        with _host(grace=5.0) as host:
            infos = [host.fork(handshaking(index, termed, ready), ()) for index in range(2)]
            for gate in ready:
                gate.wait()
            if how == "end":
                host.end(Disposition.TERMINATE)
            else:
                host.terminate(*(info.id for info in infos))
            report[how] = [_show(host.outcome(info.id)) for info in infos]
    return report


def test_several_processes_are_terminated_concurrently_under_one_deadline(
    tmp_path: Path,
) -> None:
    report = run_callable(_concurrent_termination, tmp_path, timeout=RUN_TIMEOUT).value

    expected = [["E", 7, None], ["E", 8, None]]
    assert report == {"end": expected, "terminate": expected}


def _sigterms_sent_to_a_group_leader(workdir: Path) -> int:
    """How many SIGTERMs reach a child that leads its own group during its termination."""
    gate = _Gate()
    sent: list[int] = []
    real_kill, real_killpg = os.kill, os.killpg

    with _host() as host:
        info = host.fork(gate.wait_then(b"unreachable"), ())

        def kill(pid: int, number: int) -> None:
            if pid == info.pid:
                sent.append(number)
            real_kill(pid, number)

        def killpg(pgid: int, number: int) -> None:
            if pgid == info.pid:
                sent.append(number)
            real_killpg(pgid, number)

        with mock.patch.object(os, "kill", kill), mock.patch.object(os, "killpg", killpg):
            host.terminate(info.id)
    return sent.count(signal.SIGTERM)


def test_a_terminated_child_receives_sigterm_once(tmp_path: Path) -> None:
    # A duplicate signal re-enters a handler that is still cleaning up.
    count = run_callable(_sigterms_sent_to_a_group_leader, tmp_path, timeout=RUN_TIMEOUT).value

    assert count == 1


def _interrupted_termination(workdir: Path) -> dict[str, Any]:
    ready, gate = _Gate(), _Gate()

    interrupts = [signal.SIGINT]

    def interrupt(call: int) -> None:
        if interrupts:
            os.kill(os.getpid(), interrupts.pop())

    # A grace period that no test waits out: only the interrupt can end the termination.
    with _host(grace=600.0) as host:
        stubborn = host.fork(_stubborn(ready, gate), ())
        ready.wait()
        with before_each_poll(interrupt):
            try:
                host.terminate(stubborn.id)
            except KeyboardInterrupt:
                interrupted = True
            else:
                interrupted = False
        return {
            "interrupted": interrupted,
            "reaped": _is_reaped(stubborn.pid),
            "outcome": _show(host.outcome(stubborn.id)),
        }


def test_an_interrupt_during_termination_kills_the_remaining_processes_at_once(
    tmp_path: Path,
) -> None:
    report = run_callable(_interrupted_termination, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {
        "interrupted": True,
        "reaped": True,
        "outcome": ["E", None, signal.SIGKILL],
    }


def _interrupt_in_completion_predicate(workdir: Path, how: str) -> dict[str, Any]:
    ready, gate = _Gate(), _Gate()
    sent = False
    predicates = {
        "ProcessHost._all_done.<locals>.<lambda>",
        "ProcessHost._all_done.<locals>.done",
        "ProcessHost._terminate.<locals>.done",
    }

    def trace(frame: FrameType, event: str, arg: Any) -> Any:
        nonlocal sent
        if not sent and frame.f_code.co_filename == ProcessHost.settle_any.__code__.co_filename:
            # Interrupt generator close, or the replacement predicate's normal return.
            closing = (
                event == "exception"
                and frame.f_code.co_name == "<genexpr>"
                and arg[0] is GeneratorExit
            )
            returning = event == "return" and (
                (arg is False and frame.f_code.co_qualname in predicates)
                or (
                    arg is True
                    and frame.f_code.co_qualname == "ProcessHost.settle_any.<locals>.finished"
                )
            )
            if closing or returning:
                sent = True
                os.kill(os.getpid(), signal.SIGINT)
        return trace

    with _host(grace=0.0) as host:
        info = host.fork(_stubborn(ready, gate), ())
        ready.wait()
        if how == "settle":
            gate.open()
            host.settle(info.id)
        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            try:
                if how == "terminate":
                    host.terminate(info.id)
                elif how == "end_settle":
                    host.end(Disposition.SETTLE)
                else:
                    host.settle_any([info.id])
            except KeyboardInterrupt:
                interrupted = True
            else:
                interrupted = False
        finally:
            sys.settrace(previous)
        return {
            "sent": sent,
            "interrupted": interrupted,
            "reaped": _is_reaped(info.pid),
            "outcome": _show(host.outcome(info.id)),
        }


@pytest.mark.parametrize("how", ["end_settle", "terminate", "settle"])
def test_completion_predicates_do_not_lose_sigint(tmp_path: Path, how: str) -> None:
    report = run_callable(
        _interrupt_in_completion_predicate, tmp_path, {"how": how}, timeout=RUN_TIMEOUT
    ).value

    assert report == {
        "sent": True,
        "interrupted": True,
        "reaped": True,
        "outcome": ["D", "stubborn"] if how == "settle" else ["E", None, signal.SIGKILL],
    }


def _scopes(workdir: Path) -> dict[str, Any]:
    open_gate, doomed = _Gate(), _Gate()
    report: dict[str, Any] = {}
    exceptions = {"error": ValueError("x"), "interrupt": KeyboardInterrupt(), "exit": SystemExit(2)}
    with _host() as host:
        outside = host.fork(open_gate.wait_then(b"outside"), ())
        for name, exception in exceptions.items():
            try:
                with host.scope():
                    first = host.fork(doomed.wait_then(b"first"), ())
                    with host.scope():
                        nested = host.fork(doomed.wait_then(b"nested"), ())
                    raise exception
            except (ValueError, KeyboardInterrupt, SystemExit):
                pass
            report[name] = [_show(host.outcome(info.id)) for info in (first, nested)]
        with host.scope():
            kept = host.fork(open_gate.wait_then(b"kept"), ())
            try:
                with host.scope():
                    inner = host.fork(doomed.wait_then(b"inner"), ())
                    raise ValueError
            except ValueError:
                pass
            report["inner"] = _show(host.outcome(inner.id))
        # One byte for each of the two waiting processes.
        open_gate.open()
        open_gate.open()
        report["outside"] = _show(host.outcome(outside.id))
        report["normal exit"] = _show(host.outcome(kept.id))
    return report


def test_leaving_a_scope_by_exception_terminates_only_the_processes_forked_inside_it(
    tmp_path: Path,
) -> None:
    report = run_callable(_scopes, tmp_path, timeout=RUN_TIMEOUT).value

    terminated = ["E", None, signal.SIGTERM]
    for name in ("error", "interrupt", "exit"):
        assert report[name] == [terminated, terminated]
    assert report["inner"] == terminated
    assert report["outside"] == ["D", "outside"]
    assert report["normal exit"] == ["D", "kept"]


def _terminated_right_after_the_fork(workdir: Path) -> dict[str, Any]:
    gate, doomed_gate = _Gate(), _Gate()
    with _host() as host:
        bystander = host.fork(gate.wait_then(b"bystander"), ())
        outcomes = []
        for _ in range(_REPEATS):
            doomed = host.fork(doomed_gate.wait_then(b"unreachable"), ())
            host.terminate(doomed.id)
            outcomes.append(_show(host.outcome(doomed.id)))
        gate.open()
        return {"doomed": outcomes, "bystander": _show(host.outcome(bystander.id))}


def test_a_child_interrupted_as_it_starts_never_touches_its_parents_processes(
    tmp_path: Path,
) -> None:
    report = run_callable(_terminated_right_after_the_fork, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {
        "doomed": [["E", None, signal.SIGTERM]] * _REPEATS,
        "bystander": ["D", "bystander"],
    }


def _delivered_failures(workdir: Path) -> dict[str, Any]:
    log = workdir / "log"
    with _host(_Recorder("a", log)) as host:
        children = {
            "failure": host.fork(lambda: Delivered(b"carrier", failure="the summary"), ()),
            "empty": host.fork(lambda: Delivered(b"", failure=""), ()),
            "unicode": host.fork(lambda: Delivered(b"ok", failure="é中"), ()),
            "value": host.fork(lambda: Delivered(b"value"), ()),
        }
        outcomes = {name: _show(host.outcome(info.id)) for name, info in children.items()}
    exits = [line[2:] for line in _lines(log) if line.startswith("C exit")]
    return {"outcomes": outcomes, "exits": sorted(exits)}


def test_a_delivered_failure_carries_its_payload_and_summary_and_tears_down_as_a_failure(
    tmp_path: Path,
) -> None:
    result = run_callable(_delivered_failures, tmp_path, timeout=RUN_TIMEOUT).value

    assert result["outcomes"] == {
        "failure": ["DF", "carrier", "the summary"],
        "empty": ["DF", "", ""],
        "unicode": ["DF", "ok", "é中"],
        "value": ["D", "value"],
    }
    assert result["exits"] == ["exit SETTLE:a", *["exit TERMINATE:a"] * 3]


def _settling_end(workdir: Path) -> dict[str, Any]:
    gate, late = _Gate(), _Gate()
    reports: list[Any] = []
    stderr = io.StringIO()
    with _host(reporter=lambda info, text: reports.append([info.pid, text])) as host:

        def opener() -> bytes:
            gate.open()
            return b"opened"

        waiter = host.fork(gate.wait_then(b"waited"), ())
        opened = host.fork(opener, ())
        carrying = host.fork(lambda: Delivered(b"carrier", failure="summary-text"), ())
        raising = host.fork(_raise_value_error, ())
        exiting = host.fork(lambda: sys.exit(3), ())
        killed = host.fork(lambda: _kill_self(signal.SIGKILL), ())
        consumed = host.fork(_raise_value_error, ())
        host.outcome(consumed.id)
        released = host.fork(late.wait_then(b"x"), ())
        host.detach(released.id)
        with contextlib.redirect_stderr(stderr):
            host.end(Disposition.SETTLE)
            settled = [_is_reaped(info.pid) for info in (waiter, opened, carrying, raising)]
            host.end(Disposition.SETTLE)
        late.open()
        os.waitid(os.P_PID, released.pid, os.WEXITED | os.WNOWAIT)
        host.end(Disposition.SETTLE)
        result: dict[str, Any] = {
            "settled": settled,
            "reaped detached": _is_reaped(released.pid),
            "pids": [info.pid for info in (carrying, raising, exiting, killed)],
            "reports": reports,
            "warnings": stderr.getvalue().splitlines(),
        }
    # Without a reporter, the warning is all there is.
    alone = io.StringIO()
    with _host() as unreported, contextlib.redirect_stderr(alone):
        unreported.fork(_raise_value_error, ())
        unreported.end(Disposition.SETTLE)
    result["alone"] = alone.getvalue().splitlines()
    return result


def test_ending_by_settling_waits_for_all_and_warns_once_about_unconsumed_failures(
    tmp_path: Path,
) -> None:
    result = run_callable(_settling_end, tmp_path, timeout=RUN_TIMEOUT).value

    assert all(result["settled"])
    assert result["reaped detached"]
    carrying, raising, exiting, killed = result["pids"]
    # Clean, consumed, and detached processes are not reported, and nothing is reported twice.
    assert [report[0] for report in result["reports"]] == [carrying, raising, exiting, killed]
    assert "summary-text" in result["reports"][0][1]
    assert "boom" in result["reports"][1][1]
    warnings = result["warnings"]
    assert len(warnings) == 4
    # The reporter receives the text of the warning.
    for line, pid, report in zip(warnings, result["pids"], result["reports"], strict=True):
        assert str(pid) in line
        assert line.endswith(report[1])
    assert "summary-text" in warnings[0]
    assert "boom" in warnings[1]
    assert len(result["alone"]) == 1


def _terminating_end(workdir: Path) -> dict[str, Any]:
    running, survivor = _Gate(), _Gate()
    reports: list[Any] = []
    stderr = io.StringIO()
    with _host(reporter=lambda info, text: reports.append(info.pid)) as host:
        doomed = host.fork(running.wait_then(b"unreachable"), ())
        released = host.fork(survivor.wait_then(b"x"), ())
        host.detach(released.id)
        with contextlib.redirect_stderr(stderr):
            host.end(Disposition.TERMINATE)
        survivor.open()
        _, status = os.waitpid(released.pid, 0)
        return {
            "doomed": _show(host.outcome(doomed.id)),
            "released": os.waitstatus_to_exitcode(status),
            "reports": reports,
            "warnings": stderr.getvalue(),
        }


def test_ending_by_terminating_ends_attached_processes_and_spares_detached_ones(
    tmp_path: Path,
) -> None:
    result = run_callable(_terminating_end, tmp_path, timeout=RUN_TIMEOUT).value

    assert result == {
        "doomed": ["E", None, signal.SIGTERM],
        "released": 0,
        "reports": [],
        "warnings": "",
    }


def _interrupted_settling_end(workdir: Path) -> dict[str, Any]:
    gate = _Gate()

    interrupts = [signal.SIGINT]

    def interrupt(call: int) -> None:
        if interrupts:
            os.kill(os.getpid(), interrupts.pop())

    with _host() as host:
        infos = [host.fork(gate.wait_then(b"unreachable"), ()) for _ in range(2)]
        with before_each_poll(interrupt):
            try:
                host.end(Disposition.SETTLE)
            except KeyboardInterrupt:
                interrupted = True
            else:
                interrupted = False
        return {
            "interrupted": interrupted,
            "outcomes": [_show(host.outcome(info.id)) for info in infos],
        }


def test_an_interrupt_while_settling_terminates_the_remaining_processes(tmp_path: Path) -> None:
    report = run_callable(_interrupted_settling_end, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"interrupted": True, "outcomes": [["E", None, signal.SIGTERM]] * 2}


def _child_settles_its_grandchildren(workdir: Path) -> dict[str, Any]:
    marker = workdir / "marker"
    forked, release = _Gate(), _Gate()

    def grandchild() -> bytes:
        release.pass_through()
        marker.write_text("done")
        return b""

    def middle() -> bytes:
        info = host.fork(grandchild, ())
        forked.open()
        return str(info.pid).encode()

    with _host() as host:
        child = host.fork(middle, ())
        forked.wait()
        release.open()
        outcome = host.outcome(child.id)
        return {
            "delivered": isinstance(outcome, Delivered),
            "marker": marker.exists(),
            "gone": isinstance(outcome, Delivered) and _pid_is_gone(int(outcome.payload)),
        }


def test_a_child_that_ends_normally_awaits_its_grandchildren_before_delivering(
    tmp_path: Path,
) -> None:
    report = run_callable(_child_settles_its_grandchildren, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"delivered": True, "marker": True, "gone": True}


def _child_terminates_its_grandchildren(workdir: Path, stubborn: bool) -> dict[str, Any]:
    unwound, pid_file = workdir / "unwound", workdir / "pid"
    ready, gate = _Gate(), _Gate()

    def cooperative() -> bytes:
        try:
            ready.open()
            gate.pass_through()
        finally:
            unwound.write_text("unwound")
        return b""

    def middle() -> bytes:
        grandchild = host.fork(_stubborn(ready, gate) if stubborn else cooperative, ())
        ready.wait()
        pid_file.write_text(str(grandchild.pid))
        raise ValueError("middle failed")

    # A stubborn grandchild is killed whatever the timing; a cooperative one must be allowed
    # to unwind.
    with _host(grace=0.3 if stubborn else 30.0) as host:
        outcome = _show(host.outcome(host.fork(middle, ()).id))
        return {
            "outcome": outcome[0],
            "unwound": unwound.exists(),
            "gone": _pid_is_gone(int(pid_file.read_text())),
        }


@pytest.mark.parametrize("stubborn", [False, True], ids=["cooperative", "stubborn"])
def test_a_child_that_fails_terminates_its_grandchildren_escalating_to_sigkill(
    tmp_path: Path, stubborn: bool
) -> None:
    report = run_callable(
        _child_terminates_its_grandchildren, tmp_path, {"stubborn": stubborn}, timeout=RUN_TIMEOUT
    ).value

    assert report == {"outcome": "F", "unwound": not stubborn, "gone": True}


def _terminated_while_settling(workdir: Path) -> dict[str, Any]:
    ready, settling, termed, gate = _Gate(), _Gate(), _Gate(), _Gate()
    alive_read, alive_write = os.pipe()
    root = os.getpid()

    def middle() -> bytes:
        host.fork(_stubborn(ready, gate, termed), ())
        return b"unreachable"

    def before_poll(call: int) -> None:
        if os.getppid() == root:
            settling.open()

    # A grace period that no test waits out: only the second SIGTERM can end the termination.
    with _host(grace=600.0) as host, before_each_poll(before_poll):
        child = host.fork(middle, ())
        os.close(alive_write)
        # Await the grandchild's handler and the child's automatic settling, not its callable.
        ready.wait()
        settling.wait()
        kill_group(child.pid, signal.SIGTERM)
        termed.wait()
        kill_group(child.pid, signal.SIGTERM)
        outcome = _show(host.outcome(child.id))
        # EOF: the child and its grandchild, which ignores the first SIGTERM, are gone.
        eof = os.read(alive_read, 1) == b""
    os.close(alive_read)
    return {"outcome": outcome, "eof": eof}


def test_a_child_terminated_while_settling_terminates_its_children_and_a_second_signal_kills(
    tmp_path: Path,
) -> None:
    report = run_callable(_terminated_while_settling, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"outcome": ["E", None, signal.SIGTERM], "eof": True}


def _leaving_its_group(inner: Callable[[], bytes]) -> Callable[[], bytes]:
    """A child that moves into the group of a helper process before it runs *inner*.

    Its own group is then gone, so it can be signalled by its pid only. The helper ends when
    the child does.
    """

    def child() -> bytes:
        read_fd, write_fd = os.pipe()
        helper = os.fork()
        if helper == 0:
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(number, signal.SIG_DFL)
            os.close(write_fd)
            os.read(read_fd, 1)
            os._exit(0)
        os.setpgid(helper, helper)
        os.setpgid(0, helper)
        return inner()

    return child


def _terminate_outside_the_group(workdir: Path) -> dict[str, Any]:
    gate, ready = _Gate(), _Gate()
    report = {}

    def cooperative() -> bytes:
        ready.open()
        gate.pass_through()
        return b"unreachable"

    with _host() as host:
        moved = host.fork(_leaving_its_group(cooperative), ())
        ready.wait()
        host.terminate(moved.id)
        report["cooperative"] = _show(host.outcome(moved.id))
    stubborn_ready = _Gate()
    with _host(grace=0.3) as host:
        moved = host.fork(_leaving_its_group(_stubborn(stubborn_ready, gate)), ())
        stubborn_ready.wait()
        host.terminate(moved.id)
        report["stubborn"] = _show(host.outcome(moved.id))
    return report


def test_a_process_that_left_its_group_is_still_terminated_escalating_to_sigkill(
    tmp_path: Path,
) -> None:
    report = run_callable(_terminate_outside_the_group, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {
        "cooperative": ["E", None, signal.SIGTERM],
        "stubborn": ["E", None, signal.SIGKILL],
    }


def _terminate_above_a_stubborn_grandchild(workdir: Path) -> dict[str, Any]:
    ready, forked, gate = _Gate(), _Gate(), _Gate()
    pid_file = workdir / "pid"

    def middle() -> bytes:
        grandchild = host.fork(_stubborn(ready, gate), ())
        ready.wait()
        pid_file.write_text(str(grandchild.pid))
        forked.open()
        gate.pass_through()
        return b"unreachable"

    # The child's grace is half of the parent's: the child ends before the parent kills it.
    with _host(grace=4.0) as host:
        child = host.fork(middle, ())
        forked.wait()
        pid = int(pid_file.read_text())
        try:
            host.terminate(child.id)
            return {"outcome": _show(host.outcome(child.id)), "gone": _pid_is_gone(pid)}
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_terminating_a_child_ends_its_stubborn_grandchildren_before_it_is_killed(
    tmp_path: Path,
) -> None:
    report = run_callable(_terminate_above_a_stubborn_grandchild, tmp_path, timeout=RUN_TIMEOUT)

    assert report.value == {"outcome": ["E", None, signal.SIGTERM], "gone": True}


class _ExitLog(ForkParticipant[None]):
    """Appends ``pid disposition`` to a file for every child exit."""

    def __init__(self, log: Path) -> None:
        self.log = log

    def prepare(self, request: ForkRequest) -> None:
        return None

    def child_exit(self, disposition: Disposition) -> None:
        with self.log.open("a") as handle:
            handle.write(f"{os.getpid()} {disposition.name}\n")


def _interrupted_settling_exits(workdir: Path) -> list[str]:
    log = workdir / "log"
    gate = _Gate()
    parent = os.getpid()
    armed: list[None] = []

    def interrupt(call: int) -> None:
        # The child's wait for its grandchild gets its termination.
        if os.getpid() != parent and armed:
            armed.clear()
            os.kill(os.getpid(), signal.SIGTERM)

    def settling() -> bytes:
        host.fork(gate.wait_then(b"unreachable"), ())
        armed.append(None)
        return b"delivered"

    with _host(_ExitLog(log)) as host, before_each_poll(interrupt):
        child = host.fork(settling, ())
        outcome = _show(host.outcome(child.id))
    exits = [line.split() for line in _lines(log)]
    return [outcome[0], *[disposition for pid, disposition in exits if int(pid) == child.pid]]


def test_a_child_whose_settling_is_interrupted_runs_its_exit_hooks_as_terminated(
    tmp_path: Path,
) -> None:
    report = run_callable(_interrupted_settling_exits, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == ["E", "TERMINATE"]


def _interrupted_last_resort_termination(workdir: Path, exits: bool) -> list[Any]:
    gate = _Gate()
    parent = os.getpid()
    armed: list[None] = []
    armed_last_resort: list[None] = []
    tool = 3

    def interrupt_teardown(code: object, offset: int) -> None:
        # The teardown is cut short before it ends the child's processes, so the last resort
        # has work to do.
        if os.getpid() != parent and armed:
            armed.clear()
            armed_last_resort.append(None)
            os.kill(os.getpid(), signal.SIGTERM)

    def interrupt_last_resort(call: int) -> None:
        if armed_last_resort:
            armed_last_resort.clear()
            if exits:
                raise SystemExit(7)
            os.kill(os.getpid(), signal.SIGINT)

    def child() -> bytes:
        host.fork(gate.wait_then(b"unreachable"), ())
        armed.append(None)
        return b"delivered"

    sys.monitoring.use_tool_id(tool, "cut-short teardown")
    try:
        sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, interrupt_teardown)
        sys.monitoring.set_local_events(
            tool, ProcessHost._teardown.__code__, sys.monitoring.events.PY_START
        )
        with _host() as host, before_each_poll(interrupt_last_resort):
            return _show(host.outcome(host.fork(child, ()).id))
    finally:
        sys.monitoring.set_local_events(tool, ProcessHost._teardown.__code__, 0)
        sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, None)
        sys.monitoring.free_tool_id(tool)


@pytest.mark.parametrize(
    ("exits", "expected"),
    [(False, ["E", None, signal.SIGINT]), (True, ["E", 7, None])],
    ids=["signal", "exit"],
)
def test_an_interrupt_during_the_last_resort_termination_ends_the_child_that_way(
    tmp_path: Path, exits: bool, expected: list[Any]
) -> None:
    outcome = run_callable(
        _interrupted_last_resort_termination, tmp_path, {"exits": exits}, timeout=RUN_TIMEOUT
    ).value

    assert outcome == expected


def _unconsumed_failures_after_terminations(workdir: Path) -> dict[str, Any]:
    stderr = io.StringIO()

    def failed_child(host: ProcessHost) -> ProcessInfo:
        info = host.fork(_raise_value_error, ())
        # Exited, so its failure waits unread in its pipe.
        os.waitid(os.P_PID, info.pid, os.WEXITED | os.WNOWAIT)
        return info

    with _host() as host:
        try:
            with host.scope():
                in_scope = failed_child(host)
                raise ValueError
        except ValueError:
            pass
        explicit = failed_child(host)
        host.terminate(explicit.id)
        ended = failed_child(host)
        host.end(Disposition.TERMINATE)
        with contextlib.redirect_stderr(stderr):
            host.end(Disposition.SETTLE)
        return {
            "reported": [
                f"process {info.pid} " in stderr.getvalue() for info in (in_scope, explicit, ended)
            ],
            "lines": len(stderr.getvalue().splitlines()),
        }


def test_only_an_explicit_termination_counts_as_consuming_the_outcome(tmp_path: Path) -> None:
    report = run_callable(_unconsumed_failures_after_terminations, tmp_path, timeout=RUN_TIMEOUT)

    assert report.value == {"reported": [True, False, True], "lines": 2}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _record(tag: bytes, length: int, body: bytes) -> bytes:
    return tag + length.to_bytes(8, "big") + body


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_record(b"D", 3, b"abc"), Delivered(b"abc")),
        (_record(b"D", 0, b""), Delivered(b"")),
        (_record(b"F", 4, "booé".encode()[:4]), Failed("boo�")),
        (_record(b"X", 11, (3).to_bytes(8, "big") + b"abc"), Delivered(b"", "abc")),
        (_record(b"X", 14, (3).to_bytes(8, "big") + b"abcpay"), Delivered(b"pay", "abc")),
        (_record(b"X", 9, (0).to_bytes(8, "big") + b"p"), Delivered(b"p", "")),
        (_record(b"X", 14, (3).to_bytes(8, "big") + b"abcpay")[:-1], None),
        (_record(b"X", 3, b"abc"), None),
        (_record(b"X", 11, (9).to_bytes(8, "big") + b"abc"), None),
        (_record(b"D", 3, b"abc")[:-1], None),
        (_record(b"D", 3, b"abc") + b"x", None),
        (_record(b"D", 3, b"abc")[:5], None),
        (b"", None),
        (_record(b"Z", 1, b"a"), None),
    ],
)
def test_outcome_records_are_complete_only_when_framed_exactly(
    data: bytes, expected: Outcome | None
) -> None:
    assert parse_record(data) == expected


def test_an_interrupt_leaves_with_its_signal_by_default_sigint() -> None:
    assert _interrupt_end(KeyboardInterrupt(), signal.SIGTERM) == (128 + signal.SIGTERM, 15)
    assert _interrupt_end(KeyboardInterrupt(), None) == (128 + signal.SIGINT, signal.SIGINT)


def test_companion_runtime_processes_need_an_active_extern_call(tmp_path: Path) -> None:
    path = write_companion_file(
        tmp_path,
        "entry",
        "from agl import ProcessHostError, runtime\n"
        "\n"
        "def probe():\n"
        "    try:\n"
        "        runtime.processes\n"
        "    except ProcessHostError:\n"
        "        return True\n"
        "    return False\n",
    )
    companion = ExternRegistry().load_companion(ENTRY_ID, path)

    assert companion.probe()


def _decoded_outcomes(workdir: Path) -> dict[str, Any]:
    calls = []
    reports = []
    with _host(reporter=lambda info, problem: reports.append(problem)) as host:
        process = host.fork(lambda: Delivered(b"payload", failure="failure"), ())

        def decode(outcome: Outcome) -> object:
            calls.append(outcome)
            if len(calls) == 1:
                raise ValueError("retry")
            return None

        with pytest.raises(ValueError):
            host.decoded(process.id, decode)
        assert host.decoded(process.id, decode) is None
        assert host.decoded(process.id, decode) is None
        host.settle(process.id)
        assert host.settle_any([process.id]) == 0
        host.terminate(process.id)
        with pytest.raises(ProcessHostError):
            host.outcome(process.id)
        host.end(Disposition.SETTLE)
        host.detach(process.id)
        for id in (process.id, "unknown"):
            with pytest.raises(ProcessHostError):
                host.decoded(id, decode)
    return {"calls": len(calls), "reports": reports}


def test_decoded_outcomes_retry_failures_and_retain_only_the_decoded_value(tmp_path: Path) -> None:
    assert run_callable(_decoded_outcomes, tmp_path).value == {"calls": 2, "reports": []}
