"""Unit tests for host-managed agent sessions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

import pytest

from agnette.agent.session import create_agl_session_host
from agnette.agent.session.protocol import (
    PortableBackend,
    PortableSession,
    PortableSet,
    SessionAskError,
    SessionAskRequest,
    SessionAskResponse,
    SessionBackend,
    SessionHostError,
    SessionOpenRequest,
    SessionOperation,
    SessionOperations,
    SessionStats,
    unsupported_operation,
)
from agnette.agent.session.service import AglSessionHost, SessionService
from agnette.agent.spec import AgentCommand, AgentPi, PermissionMode, SessionTransport
from agnette.agent.transport import AgentCallInfo, AgentOutputPhase
from agnette.agl.runtime.request import AgentRequest
from agnette.agl.runtime.sessions import SessionAskError as AglSessionAskError
from agnette.agl.runtime.sessions import SessionHostError as AglSessionHostError
from agnette.sandbox.request import SandboxLimits
from tests._agl_helpers import hermetic_get_sandbox_context

ALL_OPERATIONS = frozenset(SessionOperation)


@dataclass
class FakeBackend:
    """In-memory backend used to observe the session service's behavior."""

    supported: frozenset[SessionOperation] = ALL_OPERATIONS
    continues_conversation: bool = True
    response: SessionAskResponse = field(
        default_factory=lambda: SessionAskResponse(content="answer")
    )
    ask_error: Exception | None = None
    fork_result: SessionBackend | None = None
    portable_result: PortableSession | None = None
    portable_error: Exception | None = None
    portable_calls: int = 0
    open_requests: list[SessionOpenRequest] = field(default_factory=list)
    ask_requests: list[SessionAskRequest] = field(default_factory=list)
    compact_requests: list[str] = field(default_factory=list)
    reset_calls: int = 0
    names: list[str] = field(default_factory=list)
    close_calls: int = 0
    close_error: Exception | None = None
    release_calls: int = 0

    @property
    def operations(self) -> SessionOperations:
        def native[T](operation: SessionOperation, method: T) -> T | None:
            return method if operation in self.supported else None

        return SessionOperations(
            compact=native(SessionOperation.COMPACT, self.compact),
            fork=native(SessionOperation.FORK, self.fork),
            set_name=native(SessionOperation.SET_NAME, self.set_name),
            stats=native(SessionOperation.STATS, self.stats),
        )

    def ask(self, request: SessionAskRequest) -> SessionAskResponse:
        self.ask_requests.append(request)
        if self.ask_error is not None:
            raise self.ask_error
        return self.response

    def compact(self, instructions: str) -> None:
        self.compact_requests.append(instructions)

    def reset(self) -> None:
        self.reset_calls += 1

    def fork(self) -> SessionBackend:
        if self.fork_result is None:
            raise AssertionError("test backend must be given a fork result")
        return self.fork_result

    def portable(self) -> PortableSession:
        self.portable_calls += 1
        if SessionOperation.FORK not in self.supported:
            raise unsupported_operation(SessionOperation.FORK)
        if self.portable_error is not None:
            raise self.portable_error
        return self.portable_result or PortableBackend(FakeBackend(self.supported))

    def set_name(self, name: str) -> None:
        self.names.append(name)

    def stats(self) -> SessionStats:
        return SessionStats(
            input_tokens=3,
            output_tokens=5,
            cost=Decimal("0.12"),
            context_percent=Decimal("4.5"),
        )

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error

    def release_inherited(self) -> None:
        self.release_calls += 1


class FakeBackendFactory:
    def __init__(self, supported: frozenset[SessionOperation] = ALL_OPERATIONS) -> None:
        self.supported = supported
        self.backends: list[FakeBackend] = []

    def __call__(self, request: SessionOpenRequest) -> SessionBackend:
        backend = FakeBackend(self.supported, open_requests=[request])
        self.backends.append(backend)
        return backend


def _service(
    factory: FakeBackendFactory | None = None,
) -> tuple[SessionService, FakeBackendFactory]:
    actual_factory = factory or FakeBackendFactory()
    return SessionService(actual_factory), actual_factory


def _assert_error(operation: str, action: object) -> None:
    if not callable(action):
        raise AssertionError("action must be callable")
    with pytest.raises(SessionHostError) as raised:
        action()
    assert raised.value.operation == operation


