"""The process host: OS-level mechanics of forked children and their opaque outcomes.

Eval-free: the host forks a child that runs a callable returning bytes and transports what
the child delivers; it knows nothing about AgL values. A child never returns into the
caller's stack: every path ends in ``os._exit`` or a re-raised signal.

A child delivers one framed record on a private pipe (tag, length, body), so a truncated
record is detectable; a complete record wins over the exit status. A child closes its pipe
only by exiting, so EOF is followed by a reaping. Waiting drains the pipes of all attached
children, so a child blocked on a full pipe never stalls behind another wait. Terminating
signals are held only around short critical sections (the fork and attach, a chunk read, the
reaping, detach), never across a blocking wait: an interrupt then propagates promptly.

*Participants* extend the fork with other services' state (see :class:`ForkParticipant`).
"""

from __future__ import annotations

import enum
import functools
import os
import select
import signal
import sys
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import FrameType
from typing import ClassVar, NamedTuple, NoReturn
from uuid import uuid4

from agnette.core.cleanup import cleanup_on_failure, run_cleanup_steps
from agnette.core.process import kill_group
from agnette.core.signals import SignalMask, SignalWakeup, held_signals

__all__ = [
    "Delivered",
    "Disposition",
    "Ended",
    "Failed",
    "ForkParticipant",
    "ForkRequest",
    "Outcome",
    "ProcessHost",
    "ProcessHostError",
    "ProcessInfo",
]

_DELIVERED = b"D"
_FAILED = b"F"
# A record is a one-byte tag, an 8-byte big-endian body length, and the body.
_LENGTH_SIZE = 8
_HEADER_SIZE = 1 + _LENGTH_SIZE
_READ_CHUNK = 1 << 16
# A child that fails outside its callable (a hook, the record write) exits with this status.
_UNEXPECTED_EXIT = 70
# Held across a fork and a reaping, so no interrupt runs a handler while the state is torn.
_TERMINATING = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class ProcessHostError(Exception):
    """Misuse of the host (unknown, detached, or foreign process) or a refused fork."""


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """A forked child: the host's handle id and its OS pid."""

    id: str
    pid: int


@dataclass(frozen=True, slots=True)
class Delivered:
    """The child returned *payload*."""

    payload: bytes


@dataclass(frozen=True, slots=True)
class Failed:
    """The child's callable raised a Python ``Exception``."""

    message: str


@dataclass(frozen=True, slots=True)
class Ended:
    """The child ended without a complete record: by exit status or by signal."""

    exit_code: int | None
    signal: int | None


Outcome = Delivered | Failed | Ended


class Disposition(enum.Enum):
    """How a child's outcome ends its teardown: normally, or abnormally."""

    SETTLE = "settle"
    TERMINATE = "terminate"


@dataclass(frozen=True, slots=True)
class ForkRequest:
    """What a fork asks of participants: the agent sessions to carry over."""

    sessions: tuple[str, ...]


class ForkParticipant[S](ABC):
    """A service whose state follows a fork. Hosts register participants in order.

    ``prepare`` runs in the parent before the fork and may raise. If the fork fails, as a
    ``prepare`` or after it, the states of the participants already prepared are passed to
    their ``abort`` (reverse order). After the fork ``parent`` runs in the parent and ``child``
    in the child, in order; every ``parent`` hook runs even if one fails, and the fork then
    fails. A child's ``child_exit`` hooks run in reverse order before it exits, for the
    participants whose ``child`` completed. Only ``prepare`` is required.
    """

    @abstractmethod
    def prepare(self, request: ForkRequest) -> S:
        """Capture what the fork needs from the parent's *S* state; may raise."""

    def abort(self, state: S) -> None:
        """Release what ``prepare`` made, since no child survives the fork.

        May run after ``parent``, if a later step of the fork failed.
        """

    def parent(self, state: S) -> None:
        """Run in the parent after a successful fork."""

    def child(self, state: S) -> None:
        """Run in the child right after the fork."""

    def child_exit(self, disposition: Disposition) -> None:
        """Run in the child as it exits, with the disposition of its outcome."""


