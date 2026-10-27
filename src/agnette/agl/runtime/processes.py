"""The process host: OS-level mechanics of forked children and their opaque outcomes.

Eval-free: the host forks a child that runs a callable returning bytes and transports what
the child delivers; it knows nothing about AgL values. A child never returns into the
caller's stack: every path ends in ``os._exit`` or a re-raised signal.

A child delivers one framed record on a private pipe (tag, length, body), so a truncated
record is detectable; a complete record wins over the exit status. A child closes its pipe
only by exiting, so EOF is followed by a reaping. Waiting drains the pipes of all attached
children, so a child blocked on a full pipe never stalls behind another wait. Terminating
signals are held only around short critical sections (the fork and attach, a chunk read, the
reaping, detach, the final kill), never across a blocking wait: an interrupt then propagates
promptly.

*Lifetime.* Terminating processes sends SIGTERM to each child and its group, keeps draining
and reaping until one shared deadline, then SIGKILLs the stragglers; an interrupt meanwhile
SIGKILLs at once. Process groups remain targets after their leaders are reaped.
:meth:`ProcessHost.scope` terminates the processes forked inside it when
left by an exception, and :meth:`ProcessHost.end` settles or terminates all of them. A child
tears down the same way before it exits, with half its parent's grace period, so it ends its
own processes within the deadline its parent gives it. Only a SIGKILL that arrives at once (a
second interrupt, or after the whole grace period) can orphan a child's descendants.

*Participants* extend the fork with other services' state (see :class:`ForkParticipant`).
"""

from __future__ import annotations

import enum
import functools
import os
import select
import signal
import sys
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from types import FrameType
from typing import ClassVar, NamedTuple, NoReturn
from uuid import uuid4

from agnette.agl.runtime.process_records import (
    Delivered,
    Ended,
    Failed,
    Outcome,
    classify,
    delivered_record,
    failed_record,
)
from agnette.core.cleanup import cleanup_on_failure, run_cleanup_steps
from agnette.core.fs import write_all
from agnette.core.process import kill_group
from agnette.core.signals import SignalMask, SignalWakeup, held_signals

__all__ = [
    "ChildResult",
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
    "TERMINATION_GRACE",
]

_READ_CHUNK = 1 << 16
# A child that fails outside its callable (a hook, the record write) exits with this status.
_UNEXPECTED_EXIT = 70
# Held across a fork and a reaping, so no interrupt runs a handler while the state is torn.
_TERMINATING = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# Seconds between SIGTERM and SIGKILL when terminating processes.
TERMINATION_GRACE = 10.0


class ProcessHostError(Exception):
    """Misuse of the host (unknown, detached, or foreign process) or a refused fork."""


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """A forked child: the host's handle id and its OS pid."""

    id: str
    pid: int


# What a child callable returns: the payload, or a payload marked as a failure.
type ChildResult = bytes | Delivered


class Disposition(enum.Enum):
    """How a session of processes ends: ``SETTLE`` awaits them, ``TERMINATE`` terminates them.

    A child's teardown uses ``SETTLE`` after a normal outcome, else ``TERMINATE``.
    """

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


@dataclass(slots=True)
class _Entry:
    """An attached child: running until its pipe reaches EOF, then holding its outcome.

    ``fd`` is ``-1`` once closed. A child at EOF that is not yet reaped keeps ``result``
    ``None`` until a wait completes it, so an interrupt can strike between the two steps.
    ``consumed``: the outcome was returned or reported, or the process was terminated by
    :meth:`ProcessHost.terminate`. ``group_pending`` tracks group cleanup independently
    of the outcome; it clears once the group is gone or has received SIGKILL.
    """

    id: str
    pid: int
    fd: int
    buffer: bytearray = field(default_factory=bytearray)
    result: Outcome | None = None
    consumed: bool = False
    decoded: object = None
    has_decoded: bool = False
    group_pending: bool = True

    @property
    def done(self) -> bool:
        return self.result is not None or self.has_decoded


def _signal(entry: _Entry, number: int = signal.SIGKILL) -> None:
    """Send *number* once to *entry*'s unreaped child and to its process group.

    The group reaches the child while it leads it, and its descendants; the pid alone reaches a
    child that left its group (or has not joined it yet).
    """
    if not entry.done:
        with suppress(ProcessLookupError):
            if os.getpgid(entry.pid) != entry.pid:
                os.kill(entry.pid, number)
    if entry.group_pending:
        entry.group_pending = kill_group(entry.pid, number) and number != signal.SIGKILL


def _group_exists(entry: _Entry) -> bool:
    """Check a pending group; never revisit a group once it has disappeared."""
    if entry.group_pending:
        entry.group_pending = kill_group(entry.pid, 0)
    return entry.group_pending