def test_agl_session_host_adapts_all_lifecycle_operations() -> None:
    parent = FakeBackend()
    child = FakeBackend()
    parent.fork_result = child
    created: list[SessionOpenRequest] = []

    def backend_for(request: SessionOpenRequest) -> SessionBackend:
        created.append(request)
        return parent

    host = AglSessionHost(SessionService(backend_for))
    agent = AgentCommand(command="worker")
    handle = host.open(agent, "Cli", name="primary", env={})
    assert host.ask(handle, "hello") == "answer"
    host.compact(handle, "retain")
    host.reset(handle)
    child_handle = host.fork(handle)
    host.set_name(child_handle, "child")
    stats = host.stats(child_handle)
    snapshot = host.snapshot(child_handle)
    host.close(handle)
    host.close_all()

    assert created[0].transport == SessionTransport.CLI
    assert parent.ask_requests == [SessionAskRequest("hello")]
    assert parent.compact_requests == ["retain"]
    assert parent.reset_calls == 1
    assert child.names == ["child"]
    assert stats.input_tokens == 3
    assert snapshot.agent == agent
    assert snapshot.transport == "Cli"
    with pytest.raises(AglSessionHostError):
        host.snapshot("unknown")
    assert parent.close_calls == 1
    assert child.close_calls == 1


def test_production_session_host_selects_and_rejects_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agnette.agent.session import rpc

    spawned: list[list[str]] = []

    def spawn(agent: AgentPi, command: list[str], operation: str, *_args: object) -> object:
        spawned.append(command)
        return object()

    monkeypatch.setattr(rpc, "_spawn", spawn)
    monkeypatch.setattr(rpc, "_terminate", lambda _child: None)
    host = create_agl_session_host(
        idle_timeout=1.0, get_sandbox_context=hermetic_get_sandbox_context()
    )
    pi = AgentPi(provider="provider", model="model", thinking="think")
    handle = host.open(pi, "Rpc", env={})
    host.close(handle)
    assert len(spawned) == 1
    command = AgentCommand(command="echo %{SESSION_ID}")
    command_handle = host.open(command, "Cli", env={})
    host.close(command_handle)
    with pytest.raises(AglSessionHostError):
        host.open(command, "Rpc", env={})


def test_agl_session_host_rejects_closed_and_unknown_sessions() -> None:
    """Agent decoding now happens in the evaluator; the host only sees specs."""
    backend = FakeBackend(frozenset())
    host = AglSessionHost(SessionService(lambda _request: backend))
    agent = AgentCommand(command="worker")
    handle = host.open(agent, "Cli", env={})
    assert host.ask(handle, "hello") == "answer"
    host.close(handle)
    with pytest.raises(AglSessionHostError):
        host.ask(handle, "closed")
    with pytest.raises(AglSessionHostError) as unknown:
        host.ask("unknown", "missing")
    assert unknown.value.operation == "ask"


def test_agl_session_host_preserves_success_response_metadata_and_call_info() -> None:
    backend = FakeBackend(
        frozenset(),
        response=SessionAskResponse(
            content="answer",
            metadata={"elapsed": 1.5, "provider": "pi"},
            call_info=AgentCallInfo(
                argv=["pi", "--rpc"],
                prompt_via_stdin=True,
                elapsed=1.5,
                exit_code=0,
                sandboxed=False,
                permission_mode="none",
            ),
        ),
    )
    host = AglSessionHost(SessionService(lambda _request: backend))
    agent = AgentPi(provider="provider", model="model", thinking="think")
    handle = host.open(agent, "Rpc", env={})

    def callback(_phase: AgentOutputPhase, _text: str, **_metadata: object) -> None:
        return None

    response = host.ask_request(
        handle, AgentRequest(agent=agent, prompt="hello", env={}, output_callback=callback)
    )

    assert response.content == "answer"
    assert response.metadata == {"elapsed": 1.5, "provider": "pi"}
    assert response.call_info is not None
    assert response.call_info.to_trace() == {
        "argv": ["pi", "--rpc"],
        "prompt_via_stdin": True,
        "elapsed": 1.5,
        "exit_code": 0,
        "sandboxed": False,
        "permission_mode": "none",
    }
    assert backend.ask_requests[0].output_callback is callback


def test_agl_session_host_translates_host_and_ask_failures() -> None:
    backend = FakeBackend(frozenset())
    host = AglSessionHost(SessionService(lambda _request: backend))
    agent = AgentCommand(command="worker")
    handle = host.default(agent, "Cli", env={})
    assert host.default(agent, "Cli", env={}) == handle

    backend.ask_error = SessionAskError(
        cause="failed",
        exit_code=2,
        stderr_tail="tail",
        elapsed=1.5,
        call_info=AgentCallInfo(
            argv=("worker",),
            prompt_via_stdin=True,
            elapsed=1.5,
            exit_code=2,
            sandboxed=False,
            permission_mode="none",
        ),
    )
    with pytest.raises(AglSessionAskError) as ask_error:
        host.ask(handle, "hello")
    assert ask_error.value.cause == "failed"

    host.close(handle)
    with pytest.raises(AglSessionHostError) as host_error:
        host.reset(handle)
    assert host_error.value.operation == "reset"