class _Prepared(NamedTuple):
    """A participant's hooks bound to the state its ``prepare`` returned."""

    abort: Callable[[], None]
    parent: Callable[[], None]
    child: Callable[[], None]
    child_exit: Callable[[Disposition], None]


class _ChildEnd(NamedTuple):
    """How a child leaves: its exit status, or the signal that it re-raises."""

    exit_code: int
    signal: int | None


def _frame(tag: bytes, body: bytes) -> bytes:
    return tag + len(body).to_bytes(_LENGTH_SIZE, "big") + body


def _parse_record(data: bytes | bytearray) -> Delivered | Failed | None:
    """The record *data* holds, or ``None`` if it is not exactly one complete record."""
    with memoryview(data) as view:
        if len(view) < _HEADER_SIZE:
            return None
        tag = bytes(view[:1])
        body = view[_HEADER_SIZE:]
        if len(body) != int.from_bytes(view[1:_HEADER_SIZE], "big"):
            return None
        if tag == _DELIVERED:
            return Delivered(bytes(body))
        if tag == _FAILED:
            return Failed(str(body, "utf-8", errors="replace"))
        return None


def _classify(data: bytes | bytearray, status: int) -> Outcome:
    """The outcome of a finished child: a complete record wins over its exit status."""
    record = _parse_record(data)
    if record is not None:
        return record
    code = os.waitstatus_to_exitcode(status)
    return Ended(code, None) if code >= 0 else Ended(None, -code)


def _describe(exc: Exception) -> str:
    """*exc* as Python reports it (``Type: message``); its type's name if that fails."""
    try:
        return "".join(traceback.format_exception_only(exc)).rstrip()
    except Exception:
        return type(exc).__qualname__


def _failed_record(exc: Exception) -> bytes:
    """The record reporting *exc*; text that is not valid Unicode is escaped, not lost."""
    return _frame(_FAILED, _describe(exc).encode("utf-8", errors="backslashreplace"))


def _exit_code(status: object) -> int:
    """The exit status a ``SystemExit`` carrying *status* means, as Python's own exit has it."""
    if status is None:
        return 0
    if isinstance(status, int):
        return status
    print(status, file=sys.stderr)
    return 1


def _interrupt_end(exc: KeyboardInterrupt | SystemExit, recorded: int | None) -> _ChildEnd:
    """How a child ends after *exc*: ``SystemExit`` by status, an interrupt by its signal."""
    if isinstance(exc, SystemExit):
        return _ChildEnd(_exit_code(exc.code), None)
    number = recorded or signal.SIGINT
    return _ChildEnd(128 + number, number)


def _leave(exit_code: int, number: int | None) -> NoReturn:
    """Leave the child by re-raising *number*, else (or if it is ignored) by exiting.

    Resetting the disposition runs pending handlers, which end the child by their own signal.
    """
    if number is not None:
        signal.signal(number, signal.SIG_DFL)
        signal.raise_signal(number)
    os._exit(exit_code)


def _write_all(fd: int, data: bytes) -> None:
    """Write *data* to *fd*; a closed reader (a detached process) is not an error."""
    view = memoryview(data)
    try:
        while view:
            view = view[os.write(fd, view) :]
    except BrokenPipeError:
        pass


@dataclass(slots=True)
class _Entry:
    """An attached child: running until its pipe reaches EOF, then holding its outcome.

    ``fd`` is ``-1`` once closed. A child at EOF that is not yet reaped keeps ``result``
    ``None`` until a wait completes it, so an interrupt can strike between the two steps.
    """

    id: str
    pid: int
    fd: int
    buffer: bytearray = field(default_factory=bytearray)
    result: Outcome | None = None


def _close_pipe(entry: _Entry) -> None:
    """Close *entry*'s pipe once; marked first, so an interrupt never leaves a stale number."""
    fd, entry.fd = entry.fd, -1
    if fd >= 0:
        os.close(fd)


