"""Built-in :class:`~agnette.agl.runtime.processes.ForkParticipant` implementations."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from agnette.agl.runtime.externs import ExternRuntimeState, ForkedEntries
from agnette.agl.runtime.processes import (
    Disposition,
    ForkParticipant,
    ForkRegistration,
    ForkRequest,
    ProcessHost,
    ProcessInfo,
)
from agnette.agl.runtime.sessions import SessionHost, SessionHostError

__all__ = [
    "build_process_host",
    "CompanionStateParticipant",
    "SessionParticipant",
    "StdioFlushParticipant",
]


class StdioFlushParticipant(ForkParticipant[None]):
    """Flushes stdout and stderr before the fork, so the child never repeats buffered output."""

    def prepare(self, request: ForkRequest) -> None:
        sys.stdout.flush()
        sys.stderr.flush()


class CompanionStateParticipant(ForkParticipant[ForkedEntries]):
    """Carries companion state across the fork through its registered fork hooks.

    The child keeps only the hook-made entries and closes them as it exits, honouring the
    run's final ``debug`` setting as read by *debug* at that time.
    """

    def __init__(self, state: ExternRuntimeState, debug: Callable[[], bool]) -> None:
        self._state = state
        self._debug = debug

    def prepare(self, request: ForkRequest) -> ForkedEntries:
        return self._state.prepare_fork()

    def abort(self, state: ForkedEntries) -> None:
        state.abort()

    def child(self, state: ForkedEntries) -> None:
        self._state.enter_forked_child(state)

    def child_exit(self, disposition: Disposition) -> None:
        self._state.close_all(debug=self._debug())


class SessionParticipant(ForkParticipant[None]):
    """Makes the sessions the child inherited inert, since sessions belong to their opener.

    Carrying sessions into a child is not supported: a request for any is refused.
    """

    def __init__(self, sessions: SessionHost) -> None:
        self._sessions = sessions

    def prepare(self, request: ForkRequest) -> None:
        if request.sessions:
            raise SessionHostError("forking with sessions is not supported", "fork")

    def child(self, state: None) -> None:
        self._sessions.enter_forked_child()

    def child_exit(self, disposition: Disposition) -> None:
        self._sessions.close_all()


def build_process_host(
    *,
    state: ExternRuntimeState,
    sessions: SessionHost,
    debug: Callable[[], bool],
    reporter: Callable[[ProcessInfo, str], None],
    extra: Sequence[ForkRegistration] = (),
) -> ProcessHost:
    """A process host with the participants every host session needs, registered in order.

    A child's ``child_exit`` hooks run in reverse registration order, after the host has ended
    the child's own processes: sessions close first, then companion state, as in a run's end.
    Stdio goes last so it is flushed right before ``os.fork``, after every other participant's
    ``prepare`` (a fork hook's output included); it has no exit hook, so it does not affect
    the order of the others. The host owner's *extra* participants come first: their ``child``
    hooks run before the built-in ones, and their ``child_exit`` hooks after them.
    """
    host = ProcessHost(reporter=reporter)
    for registration in extra:
        registration.register_on(host)
    host.register(CompanionStateParticipant(state, debug))
    host.register(SessionParticipant(sessions))
    host.register(StdioFlushParticipant())
    return host