def test_open_ask_and_close_manage_a_live_session() -> None:
    service, factory = _service()
    agent = object()

    handle = service.open(agent, SessionTransport.CLI, name="named", env={})
    response = service.ask(handle, SessionAskRequest(prompt="hello"))
    service.close(handle)

    UUID(handle)
    backend = factory.backends[0]
    assert backend.open_requests == [
        SessionOpenRequest(agent=agent, transport=SessionTransport.CLI, name="named", env={})
    ]
    assert response == SessionAskResponse(content="answer")
    assert backend.ask_requests == [SessionAskRequest(prompt="hello")]
    assert backend.close_calls == 1


def test_close_is_idempotent_but_closed_unknown_and_forged_handles_cannot_be_used() -> None:
    service, _ = _service()
    handle = service.open(object(), SessionTransport.CLI, env={})

    service.close(handle)
    service.close(handle)

    _assert_error("ask", lambda: service.ask(handle, SessionAskRequest(prompt="again")))
    _assert_error("stats", lambda: service.stats("unknown"))
    _assert_error("reset", lambda: service.reset(str(UUID(int=0))))
    _assert_error("close", lambda: service.close("forged"))


def test_unsupported_operations_fail_before_the_backend_is_called() -> None:
    supported = ALL_OPERATIONS - {SessionOperation.COMPACT}
    service, factory = _service(FakeBackendFactory(supported))
    handle = service.open(object(), SessionTransport.CLI, env={})

    _assert_error("compact", lambda: service.compact(handle, "make room"))

    assert factory.backends[0].compact_requests == []


def test_default_session_snapshots_the_first_agent_and_returns_it_after_changes() -> None:
    service, factory = _service()
    first_agent = object()

    first = service.default(first_agent, SessionTransport.CLI, env={})
    second = service.default(object(), SessionTransport.RPC, env={})

    assert second == first
    assert len(factory.backends) == 1
    assert factory.backends[0].open_requests[0].agent is first_agent


def test_reset_all_discards_handles_and_starts_a_fresh_default_generation() -> None:
    service, factory = _service()
    first_default = service.default(AgentCommand("first"), SessionTransport.CLI, env={})
    explicit = service.open(AgentCommand("other"), SessionTransport.CLI, env={})
    service.close(explicit)

    service.reset_all()

    assert [backend.close_calls for backend in factory.backends] == [1, 1]
    assert not service.is_known(first_default)
    assert not service.is_known(explicit)
    next_default = service.default(AgentCommand("second"), SessionTransport.CLI, env={})
    assert next_default != first_default
    assert len(factory.backends) == 3


def test_reset_all_retains_failed_closes_for_cleanup_retry() -> None:
    service, factory = _service()
    failed = service.open(AgentCommand("failed"), SessionTransport.CLI, env={})
    closed = service.open(AgentCommand("closed"), SessionTransport.CLI, env={})
    factory.backends[0].close_error = RuntimeError("busy")

    with pytest.raises(ExceptionGroup):
        service.reset_all()

    assert service.is_known(failed)
    assert not service.is_known(closed)
    factory.backends[0].close_error = None
    service.reset_all()
    assert not service.is_known(failed)


def test_reset_all_starts_a_fresh_default_even_when_another_close_fails() -> None:
    service, factory = _service()
    first_default = service.default(AgentCommand("default"), SessionTransport.CLI, env={})
    other = service.open(AgentCommand("other"), SessionTransport.CLI, env={})
    factory.backends[1].close_error = RuntimeError("busy")

    with pytest.raises(ExceptionGroup):
        service.reset_all()

    assert not service.is_known(first_default)
    assert service.is_known(other)

    next_default = service.default(AgentCommand("second"), SessionTransport.CLI, env={})
    assert next_default != first_default
    assert service.ask(next_default, SessionAskRequest(prompt="hello")).content == "answer"