def _close_pipe(entry: _Entry) -> None:
    """Close *entry*'s pipe once; marked first, so an interrupt never leaves a stale number."""
    fd, entry.fd = entry.fd, -1
    if fd >= 0:
        os.close(fd)


def _problem(outcome: Outcome) -> str | None:
    """A one-line account of *outcome* if it is not a clean delivery, else ``None``."""
    if not isinstance(outcome, Delivered):
        text = outcome.describe()
    elif outcome.failure is None:
        return None
    else:
        text = f"failed: {outcome.failure}"
    return " ".join(text.split())


def _exit_hooks(entered: Sequence[_Prepared], disposition: Disposition) -> None:
    """Run the ``child_exit`` hooks of *entered* in reverse; every hook runs."""
    run_cleanup_steps(
        [functools.partial(hooks.child_exit, disposition) for hooks in reversed(entered)]
    )


class ProcessHost:
    """Forks children and transports their outcomes, for one host session.

    Not thread-safe. A forked child gets an empty process table: handles of its parent
    are foreign to it. The outcome types are class attributes, so a companion reaches
    them through the host it holds. *grace* is the seconds between SIGTERM and SIGKILL when
    terminating processes (halved in each forked child). *reporter*, if given, is called with
    the one-line problem text of each outcome :meth:`end` reports, after its stderr warning.
    """

    Delivered: ClassVar[type[Delivered]] = Delivered
    Failed: ClassVar[type[Failed]] = Failed
    Ended: ClassVar[type[Ended]] = Ended

    def __init__(
        self,
        *,
        grace: float = TERMINATION_GRACE,
        reporter: Callable[[ProcessInfo, str], None] | None = None,
    ) -> None:
        self._grace = grace
        self._participants: list[Callable[[ForkRequest], _Prepared]] = []
        self._entries: dict[str, _Entry] = {}
        self._detached: list[int] = []
        self._reporter = reporter
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

    def fork(self, child: Callable[[], ChildResult], sessions: Sequence[str]) -> ProcessInfo:
        """Run *child* in a forked process; *sessions* are passed on to the participants.

        *child* returns the payload, or a :class:`Delivered` marking it as a failure.

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
        _signal(entry)
        _close_pipe(entry)
        os.waitpid(entry.pid, 0)

    # -- child side ---------------------------------------------------------

    def _run_child(
        self,
        child: Callable[[], ChildResult],
        prepared: list[_Prepared],
        read_fd: int,
        write_fd: int,
        unblocked: SignalMask,
    ) -> NoReturn:
        """Run the child to its end; never returns.

        An interrupt or ``SystemExit`` anywhere ends the child as such; any other error
        prints its traceback and ends it with status 70. Processes still attached then are
        terminated, so none outlives an abnormally ended child. A terminating signal that
        arrives once the child is leaving ends it by that signal, without unwinding.
        """
        end = _ChildEnd(_UNEXPECTED_EXIT, None)
        try:
            end = self._child_main(child, prepared, read_fd, write_fd, unblocked)
        except (KeyboardInterrupt, SystemExit) as exc:
            end = _interrupt_end(exc, self._received_signal)
        except BaseException:
            traceback.print_exc()
        finally:
            try:
                # A no-op after a teardown that completed: every outcome is then known.
                self.end(Disposition.TERMINATE)
            except (KeyboardInterrupt, SystemExit) as exc:
                end = _interrupt_end(exc, self._received_signal)
            finally:
                # First, so that no handler can unwind the child from here on.
                self._leaving = True
                _leave(*end)

    def _child_main(
        self,
        child: Callable[[], ChildResult],
        prepared: list[_Prepared],
        read_fd: int,
        write_fd: int,
        unblocked: SignalMask,
    ) -> _ChildEnd:
        """Hooks, callable, teardown, record: the child's life.

        The teardown ends the child's own processes (:meth:`end`: awaited after a normal
        outcome, else terminated), then runs the ``child_exit`` hooks. A failure of either
        after a delivered result turns the result into a failure.
        """
        # First, while terminating signals are held: an interrupt of the child must never
        # find its parent's processes in the table.
        self._enter_child_table(read_fd, write_fd)
        self._received_signal = None
        # The parent's wakeup pipe is not the child's to write to.
        signal.set_wakeup_fd(-1)
        for number in _TERMINATING:
            signal.signal(number, self._on_terminate)
        os.setpgid(0, 0)
        # Signals held since the fork now interrupt this guarded code.
        signal.pthread_sigmask(signal.SIG_SETMASK, unblocked)
        inherited_stdin = sys.stdin
        sys.stdin = open(os.devnull, "r")
        if inherited_stdin is not None:
            inherited_stdin.close()
        stdin = os.open(os.devnull, os.O_RDONLY)
        if stdin != 0:
            os.dup2(stdin, 0)
            os.close(stdin)
        entered: list[_Prepared] = []
        with cleanup_on_failure(
            functools.partial(_exit_hooks, entered, Disposition.TERMINATE), label="child_exit"
        ):
            for hooks in prepared:
                hooks.child()
                entered.append(hooks)
        record: tuple[bytes, ...] | None
        try:
            returned = child()
            delivered = returned if isinstance(returned, Delivered) else Delivered(returned)
            record = delivered_record(delivered)
            end = _ChildEnd(0, None)
            disposition = Disposition.SETTLE if delivered.failure is None else Disposition.TERMINATE
        except Exception as exc:
            record = failed_record(exc)
            end, disposition = _ChildEnd(1, None), Disposition.TERMINATE
        except (KeyboardInterrupt, SystemExit) as exc:
            record = None
            end, disposition = _interrupt_end(exc, self._received_signal), Disposition.TERMINATE
        except BaseException:
            traceback.print_exc()
            record = None
            end, disposition = _ChildEnd(_UNEXPECTED_EXIT, None), Disposition.TERMINATE
        # An interrupt the callable survived is over.
        self._received_signal = None
        try:
            self._teardown(entered, disposition)
        except Exception as exc:
            if disposition is Disposition.SETTLE:
                record, end = failed_record(exc), _ChildEnd(1, None)
        if record is not None:
            # A closed reader (a detached process) is not an error.
            with suppress(BrokenPipeError):
                write_all(write_fd, *record)
        sys.stdout.flush()
        sys.stderr.flush()
        return end

    def _teardown(self, entered: Sequence[_Prepared], disposition: Disposition) -> None:
        """End the child's processes as *disposition*, then run the ``child_exit`` hooks.

        If the ending fails, the hooks run with ``TERMINATE``, as the child ends abnormally.
        """
        with cleanup_on_failure(
            functools.partial(_exit_hooks, entered, Disposition.TERMINATE), label="child_exit"
        ):
            self.end(disposition)
        _exit_hooks(entered, disposition)

    def _enter_child_table(self, read_fd: int, write_fd: int) -> None:
        """Drop everything inherited from the parent's process table; own the new pipe.

        The grace period halves, so the child terminates its own processes within the
        deadline its parent gives it.
        """
        self._grace /= 2
        inherited = list(self._entries.values())
        self._entries.clear()
        self._detached.clear()
        for entry in inherited:
            _close_pipe(entry)
        os.close(read_fd)
        if self._outcome_fd is not None:
            os.close(self._outcome_fd)
        self._outcome_fd = write_fd

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
        self._pump(lambda: any(entry.done for entry in entries))
        return next(index for index, entry in enumerate(entries) if entry.done)

    def outcome(self, id: str) -> Outcome:
        """Block until process *id* has finished, then return its outcome (idempotent).

        Consumes the outcome: :meth:`end` does not report it. Raises
        :class:`ProcessHostError` after :meth:`decoded` has replaced the raw outcome.
        """
        self.settle(id)
        entry = self._entries[id]
        if entry.has_decoded:
            raise ProcessHostError(f"process {id!r} outcome has been decoded")
        assert entry.result is not None
        entry.consumed = True
        return entry.result

    def decoded(self, id: str, decode: Callable[[Outcome], object]) -> object:
        """Decode once, replacing the raw outcome; a failed decode can be retried."""
        entry = self._entry(id)
        if not entry.has_decoded:
            result = decode(self.outcome(id))
            entry.decoded = result
            entry.has_decoded = True
            entry.result = None
        return entry.decoded

    def detach(self, id: str) -> None:
        """Release process *id*: it is no longer awaited, and a later use of it is an error."""
        self._reap_detached()
        entry = self._entry(id)
        # Held, so that an interrupt cannot leave the child or its pipe unreleased.
        with held_signals(_TERMINATING):
            if not entry.done:
                self._detached.append(entry.pid)
            _close_pipe(entry)
            del self._entries[id]

    # -- termination, scopes, session end -----------------------------------

    def terminate(self, *ids: str) -> None:
        """Terminate processes *ids* under one shared deadline and wait until they have ended.

        Finished children are left alone; surviving group members are terminated.
        A process ended before it delivered reports
        ``Ended(signal=SIGTERM)``, or ``SIGKILL`` if it outlived the grace period. The outcomes
        count as consumed, so :meth:`end` does not report them.
        """
        self._reap_detached()
        entries = [self._entry(id) for id in ids]
        self._terminate(entries)
        for entry in entries:
            entry.consumed = True

    @contextmanager
    def scope(self) -> Iterator[None]:
        """Terminate the processes forked inside the block if it fails.

        Leaving by an exception (any ``BaseException``) terminates those of them still
        attached, those of nested scopes included, then re-raises. Leaving normally does
        nothing; the terminated outcomes stay unconsumed.
        """
        before = set(self._entries)
        with self._terminating_on_error(
            lambda: [e for id, e in self._entries.items() if id not in before]
        ):
            yield

    def end(self, disposition: Disposition) -> None:
        """End the session of the attached processes; detached ones are only reaped.

        ``SETTLE`` waits for all of them, then reports each unconsumed outcome that is not a
        clean delivery (a failure, a raised exception, an early end): a one-line warning on
        stderr, then the reporter. An interrupt meanwhile terminates the rest and re-raises.
        Both dispositions terminate surviving group members after their leaders finish.
        ``TERMINATE`` terminates all of them, reporting nothing.
        """
        try:
            if disposition is Disposition.TERMINATE:
                self._terminate(list(self._entries.values()))
            else:
                everything = list(self._entries.values())
                with self._terminating_on_error(lambda: everything):
                    self._pump(self._all_done(everything))
                    self._terminate(everything)
                run_cleanup_steps([functools.partial(self._report, e) for e in everything])
        finally:
            self._reap_detached()

    def _report(self, entry: _Entry) -> None:
        """Warn about *entry*'s outcome once, unless it was consumed or is a clean delivery."""
        if entry.consumed:
            return
        entry.consumed = True
        assert entry.result is not None
        text = _problem(entry.result)
        if text is not None:
            sys.stderr.write(f"warning: process {entry.pid} {text}\n")
            if self._reporter is not None:
                self._reporter(ProcessInfo(entry.id, entry.pid), text)

    @contextmanager
    def _terminating_on_error(self, targets: Callable[[], Sequence[_Entry]]) -> Iterator[None]:
        """Terminate *targets* (taken when needed) if the block raises, then re-raise."""
        try:
            yield
        except BaseException:
            self._terminate(targets())
            raise

    @staticmethod
    def _all_done(entries: Sequence[_Entry]) -> Callable[[], bool]:
        return lambda: all(entry.done for entry in entries)

    def _terminate(self, entries: Sequence[_Entry]) -> None:
        """Terminate unfinished children and surviving groups under one shared deadline.

        SIGTERM goes to them and their groups; the pipes of all attached processes are drained and
        finished ones reaped until the grace period is over, then SIGKILL goes to the
        groups of the stragglers. Any exception meanwhile (an interrupt) skips the wait:
        the remaining ones are killed and reaped, and it propagates.
        """
        targets = [entry for entry in entries if not entry.done or _group_exists(entry)]

        def done() -> bool:
            return all(entry.done and not _group_exists(entry) for entry in targets)

        try:
            for entry in targets:
                _signal(entry, signal.SIGTERM)
            self._pump(done, time.monotonic() + self._grace)
        except BaseException:
            self._kill(targets)
            raise
        if not done():
            self._kill(targets)

    def _kill(self, targets: Sequence[_Entry]) -> None:
        """SIGKILL pending groups and unreaped children, then reap the children.

        Signals are held: SIGKILL ends the wait, and a pending interrupt then propagates.
        """
        with held_signals(_TERMINATING):
            for entry in targets:
                _signal(entry)
            self._pump(self._all_done(targets))

    def _entry(self, id: str) -> _Entry:
        try:
            return self._entries[id]
        except KeyError:
            raise ProcessHostError(f"unknown, detached, or foreign process {id!r}") from None

    def _pump(self, done: Callable[[], bool], deadline: float | None = None) -> None:
        """Read the pipes of all running children until *done*; an interrupt propagates.

        With a *deadline* (``time.monotonic`` seconds), it also returns when that has passed.

        A signal wakes the blocking poll (see :class:`SignalWakeup`), so a Python handler
        that raises interrupts the wait at once. The wakeup lasts only for this call, never
        across a fork.
        """
        entries = list(self._entries.values())
        for entry in entries:
            if not entry.done and entry.fd < 0:
                # At EOF, but an interrupt cut its reaping short.
                self._reap(entry)
        if done():
            return
        running = {entry.fd: entry for entry in entries if not entry.done}
        poller = select.poll()
        for fd in running:
            poller.register(fd, select.POLLIN)
        with SignalWakeup() as wakeup:
            while not done():
                ready = wakeup.poll(poller, deadline)
                if ready is None:
                    return
                for fd in ready:
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
            entry.result = classify(entry.buffer, status)
            entry.buffer.clear()
            _group_exists(entry)

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