def _exit_hooks(entered: Sequence[_Prepared], disposition: Disposition) -> None:
    """Run the ``child_exit`` hooks of *entered* in reverse; every hook runs."""
    run_cleanup_steps(
        [functools.partial(hooks.child_exit, disposition) for hooks in reversed(entered)]
    )


class ProcessHost:
    """Forks children and transports their outcomes, for one host session.

    Not thread-safe. A forked child gets an empty process table: handles of its parent
    are foreign to it. The outcome types are class attributes, so a companion reaches
    them through the host it holds.
    """

    Delivered: ClassVar[type[Delivered]] = Delivered
    Failed: ClassVar[type[Failed]] = Failed
    Ended: ClassVar[type[Ended]] = Ended

    def __init__(self) -> None:
        self._participants: list[Callable[[ForkRequest], _Prepared]] = []
        self._entries: dict[str, _Entry] = {}
        self._detached: list[int] = []
        # In a child: the write end of the pipe it delivers its record on.
        self._outcome_fd: int | None = None
        # In a child: the last terminating signal received, while its interrupt is in flight.
        self._received_signal: int | None = None
        # In a child: set once it is past the last point where an interrupt may unwind it.
        self._leaving = False

    def register[S](self, participant: ForkParticipant[S]) -> None:
        """Add *participant*, after those already registered."""

        def prepare(request: ForkRequest) -> _Prepared:
            state = participant.prepare(request)
            return _Prepared(
                functools.partial(participant.abort, state),
                functools.partial(participant.parent, state),
                functools.partial(participant.child, state),
                participant.child_exit,
            )

        self._participants.append(prepare)

    # -- fork ---------------------------------------------------------------

    def fork(self, child: Callable[[], bytes], sessions: Sequence[str]) -> ProcessInfo:
        """Run *child* in a forked process; *sessions* are passed on to the participants.

        Raises :class:`ProcessHostError` unless this is the only Python thread, if the OS
        refuses the fork, or if a ``parent`` hook fails (the child is then killed and
        reaped). The returned process stays attached until detached.

        Terminating signals are held from the OS fork until the fork is consistent: in the
        parent until the child is attached, in the child until its handlers are installed.
        A signal held in the parent then interrupts the fork, which kills the child. Any
        failure aborts the prepared states once no child is left.
        """
        self._reap_detached()
        if len(sys._current_frames()) != 1:
            raise ProcessHostError("cannot fork: the process has other threads")
        request = ForkRequest(tuple(sessions))
        prepared: list[_Prepared] = []
        with cleanup_on_failure(
            functools.partial(self._abort, prepared), label="fork participant abort"
        ):
            for prepare in self._participants:
                prepared.append(prepare(request))
            with held_signals(_TERMINATING) as previous:
                pid, read_fd, write_fd = self._spawn()
                if pid == 0:
                    # Never returns: the restoring exit of ``held_signals`` is the parent's alone.
                    self._run_child(child, prepared, read_fd, write_fd, previous)
                return self._attach(pid, read_fd, write_fd, prepared, previous)

    @staticmethod
    def _abort(prepared: list[_Prepared]) -> None:
        run_cleanup_steps([hooks.abort for hooks in reversed(prepared)])

    @staticmethod
    def _spawn() -> tuple[int, int, int]:
        """``os.fork`` with a fresh non-inheritable outcome pipe: ``(pid, read, write)``."""
        try:
            read_fd, write_fd = os.pipe()
            try:
                return os.fork(), read_fd, write_fd
            except BaseException:
                os.close(read_fd)
                os.close(write_fd)
                raise
        except OSError as exc:
            raise ProcessHostError(f"cannot fork: {exc}") from exc

    def _attach(
        self,
        pid: int,
        read_fd: int,
        write_fd: int,
        prepared: Sequence[_Prepared],
        previous: SignalMask,
    ) -> ProcessInfo:
        """In the parent after the fork: attach the child, run the ``parent`` hooks.

        Restores the signal mask last, so a held signal interrupts here. Any failure kills
        the child's process group (:func:`kill_group`) and reaps it, as no caller holds its id;
        an ``Exception`` is reported as a :class:`ProcessHostError`.
        """
        entry = _Entry(uuid4().hex, pid, read_fd)
        self._entries[entry.id] = entry
        try:
            os.close(write_fd)
            try:
                os.setpgid(pid, pid)
            except (ProcessLookupError, PermissionError):
                # The child is gone or has moved on; it also sets its own group.
                pass
            run_cleanup_steps([hooks.parent for hooks in prepared])
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        except BaseException as exc:
            with held_signals(_TERMINATING):
                self._destroy(entry)
            if isinstance(exc, Exception):
                raise ProcessHostError(f"cannot fork: {exc}") from exc
            raise
        return ProcessInfo(entry.id, pid)

    def _destroy(self, entry: _Entry) -> None:
        """Kill *entry*'s child and its process group, reap it, and forget it."""
        del self._entries[entry.id]
        os.kill(entry.pid, signal.SIGKILL)
        kill_group(entry.pid)
        _close_pipe(entry)
        os.waitpid(entry.pid, 0)

    # -- child side ---------------------------------------------------------

    def _run_child(
        self,
        child: Callable[[], bytes],
        prepared: list[_Prepared],
        read_fd: int,
        write_fd: int,
        unblocked: SignalMask,
    ) -> NoReturn:
        """Run the child to its end; never returns.

        An interrupt or ``SystemExit`` anywhere ends the child as such; any other error
        prints its traceback and ends it with status 70. A terminating signal that arrives
        once the child is leaving ends it by that signal, without unwinding.
        """
        end = _ChildEnd(_UNEXPECTED_EXIT, None)
        try:
            end = self._child_main(child, prepared, read_fd, write_fd, unblocked)
        except (KeyboardInterrupt, SystemExit) as exc:
            end = _interrupt_end(exc, self._received_signal)
        except BaseException:
            traceback.print_exc()
        finally:
            # First, so that no handler can unwind the child from here on.
            self._leaving = True
            _leave(*end)

    def _child_main(
        self,
        child: Callable[[], bytes],
        prepared: list[_Prepared],
        read_fd: int,
        write_fd: int,
        unblocked: SignalMask,
    ) -> _ChildEnd:
        """Hooks, callable, teardown, record: the child's life. A failing ``child_exit`` after
        a delivered result turns the result into a failure.
        """
        self._received_signal = None
        # The parent's wakeup pipe is not the child's to write to.
        signal.set_wakeup_fd(-1)
        for number in _TERMINATING:
            signal.signal(number, self._on_terminate)
        os.setpgid(0, 0)
        # Signals held since the fork now interrupt this guarded code.
        signal.pthread_sigmask(signal.SIG_SETMASK, unblocked)
        stdin = os.open(os.devnull, os.O_RDONLY)
        os.dup2(stdin, 0)
        os.close(stdin)
        self._enter_child_table(read_fd, write_fd)
        entered: list[_Prepared] = []
        with cleanup_on_failure(
            functools.partial(_exit_hooks, entered, Disposition.TERMINATE), label="child_exit"
        ):
            for hooks in prepared:
                hooks.child()
                entered.append(hooks)
        record: bytes | None
        try:
            record = _frame(_DELIVERED, child())
            end, disposition = _ChildEnd(0, None), Disposition.SETTLE
        except Exception as exc:
            record = _failed_record(exc)
            end, disposition = _ChildEnd(1, None), Disposition.TERMINATE
        except (KeyboardInterrupt, SystemExit) as exc:
            record = None
            end, disposition = _interrupt_end(exc, self._received_signal), Disposition.TERMINATE
        # An interrupt the callable survived is over.
        self._received_signal = None
        try:
            _exit_hooks(entered, disposition)
        except Exception as exc:
            if disposition is Disposition.SETTLE:
                record, end = _failed_record(exc), _ChildEnd(1, None)
        if record is not None:
            _write_all(write_fd, record)
        sys.stdout.flush()
        sys.stderr.flush()
        return end

    def _enter_child_table(self, read_fd: int, write_fd: int) -> None:
        """Drop everything inherited from the parent's process table; own the new pipe."""
        os.close(read_fd)
        if self._outcome_fd is not None:
            os.close(self._outcome_fd)
        self._outcome_fd = write_fd
        for entry in self._entries.values():
            _close_pipe(entry)
        self._entries.clear()
        self._detached.clear()

    def _on_terminate(self, number: int, frame: FrameType | None) -> None:
        if self._leaving:
            _leave(128 + number, number)
        self._received_signal = number
        raise KeyboardInterrupt

    # -- waiting ------------------------------------------------------------

    def settle(self, id: str) -> None:
        """Block until process *id* has finished."""
        self.settle_any([id])

    def settle_any(self, ids: Sequence[str]) -> int:
        """Block until one of *ids* has finished; return the lowest finished index.

        Outcomes are not consumed. Raises :class:`ProcessHostError` for no ids.
        """
        self._reap_detached()
        if not ids:
            raise ProcessHostError("settle_any needs at least one process")
        entries = [self._entry(id) for id in ids]
        self._pump(lambda: any(entry.result is not None for entry in entries))
        return next(index for index, entry in enumerate(entries) if entry.result is not None)

    def outcome(self, id: str) -> Outcome:
        """Block until process *id* has finished, then return its outcome (idempotent)."""
        self.settle(id)
        result = self._entries[id].result
        assert result is not None
        return result

    def detach(self, id: str) -> None:
        """Release process *id*: it is no longer awaited, and a later use of it is an error."""
        self._reap_detached()
        entry = self._entry(id)
        # Held, so that an interrupt cannot leave the child or its pipe unreleased.
        with held_signals(_TERMINATING):
            if entry.result is None:
                self._detached.append(entry.pid)
            _close_pipe(entry)
            del self._entries[id]

    def _entry(self, id: str) -> _Entry:
        try:
            return self._entries[id]
        except KeyError:
            raise ProcessHostError(f"unknown, detached, or foreign process {id!r}") from None

    def _pump(self, done: Callable[[], bool]) -> None:
        """Read the pipes of all running children until *done*; an interrupt propagates.

        A signal wakes the blocking poll (see :class:`SignalWakeup`), so a Python handler
        that raises interrupts the wait at once. The wakeup lasts only for this call, never
        across a fork.
        """
        entries = list(self._entries.values())
        for entry in entries:
            if entry.result is None and entry.fd < 0:
                # At EOF, but an interrupt cut its reaping short.
                self._reap(entry)
        if done():
            return
        running = {entry.fd: entry for entry in entries if entry.result is None}
        poller = select.poll()
        with SignalWakeup() as wakeup:
            wakeup.register(poller)
            for fd in running:
                poller.register(fd, select.POLLIN)
            while not done():
                for fd, _ in poller.poll():
                    if wakeup.consume(fd):
                        continue
                    entry = running[fd]
                    # Held, so that an interrupt cannot drop a chunk already read.
                    with held_signals(_TERMINATING):
                        chunk = os.read(fd, _READ_CHUNK)
                        if chunk:
                            entry.buffer += chunk
                    if not chunk:
                        poller.unregister(fd)
                        del running[fd]
                        self._reap(entry)

    @staticmethod
    def _reap(entry: _Entry) -> None:
        """Reap *entry*'s child at EOF (the child closes its pipe only by exiting).

        The wait for the exit is interruptible and consumes no status. Termination signals
        are held from the reaping until the result is stored, so an interrupt cannot lose
        the status. An interrupt before it leaves the entry completable.
        """
        _close_pipe(entry)
        os.waitid(os.P_PID, entry.pid, os.WEXITED | os.WNOWAIT)
        with held_signals(_TERMINATING):
            _, status = os.waitpid(entry.pid, 0)
            entry.result = _classify(entry.buffer, status)
            entry.buffer.clear()

    def _reap_detached(self) -> None:
        """Reap detached children that have exited; forget those already gone."""
        alive = []
        for pid in self._detached:
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                continue
            if reaped == 0:
                alive.append(pid)
        self._detached = alive