def test_close_all_closes_live_backends_once_and_is_safe_to_repeat() -> None:
    service, factory = _service()
    first = service.open(object(), SessionTransport.CLI, env={})
    second = service.open(object(), SessionTransport.RPC, env={})
    service.close(first)

    service.close_all()
    service.close_all()

    assert factory.backends[0].close_calls == 1
    assert factory.backends[1].close_calls == 1
    _assert_error("ask", lambda: service.ask(second, SessionAskRequest(prompt="nope")))


def test_close_all_attempts_every_session_and_leaves_failures_retryable() -> None:
    service, factory = _service()
    errors = [RuntimeError("first close failed"), None, RuntimeError("third close failed")]
    handles = [service.open(object(), SessionTransport.CLI, env={}) for _ in errors]
    for backend, error in zip(factory.backends, errors, strict=True):
        backend.close_error = error

    with pytest.raises(ExceptionGroup) as raised:
        service.close_all()

    assert raised.value.exceptions == (errors[0], errors[2])
    assert [backend.close_calls for backend in factory.backends] == [1, 1, 1]
    _assert_error("ask", lambda: service.ask(handles[1], SessionAskRequest(prompt="closed")))

    factory.backends[0].close_error = None
    factory.backends[2].close_error = None
    service.close_all()

    assert [backend.close_calls for backend in factory.backends] == [2, 1, 2]
    for handle in handles:
        _assert_error(
            "ask", lambda handle=handle: service.ask(handle, SessionAskRequest(prompt="closed"))
        )


def test_ephemeral_lifecycle_retires_its_handle_after_closing() -> None:
    service, factory = _service()
    agent = object()

    response = service.with_ephemeral(
        agent,
        SessionTransport.CLI,
        lambda handle: service.ask(handle, SessionAskRequest(prompt="hello")),
        env={},
    )

    assert response == SessionAskResponse(content="answer")
    assert factory.backends[0].close_calls == 1
    assert factory.backends[0].open_requests == [
        SessionOpenRequest(agent=agent, transport=SessionTransport.CLI, ephemeral=True, env={})
    ]
    assert service._entries == {}


def test_agl_host_single_prompt_ephemeral_lifecycle_retires_its_agent_mapping() -> None:
    service, factory = _service()
    host = AglSessionHost(service)
    agent = AgentCommand(command="worker")

    assert (
        host.with_ephemeral(
            agent, "Cli", lambda handle: host.ask(handle, "hello"), single_prompt=True, env={}
        )
        == "answer"
    )
    assert factory.backends[0].open_requests == [
        SessionOpenRequest(
            agent=AgentCommand("worker"),
            transport=SessionTransport.CLI,
            single_prompt=True,
            ephemeral=True,
            env={},
        )
    ]
    assert host._sessions == {}


def test_agl_host_ephemeral_lifecycle_retires_its_agent_mapping() -> None:
    service, factory = _service()
    host = AglSessionHost(service)
    agent = AgentCommand(command="worker")
    handles: list[str] = []

    response = host.with_ephemeral(
        agent,
        "Cli",
        lambda handle: handles.append(handle) or host.ask(handle, "hello"),
        env={},
    )

    assert response == "answer"
    assert factory.backends[0].close_calls == 1
    assert service._entries == {}
    assert host._sessions == {}
    with pytest.raises(AglSessionHostError):
        host.close(handles[0])


def test_agl_host_reset_all_retires_all_successfully_closed_mappings() -> None:
    service, _factory = _service()
    host = AglSessionHost(service)
    agent = AgentCommand(command="worker")
    persistent = host.open(agent, "Cli", env={})
    ephemeral = host.open_ephemeral(agent, "Cli", env={})

    host.reset_all()

    assert not service.is_known(persistent)
    assert not service.is_known(ephemeral)
    assert host._sessions == {}


def test_agl_host_reset_all_keeps_only_failed_close_mappings() -> None:
    service, factory = _service()
    host = AglSessionHost(service)
    agent = AgentCommand(command="worker")
    failed = host.open(agent, "Cli", env={})
    closed = host.open(agent, "Cli", env={})
    factory.backends[0].close_error = RuntimeError("busy")

    with pytest.raises(ExceptionGroup):
        host.reset_all()

    assert set(host._sessions) == {failed}
    assert service.is_known(failed)
    assert not service.is_known(closed)


def test_close_all_retires_closed_ephemeral_host_mappings() -> None:
    service, factory = _service()
    host = AglSessionHost(service)
    agent = AgentCommand(command="worker")
    handle = host.open_ephemeral(agent, "Cli", single_prompt=True, env={})

    host.close_all()

    assert factory.backends[0].open_requests == [
        SessionOpenRequest(
            agent=AgentCommand("worker"),
            transport=SessionTransport.CLI,
            single_prompt=True,
            ephemeral=True,
            env={},
        )
    ]
    assert factory.backends[0].close_calls == 1
    assert service._entries == {}
    assert host._sessions == {}
    with pytest.raises(AglSessionHostError):
        host.close(handle)


