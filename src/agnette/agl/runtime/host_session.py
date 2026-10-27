"""Shared host services and their ordered teardown for runs and REPL sessions."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from typing import Protocol

from agnette.agl.runtime.externs import ExternRuntimeState
from agnette.agl.runtime.fork_participants import build_process_host
from agnette.agl.runtime.processes import Disposition, ProcessInfo
from agnette.agl.runtime.sessions import SessionHost
from agnette.core.cleanup import cleanup_on_failure, run_cleanup_steps


class ServingRun(Protocol):
    """The active run's settings and process reporting boundary."""

    @property
    def debug(self) -> bool: ...

    def report_unconsumed(self, info: ProcessInfo, problem: str) -> None: ...


class HostSession:
    """Own processes, agent sessions, and companion state with one teardown order."""

    def __init__(self, sessions: SessionHost, *, fallback_debug: Callable[[], bool]) -> None:
        self.sessions = sessions
        self.state = ExternRuntimeState()
        self._fallback_debug = fallback_debug
        self._run: ServingRun | None = None
        self.processes = build_process_host(
            state=self.state, sessions=sessions, debug=self.debug, reporter=self._report_unconsumed
        )

    def debug(self) -> bool:
        """The active run's debug setting, else the owner's persisted setting."""
        return self._run.debug if self._run is not None else self._fallback_debug()

    def _report_unconsumed(self, info: ProcessInfo, problem: str) -> None:
        assert self._run is not None
        self._run.report_unconsumed(info, problem)

    @contextmanager
    def serving(self, run: ServingRun) -> Iterator[None]:
        """Route settings and reports to *run* while it executes."""
        previous = self._run
        self._run = run
        try:
            yield
        finally:
            self._run = previous

    def close(self, disposition: Disposition) -> None:
        """End processes, then close sessions and state; every step runs."""
        run_cleanup_steps(
            [
                partial(self.processes.end, disposition),
                self.sessions.close_all,
                lambda: self.state.close_all(debug=self.debug()),
            ]
        )

    @contextmanager
    def owned(self) -> Iterator[None]:
        """Settle on success; terminate on failure, preserving the primary exception."""
        with cleanup_on_failure(
            partial(self.close, Disposition.TERMINATE), label="host session cleanup"
        ):
            yield
        self.close(Disposition.SETTLE)
