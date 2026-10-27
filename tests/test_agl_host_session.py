"""Host-session ownership, teardown, and active-run routing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agnette.agl.runtime.host_session import HostSession
from agnette.agl.runtime.processes import Delivered, Disposition, ProcessInfo
from agnette.agl.runtime.sessions import AgentDispatcherSessionHost
from agnette.core.cleanup import notes_of
from tests.agl.isolated_runner import run_callable


@pytest.mark.parametrize("failure", [None, "processes", "sessions", "state"])
def test_close_runs_every_service_in_order(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    host = HostSession(AgentDispatcherSessionHost(None), fallback_debug=lambda: False)
    events: list[str] = []
    error = RuntimeError("cleanup")

    def close(name: str) -> None:
        events.append(name)
        if name == failure:
            raise error

    monkeypatch.setattr(host.processes, "end", lambda disposition: close("processes"))
    monkeypatch.setattr(host.sessions, "close_all", lambda: close("sessions"))
    host.state.get_or_create("value", object, close=lambda value: close("state"))
    if failure is None:
        host.close(Disposition.SETTLE)
    else:
        with pytest.raises(RuntimeError) as caught:
            host.close(Disposition.SETTLE)
        assert caught.value is error
    assert events == ["processes", "sessions", "state"]


@pytest.mark.parametrize("failed", [False, True])
def test_owned_selects_disposition_and_preserves_primary_error(
    monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    host = HostSession(AgentDispatcherSessionHost(None), fallback_debug=lambda: False)
    dispositions: list[Disposition] = []
    primary = ValueError("run")

    def end(disposition: Disposition) -> None:
        dispositions.append(disposition)
        if failed:
            raise RuntimeError("cleanup")

    monkeypatch.setattr(host.processes, "end", end)
    if failed:
        with pytest.raises(ValueError) as caught, host.owned():
            raise primary
        assert caught.value is primary
        assert notes_of(primary)
    else:
        with host.owned():
            pass
    assert dispositions == [Disposition.TERMINATE if failed else Disposition.SETTLE]


class _Run:
    def __init__(self, debug: bool) -> None:
        self.debug = debug
        self.reports: list[tuple[ProcessInfo, str]] = []

    def report_unconsumed(self, info: ProcessInfo, problem: str) -> None:
        self.reports.append((info, problem))


def _serving_routes(workdir: Path) -> dict[str, Any]:
    host = HostSession(AgentDispatcherSessionHost(None), fallback_debug=lambda: False)
    outer, inner = _Run(True), _Run(False)
    assert not host.debug()
    with host.serving(outer):
        assert host.debug()
        with pytest.raises(ValueError), host.serving(inner):
            assert not host.debug()
            host.processes.fork(lambda: Delivered(b"", failure="inner"), ())
            host.processes.end(Disposition.SETTLE)
            raise ValueError("restore")
        assert host.debug()
        host.processes.fork(lambda: Delivered(b"", failure="outer"), ())
        host.processes.end(Disposition.SETTLE)
    assert not host.debug()
    host.close(Disposition.TERMINATE)
    return {"outer": [p for _, p in outer.reports], "inner": [p for _, p in inner.reports]}


def test_serving_routes_reports_and_restores_debug_after_exit(tmp_path: Path) -> None:
    assert run_callable(_serving_routes, tmp_path).value == {
        "outer": ["failed: outer"],
        "inner": ["failed: inner"],
    }


def test_owned_cleanup_honors_the_final_active_debug_setting() -> None:
    host = HostSession(AgentDispatcherSessionHost(None), fallback_debug=lambda: False)
    run = _Run(False)
    closed: list[object] = []
    host.state.get_or_create("debug", object, close=closed.append, keep_in_debug=True)
    with host.serving(run), host.owned():
        run.debug = True
    assert not closed


def test_owned_propagates_cleanup_failure_after_a_successful_body() -> None:
    host = HostSession(AgentDispatcherSessionHost(None), fallback_debug=lambda: False)
    error = RuntimeError("cleanup")

    def fail(value: object) -> None:
        raise error

    host.state.get_or_create("value", object, close=fail)
    with pytest.raises(RuntimeError) as caught, host.owned():
        pass
    assert caught.value is error