def test_close_all_keeps_an_ephemeral_mapping_when_its_close_can_be_retried() -> None:
    service, factory = _service()
    host = AglSessionHost(service)
    handle = host.open_ephemeral(AgentCommand(command="worker"), "Cli", env={})
    factory.backends[0].close_error = RuntimeError("close failed")

    with pytest.raises(ExceptionGroup):
        host.close_all()

    assert handle in service._entries
    assert host._sessions[handle].ephemeral

    factory.backends[0].close_error = None
    host.close(handle)


def _ask_ephemeral(
    service: SessionService, agent: object, request: SessionAskRequest
) -> SessionAskResponse:
    """Ask through one short-lived session, as the AgL ephemeral ask does."""
    return service.with_ephemeral(
        agent,
        SessionTransport.CLI,
        lambda handle: service.ask(handle, request),
        single_prompt=True,
        env={},
    )


def test_ephemeral_ask_returns_its_response_after_closing() -> None:
    service, factory = _service()
    agent = object()

    response = _ask_ephemeral(service, agent, SessionAskRequest(prompt="hello"))

    assert response == SessionAskResponse(content="answer")
    assert factory.backends[0].open_requests == [
        SessionOpenRequest(
            agent=agent, transport=SessionTransport.CLI, single_prompt=True, ephemeral=True, env={}
        )
    ]
    assert factory.backends[0].close_calls == 1


def test_ephemeral_ask_closes_after_a_backend_failure() -> None:
    factory = FakeBackendFactory()
    error = RuntimeError("transport failed")

    def make_failing_backend(request: SessionOpenRequest) -> SessionBackend:
        backend = FakeBackend(factory.supported, ask_error=error)
        factory.backends.append(backend)
        return backend

    failing_service = SessionService(make_failing_backend)

    with pytest.raises(RuntimeError, match="transport failed"):
        _ask_ephemeral(failing_service, object(), SessionAskRequest(prompt="hello"))

    assert factory.backends[0].close_calls == 1


def test_ephemeral_ask_preserves_an_ask_error_when_close_also_fails() -> None:
    factory = FakeBackendFactory()
    ask_error = SessionAskError(
        cause="nonzero_exit",
        exit_code=1,
        stderr_tail="agent failed",
        elapsed=0.1,
        call_info=AgentCallInfo(
            argv=["runner"],
            prompt_via_stdin=False,
            elapsed=0.1,
            exit_code=1,
            sandboxed=False,
            permission_mode="none",
        ),
    )

    def make_failing_backend(request: SessionOpenRequest) -> SessionBackend:
        backend = FakeBackend(
            factory.supported,
            ask_error=ask_error,
            close_error=RuntimeError("close failed"),
        )
        factory.backends.append(backend)
        return backend

    service = SessionService(make_failing_backend)

    with pytest.raises(SessionAskError) as raised:
        _ask_ephemeral(service, object(), SessionAskRequest(prompt="hello"))

    assert raised.value is ask_error
    assert raised.value.call_info.to_trace() == {
        "argv": ["runner"],
        "prompt_via_stdin": False,
        "elapsed": 0.1,
        "exit_code": 1,
        "sandboxed": False,
        "permission_mode": "none",
    }
    assert factory.backends[0].close_calls == 1


def test_ephemeral_ask_surfaces_a_close_failure_after_a_successful_ask() -> None:
    factory = FakeBackendFactory()
    close_error = RuntimeError("close failed")

    def make_failing_backend(request: SessionOpenRequest) -> SessionBackend:
        backend = FakeBackend(factory.supported, close_error=close_error)
        factory.backends.append(backend)
        return backend

    service = SessionService(make_failing_backend)

    with pytest.raises(RuntimeError) as raised:
        _ask_ephemeral(service, object(), SessionAskRequest(prompt="hello"))

    assert raised.value is close_error
    assert factory.backends[0].close_calls == 1


def test_fork_returns_a_distinct_live_handle_and_reset_preserves_the_original_handle() -> None:
    service, factory = _service()
    original = service.open(object(), SessionTransport.CLI, env={})
    backend = factory.backends[0]
    forked_backend = FakeBackend()
    backend.fork_result = forked_backend

    forked = service.fork(original)
    service.reset(original)

    assert forked != original
    assert backend.reset_calls == 1
    assert service.ask(original, SessionAskRequest(prompt="original")).content == "answer"
    assert service.ask(forked, SessionAskRequest(prompt="forked")).content == "answer"


