"""Built-in :class:`~agnette.agl.runtime.processes.ForkParticipant` implementations."""

from __future__ import annotations

import sys
from collections.abc import Callable

from agnette.agl.runtime.externs import ExternRuntimeState, ForkedEntries
from agnette.agl.runtime.processes import Disposition, ForkParticipant, ForkRequest

__all__ = ["CompanionStateParticipant", "StdioFlushParticipant"]


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
