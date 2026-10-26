"""The process host: forking, outcome transport, waiting, ownership, and participants.

Forking scenarios run in the isolated runner, since the suite's own processes are
multi-threaded. Children `setpgid` into groups of their own, which the runner's group kill does
not reach, so every scenario's host fixture reaps its children and fails on a leftover one.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import resource
import signal
import sys
import threading
import warnings
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest

from agnette.agl.modules.ids import ENTRY_ID
from agnette.agl.runtime.externs import ExternRegistry, ExternRuntimeState
from agnette.agl.runtime.fork_participants import CompanionStateParticipant, StdioFlushParticipant
from agnette.agl.runtime.processes import (
    _READ_CHUNK,
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
    _parse_record,
)
from tests._signal_helpers import before_each_poll, trip_signal_off_thread
from tests.agl.ir_harness import write_companion_file
from tests.agl.isolated_runner import run_callable

RUN_TIMEOUT = 30.0
_REPEATS = 20


# ---------------------------------------------------------------------------
# Scenario helpers (these run inside the isolated subprocess)
# ---------------------------------------------------------------------------


class _TrackedHost(ProcessHost):
    """A host that remembers its attached children, for the fixture's cleanup."""

    def __init__(self) -> None:
        super().__init__()
        self.attached: list[ProcessInfo] = []

    def fork(self, child: Callable[[], bytes], sessions: Sequence[str]) -> ProcessInfo:
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
def _host(*participants: ForkParticipant[Any]) -> Iterator[ProcessHost]:
    """A host that reaps its children on exit and fails if any child is left behind."""
    host = _TrackedHost()
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
        os.read(self.read_fd, 1)

    def wait_then(self, payload: bytes) -> Callable[[], bytes]:
        def child() -> bytes:
            self.pass_through()
            return payload

        return child

    def open(self) -> None:
        os.write(self.write_fd, b"x")


def _show(outcome: Outcome) -> list[Any]:
    match outcome:
        case Delivered(payload):
            return ["D", payload.decode()]
        case Failed(message):
            return ["F", message]
        case Ended(exit_code, signal_number):
            return ["E", exit_code, signal_number]


def _kill_self(number: signal.Signals) -> bytes:
    os.kill(os.getpid(), number)
    return b"unreachable"


def _raise_value_error() -> bytes:
    raise ValueError("boom")


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


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


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
    sys.stdout.write("before-")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with _host(StdioFlushParticipant()) as host:
            info = host.fork(_describe_child, ())
            outcome = host.outcome(info.id)
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
    before = _open_fds()
    with _host(_Recorder("a", log), _Recorder("b", log)) as host:
        with mock.patch.object(os, "fork", side_effect=OSError("no processes")):
            try:
                host.fork(lambda: b"x", ())
            except ProcessHostError:
                refused = True
            else:
                refused = False
    return {"refused": refused, "log": _lines(log), "leaked_fds": _open_fds() - before}


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
        fds = _open_fds()
        info = host.fork(gate.wait_then(b"done"), ())
        gate.open()
        # Patched after the fork, so only the parent's reads are interrupted.
        with mock.patch.object(os, "read", flaky), pytest.raises(KeyboardInterrupt):
            host.outcome(info.id)
        outcome = _show(host.outcome(info.id))
        return {"outcome": outcome, "leaked": _open_fds() - fds}


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
        fds = _open_fds()
        for _ in range(_REPEATS):
            info = host.fork(child, ())
            with pytest.raises(KeyboardInterrupt):
                start.open()
                host.outcome(info.id)
            gate.open()
            outcomes.append(_show(host.outcome(info.id)))
        return {"outcomes": outcomes, "leaked": _open_fds() - fds}


def test_a_signal_during_a_wait_interrupts_it_without_losing_the_child(tmp_path: Path) -> None:
    report = run_callable(_signal_during_the_wait, tmp_path, timeout=RUN_TIMEOUT).value

    assert report == {"outcomes": [["D", "done"]] * _REPEATS, "leaked": 0}


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
        fds = _open_fds()
        dropped = host.fork(lambda: b"dropped", ())
        with _interrupt_once_in_waitpid(), pytest.raises(KeyboardInterrupt):
            host.outcome(dropped.id)
        # Detached before it was reaped: a later operation reaps it.
        host.detach(dropped.id)
        os.waitid(os.P_PID, dropped.pid, os.WEXITED | os.WNOWAIT)
        kept = host.fork(lambda: b"kept", ())
        report = {"kept": _show(host.outcome(kept.id)), "reaped": _is_reaped(dropped.pid)}
        report["leaked"] = _open_fds() - fds
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
    real = os.kill

    def kill(pid: int, number: int) -> None:
        if number == signal.SIGKILL and not second:
            second.append(number)
            real(os.getpid(), signal.SIGINT)
        real(pid, number)

    def interrupt() -> None:
        real(os.getpid(), signal.SIGINT)

    second: list[int] = []
    gate = _Gate()
    with _host(_ParentHook(workdir / "log", "hook", interrupt)) as host:
        with _recorded_forks() as pids, mock.patch.object(os, "kill", kill):
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
        fds = _open_fds()
        interrupts = [signal.SIGINT]

        def close(fd: int) -> None:
            if fd == pipe_fd and interrupts:
                os.kill(os.getpid(), interrupts.pop())
            real(fd)

        with mock.patch.object(os, "close", close), pytest.raises(KeyboardInterrupt):
            host.detach(info.id)
        released = fds - _open_fds()
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
        (_record(b"D", 3, b"abc")[:-1], None),
        (_record(b"D", 3, b"abc") + b"x", None),
        (_record(b"D", 3, b"abc")[:5], None),
        (b"", None),
        (_record(b"X", 1, b"a"), None),
    ],
)
def test_outcome_records_are_complete_only_when_framed_exactly(
    data: bytes, expected: Outcome | None
) -> None:
    assert _parse_record(data) == expected


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