def test_service_passes_the_open_time_sandbox_mode_to_the_backend() -> None:
    """A session's permission mode and sandbox limits are fixed at ``open`` and
    reach the backend's own ``SessionOpenRequest``."""
    limits = SandboxLimits(memory="4G")
    service, factory = _service()

    service.open(
        AgentCommand("cat"),
        "cli",
        permission_mode=PermissionMode.UNRESTRICTED,
        sandbox=limits,
        env={},
    )

    request = factory.backends[0].open_requests[0]
    assert request.permission_mode == PermissionMode.UNRESTRICTED
    assert request.sandbox == limits


def test_agl_session_host_snapshot_exposes_the_open_time_sandbox_mode() -> None:
    """The AgL-facing snapshot carries the same fixed-at-open mode a fork inherits."""
    limits = SandboxLimits(memory="4G")
    service, factory = _service()
    host = AglSessionHost(service)
    agent = AgentCommand(command="worker")

    handle = host.open(
        agent, "Cli", permission_mode=PermissionMode.UNRESTRICTED, sandbox=limits, env={}
    )
    factory.backends[0].fork_result = FakeBackend()
    forked = host.fork(handle)

    snapshot = host.snapshot(handle)
    forked_snapshot = host.snapshot(forked)

    assert snapshot.permission_mode == PermissionMode.UNRESTRICTED
    assert snapshot.sandbox == limits
    assert forked_snapshot.permission_mode == PermissionMode.UNRESTRICTED
    assert forked_snapshot.sandbox == limits

    default_handle = host.open(agent, "Cli", env={})
    assert host.snapshot(default_handle).permission_mode == PermissionMode.NONE
    assert host.snapshot(default_handle).sandbox is None


def test_a_fork_of_an_ephemeral_session_outlives_its_own_close() -> None:
    """A fork inherits its parent's agent and transport but never its ephemerality."""
    service, factory = _service()
    host = AglSessionHost(service)
    agent = AgentCommand(command="worker")
    parent = host.open_ephemeral(agent, "Cli", env={})
    factory.backends[0].fork_result = FakeBackend()

    forked = host.fork(parent)
    host.close(forked)
    host.close(parent)

    assert service.is_known(forked)
    snapshot = host.snapshot(forked)
    assert (snapshot.agent, snapshot.transport) == (agent, "Cli")
    host.close(forked)
    assert not service.is_known(parent)
    with pytest.raises(AglSessionHostError):
        host.close(parent)


def test_supported_operations_dispatch_to_the_backend() -> None:
    service, factory = _service()
    handle = service.open(object(), SessionTransport.RPC, env={})

    service.compact(handle)
    service.set_name(handle, "renamed")
    stats = service.stats(handle)

    assert factory.backends[0].compact_requests == [""]
    assert factory.backends[0].names == ["renamed"]
    assert stats.output_tokens == 5


def test_forked_child_inherited_sessions_are_inert_and_never_closed() -> None:
    service, factory = _service()
    default = service.default(AgentCommand("worker"), SessionTransport.CLI, env={})
    explicit = service.open(AgentCommand("other"), SessionTransport.CLI, env={})
    closed = service.open(AgentCommand("done"), SessionTransport.CLI, env={})
    service.close(closed)

    service.enter_forked_child(PortableSet())

    for handle in (default, explicit, closed):
        with pytest.raises(SessionHostError):
            service.ask(handle, SessionAskRequest("question"))
        with pytest.raises(SessionHostError):
            service.reset(handle)
        with pytest.raises(SessionHostError):
            service.close(handle)
    service.close_all()
    service.reset_all()
    assert [backend.release_calls for backend in factory.backends] == [1, 1, 0]
    assert [backend.close_calls for backend in factory.backends] == [0, 0, 1]


def test_forked_child_reopens_the_default_session_fresh() -> None:
    service, factory = _service()
    inherited = service.default(AgentCommand("worker"), SessionTransport.CLI, env={})

    service.enter_forked_child(PortableSet())
    fresh = service.default(AgentCommand("worker"), SessionTransport.CLI, env={})

    assert fresh != inherited
    assert service.ask(fresh, SessionAskRequest("question")).content == "answer"
    with pytest.raises(SessionHostError):
        service.ask(inherited, SessionAskRequest("question"))
    service.close_all()
    assert [backend.close_calls for backend in factory.backends] == [0, 1]


def test_agl_host_forked_child_rejects_inherited_handles_and_reopens_the_default() -> None:
    service, _factory = _service()
    host = AglSessionHost(service)
    agent = AgentCommand("worker")
    default = host.default(agent, "Cli", env={})
    explicit = host.open(agent, "Cli", env={})

    host.enter_forked_child(PortableSet())

    for handle in (default, explicit):
        with pytest.raises(AglSessionHostError):
            host.ask(handle, "question")
        with pytest.raises(AglSessionHostError):
            host.close(handle)
        assert host.snapshot(handle).agent == agent
    fresh = host.default(agent, "Cli", env={})
    assert fresh != default
    assert host.ask(fresh, "question") == "answer"
    host.close_all()


@dataclass
class FakePortable:
    """A snapshot that counts how often its backend is opened."""

    backend: FakeBackend = field(default_factory=FakeBackend)
    materialized: int = 0
    error: Exception | None = None

    def materialize(self) -> SessionBackend:
        if self.error is not None:
            raise self.error
        self.materialized += 1
        return self.backend


def _snapshotting_service(
    handles: int = 1,
) -> tuple[SessionService, list[str], list[FakePortable], FakeBackendFactory]:
    """A service with *handles* explicit sessions, each of which snapshots as a FakePortable."""
    service, factory = _service()
    opened = [
        service.open(AgentCommand("worker"), SessionTransport.CLI, env={}) for _ in range(handles)
    ]
    portables = [FakePortable() for _ in opened]
    for backend, portable in zip(factory.backends, portables, strict=True):
        backend.portable_result = portable
    return service, opened, portables, factory


def test_portable_snapshots_each_listed_handle_once_and_marks_a_listed_default() -> None:
    service, (first, second), portables, factory = _snapshotting_service(2)
    default = service.default(AgentCommand("worker"), SessionTransport.CLI, env={})

    unlisted = service.portable([first, second, first])
    listed = service.portable([default, first])

    assert unlisted.sessions == {first: portables[0], second: portables[1]}
    assert unlisted.default is None
    assert set(listed.sessions) == {default, first}
    assert listed.default == default
    assert [backend.portable_calls for backend in factory.backends] == [2, 1, 1]


def test_portable_rejects_unknown_closed_and_foreign_handles() -> None:
    service, (live, closed), _portables, _factory = _snapshotting_service(2)
    service.close(closed)
    foreign = service.open(AgentCommand("worker"), SessionTransport.CLI, env={})
    service.enter_forked_child(PortableSet())

    for handle in ("unknown", closed, foreign):
        _assert_error("fork", lambda handle=handle: service.portable([live, handle]))


def test_portable_reports_unsupported_backends_and_failed_snapshots_as_fork_errors() -> None:
    factory = FakeBackendFactory(ALL_OPERATIONS - {SessionOperation.FORK})
    service = SessionService(factory)
    unsupported = service.open(AgentCommand("worker"), SessionTransport.CLI, env={})
    _assert_error("fork", lambda: service.portable([unsupported]))

    supported, backends = _service()
    handle = supported.open(AgentCommand("worker"), SessionTransport.CLI, env={})
    backends.backends[0].portable_error = SessionAskError(
        cause="nonzero_exit", exit_code=1, stderr_tail="boom", elapsed=0.1, call_info=None
    )
    _assert_error("fork", lambda: supported.portable([handle]))


def test_a_forked_child_resolves_listed_handles_to_lazily_materialized_snapshots() -> None:
    service, (listed, unlisted), (snapshot, _other), factory = _snapshotting_service(2)
    portables = service.portable([listed])

    service.enter_forked_child(portables)

    assert snapshot.materialized == 0
    assert service.ask(listed, SessionAskRequest("q")).content == "answer"
    service.ask(listed, SessionAskRequest("again"))
    assert snapshot.materialized == 1
    assert [request.prompt for request in snapshot.backend.ask_requests] == ["q", "again"]
    with pytest.raises(SessionHostError):
        service.ask(unlisted, SessionAskRequest("q"))
    service.close_all()
    # Inherited backends are released, never closed; the snapshot's own backend is closed.
    assert [backend.release_calls for backend in factory.backends] == [1, 1]
    assert [backend.close_calls for backend in factory.backends] == [0, 0]
    assert snapshot.backend.close_calls == 1


def test_a_forked_child_keeps_a_listed_default_and_reopens_an_unlisted_one() -> None:
    service, factory = _service()
    agent = AgentCommand("worker")
    default = service.default(agent, SessionTransport.CLI, env={})
    snapshot = FakePortable()
    factory.backends[0].portable_result = snapshot

    service.enter_forked_child(service.portable([default]))
    assert service.default(agent, SessionTransport.CLI, env={}) == default
    assert len(factory.backends) == 1
    service.ask(default, SessionAskRequest("q"))
    assert snapshot.materialized == 1

    service.enter_forked_child(PortableSet())
    fresh = service.default(agent, SessionTransport.CLI, env={})
    assert fresh != default
    assert len(factory.backends) == 2


def test_an_unused_snapshot_materializes_nothing_when_swept() -> None:
    service, (handle,), (snapshot,), _factory = _snapshotting_service()
    service.enter_forked_child(service.portable([handle]))

    service.enter_forked_child(PortableSet())
    service.close_all()
    service.reset_all()

    assert snapshot.materialized == 0


def test_passing_a_snapshot_on_gives_each_descendant_an_independent_snapshot() -> None:
    service, (handle,), (snapshot,), _factory = _snapshotting_service()
    service.enter_forked_child(service.portable([handle]))

    first = service.portable([handle]).sessions[handle]
    second = service.portable([handle]).sessions[handle]

    assert first is not snapshot
    assert second is not snapshot
    assert first is not second
    assert snapshot.materialized == 1
    assert snapshot.backend.portable_calls == 2


@pytest.mark.parametrize(
    "use",
    [
        lambda service, handle: service.stats(handle),
        lambda service, handle: service.reset(handle),
        lambda service, handle: service.compact(handle, ""),
        lambda service, handle: service.fork(handle),
        lambda service, handle: service.set_name(handle, "name"),
        lambda service, handle: service.continues_conversation(handle),
    ],
)
def test_the_first_use_of_an_unmaterialized_handle_materializes_it(
    use: Callable[[SessionService, str], object],
) -> None:
    service, (handle,), (snapshot,), _factory = _snapshotting_service()
    snapshot.backend.fork_result = FakeBackend()
    service.enter_forked_child(service.portable([handle]))
    assert snapshot.materialized == 0

    use(service, handle)
    use(service, handle)

    assert snapshot.materialized == 1


def test_a_failed_materialization_is_retried_by_the_next_use() -> None:
    service, (handle,), (snapshot,), _factory = _snapshotting_service()
    service.enter_forked_child(service.portable([handle]))
    snapshot.error = SessionHostError("spawn failed", "open")

    _assert_error("open", lambda: service.ask(handle, SessionAskRequest("q")))
    assert snapshot.materialized == 0
    snapshot.error = None

    assert service.ask(handle, SessionAskRequest("q")).content == "answer"
    assert snapshot.materialized == 1


def test_a_closed_unused_snapshot_is_closed_without_materializing() -> None:
    service, (handle,), (snapshot,), _factory = _snapshotting_service()
    service.enter_forked_child(service.portable([handle]))

    service.close(handle)

    assert snapshot.materialized == 0
    with pytest.raises(SessionHostError):
        service.ask(handle, SessionAskRequest("q"))


def test_agl_host_snapshots_handles_and_the_child_uses_them() -> None:
    service, factory = _service()
    host = AglSessionHost(service)
    agent = AgentCommand("worker")
    listed = host.open(agent, "Cli", env={})
    unlisted = host.open(agent, "Cli", env={})
    snapshot = FakePortable()
    factory.backends[0].portable_result = snapshot

    host.enter_forked_child(host.portable([listed]))

    assert host.ask(listed, "question") == "answer"
    assert snapshot.materialized == 1
    with pytest.raises(AglSessionHostError):
        host.ask(unlisted, "question")
    assert host.snapshot(listed).agent == agent


def test_agl_host_portable_maps_failures_to_session_errors() -> None:
    host = AglSessionHost(_service()[0])
    unsupported = SessionService(FakeBackendFactory(ALL_OPERATIONS - {SessionOperation.FORK}))
    handle = AglSessionHost(unsupported).open(AgentCommand("worker"), "Cli", env={})

    for failing, handles in ((host, ["unknown"]), (AglSessionHost(unsupported), [handle])):
        with pytest.raises(AglSessionHostError) as raised:
            failing.portable(handles)
        assert raised.value.operation == "fork"


def test_the_session_package_exports_only_its_host_factory() -> None:
    import agnette.agent.session as session

    assert session.create_agl_session_host is create_agl_session_host
    with pytest.raises(AttributeError):
        session.no_such_export
