"""Firewall-safe host protocol for persistent AgL agent sessions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING, NoReturn, Protocol, TypeVar, runtime_checkable
from uuid import uuid4

from agnette.agent.session.protocol import PortableSet
from agnette.agent.spec import PermissionMode, SessionTransport
from agnette.agent.transport import AgentCallInfo
from agnette.agl.runtime.request import AgentCallHostError, AgentRequest, AgentResponse
from agnette.agl.semantics.exceptions import make_builtin_exception
from agnette.agl.semantics.values import ExceptionValue, TextValue
from agnette.core.cleanup import preserve_primary_error

if TYPE_CHECKING:
    from agnette.agent.spec import AgentSpec
    from agnette.agl.ir.builtin_nominals import BuiltinNominals
    from agnette.agl.runtime.agents import AgentFn
    from agnette.sandbox.request import SandboxLimits

__all__ = [
    "AgentDispatcherSessionHost",
    "SessionAgentError",
    "SessionAskError",
    "SessionHost",
    "SessionRequestHost",
    "SessionHostError",
    "SessionSnapshot",
    "default_session_transport",
    "session_error_value",
    "with_ephemeral_session",
]

_T = TypeVar("_T")


def default_session_transport(spec: "AgentSpec") -> "SessionTransport":
    """Return the transport *spec* drives when none was selected."""
    return type(spec).DEFAULT_SESSION_TRANSPORT


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """The opening identity the host associates with an opaque handle.

    ``permission_mode``/``sandbox``/``env`` are the mode and environment this
    session's handle was fixed under at open -- never a per-ask override.
    ``continues_conversation`` is whether a later prompt continues the earlier
    ones (a corrective retry may then be a short follow-up) or each prompt must
    be self-contained.
    """

    agent: "AgentSpec"
    transport: str
    permission_mode: PermissionMode
    sandbox: "SandboxLimits | None"
    env: dict[str, str] = field(repr=False)
    continues_conversation: bool


@dataclass(frozen=True, slots=True)
class SessionStats:
    """Backend-neutral usage information for one session."""

    input_tokens: int
    output_tokens: int
    cost: Decimal
    context_percent: Decimal


class SessionHostError(Exception):
    """A lifecycle or capability failure exposed to the AgL evaluator."""

    def __init__(self, message: str, operation: str) -> None:
        self.message = message
        self.operation = operation
        super().__init__(message)


def session_error_value(error: SessionHostError, nominals: "BuiltinNominals") -> ExceptionValue:
    """The AgL ``SessionError`` value that reports the host failure *error*."""
    return make_builtin_exception(
        "SessionError",
        error.message,
        nominals=nominals,
        fields={"operation": TextValue(error.operation)},
    )


class SessionAgentError(SessionHostError):
    """An invalid agent value encountered while opening a session."""


class SessionAskError(Exception):
    """A transport failure from a session prompt."""

    def __init__(
        self,
        *,
        cause: str,
        exit_code: int | None,
        stderr_tail: str,
        elapsed: float,
        call_info: AgentCallInfo | None,
        detail: str | None = None,
    ) -> None:
        super().__init__(cause)
        self.cause = cause
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail
        self.elapsed = elapsed
        self.call_info = call_info
        # What the cause alone cannot say; see ``AgentCallHostError.detail``.
        self.detail = detail


@runtime_checkable
class SessionRequestHost(Protocol):
    """Optional session-host seam that preserves the whole request envelope.

    Hosts that adapt an ordinary agent dispatcher use this to retain the
    dispatcher-facing request metadata while the evaluator still owns the
    opaque session lifecycle.
    """

    def ask_request(self, handle: str, request: AgentRequest) -> AgentResponse: ...


@runtime_checkable
class EphemeralSessionHost(Protocol):
    """Optional host seam that owns an ephemeral session's complete scope."""

    def with_ephemeral(
        self,
        agent: "AgentSpec",
        transport: str,
        action: Callable[[str], _T],
        *,
        single_prompt: bool = False,
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: "SandboxLimits | None" = None,
        env: dict[str, str],
    ) -> _T: ...


class SessionHost(Protocol):
    """Host-owned lifecycle service addressed by opaque AgL session ids.

    *agent* is already resolved to its host specification: the evaluator
    decodes the AgL ``Agent`` value once, before it ever reaches a host.
    ``permission_mode``/``sandbox`` fix a newly opened session's sandboxing
    for its whole lifetime -- there is no per-ask override.
    """

    def open(
        self,
        agent: "AgentSpec",
        transport: str,
        *,
        name: str = "",
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: "SandboxLimits | None" = None,
        env: dict[str, str],
    ) -> str: ...

    def open_ephemeral(
        self,
        agent: "AgentSpec",
        transport: str,
        *,
        single_prompt: bool = False,
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: "SandboxLimits | None" = None,
        env: dict[str, str],
    ) -> str: ...

    def default(
        self,
        agent: "AgentSpec",
        transport: str,
        *,
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: "SandboxLimits | None" = None,
        env: dict[str, str],
    ) -> str: ...

    def ask(self, handle: str, prompt: str) -> str: ...

    def compact(self, handle: str, instructions: str = "") -> None: ...

    def reset(self, handle: str) -> None: ...

    def fork(self, handle: str) -> str: ...

    def set_name(self, handle: str, name: str) -> None: ...

    def stats(self, handle: str) -> SessionStats: ...

    def snapshot(self, handle: str) -> SessionSnapshot: ...

    def close(self, handle: str) -> None: ...

    def reset_all(self) -> None: ...

    def close_all(self) -> None: ...

    def portable(self, handles: Sequence[str]) -> PortableSet:
        """Snapshot the conversations of *handles* for a forked child.

        Raises :class:`SessionHostError` for an unknown handle and for a session that
        cannot be snapshotted.
        """
        ...

    def enter_forked_child(self, portables: PortableSet) -> None:
        """Make every session opened so far inert, as this process is a forked child.

        Sessions belong to the process that opened them: using or closing an inherited
        handle raises :class:`SessionHostError` (``snapshot`` still answers; the sweeps
        skip it). Each handle of *portables* instead resolves to its snapshot, opened on
        first use. The default handle is the snapshot's if it was listed, else it resets.
        """
        ...


def with_ephemeral_session(
    host: SessionHost,
    agent: "AgentSpec",
    transport: str,
    action: Callable[[str], _T],
    *,
    single_prompt: bool = False,
    permission_mode: PermissionMode = PermissionMode.NONE,
    sandbox: "SandboxLimits | None" = None,
    env: dict[str, str],
) -> _T:
    """Run *action* through one host ephemeral handle and always release it.

    ``single_prompt`` tells the host the handle serves exactly one prompt, so a
    backend need not establish a conversation it will never continue.
    ``permission_mode``/``sandbox``/``env`` fix this one-shot session's
    sandboxing and environment at open, for its whole (short) lifetime.
    """
    if isinstance(host, EphemeralSessionHost):
        return host.with_ephemeral(
            agent,
            transport,
            action,
            single_prompt=single_prompt,
            permission_mode=permission_mode,
            sandbox=sandbox,
            env=env,
        )
    handle = host.open_ephemeral(
        agent,
        transport,
        single_prompt=single_prompt,
        permission_mode=permission_mode,
        sandbox=sandbox,
        env=env,
    )
    with preserve_primary_error(
        lambda: host.close(handle), label="ephemeral agent session cleanup"
    ):
        return action(handle)


class AgentDispatcherSessionHost(SessionHost):
    """Dispatcher-backed session host used when no native session service exists.

    Its default handle snapshots an agent for the run, while each dispatch goes
    through the plain agent dispatcher. Explicit Agent-method calls retain their
    short-lived lifecycle.
    """

    def __init__(self, dispatcher: "AgentFn | None") -> None:
        self._dispatcher = dispatcher
        self._sessions: dict[str, SessionSnapshot] = {}
        self._closed: set[str] = set()
        self._inherited: set[str] = set()
        self._default_handle: str | None = None

    def open(
        self,
        _agent: "AgentSpec",
        _transport: str,
        *,
        name: str = "",
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: "SandboxLimits | None" = None,
        env: dict[str, str],
    ) -> str:
        del name, permission_mode, sandbox, env
        self._unavailable("open")

    def open_ephemeral(
        self,
        agent: "AgentSpec",
        transport: str,
        *,
        single_prompt: bool = False,
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: "SandboxLimits | None" = None,
        env: dict[str, str],
    ) -> str:
        del single_prompt
        handle = self._new_handle()
        self._sessions[handle] = SessionSnapshot(
            agent, transport, permission_mode, sandbox, env, continues_conversation=False
        )
        return handle

    def default(
        self,
        agent: "AgentSpec",
        transport: str,
        *,
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: "SandboxLimits | None" = None,
        env: dict[str, str],
    ) -> str:
        if self._default_handle is None:
            self._default_handle = self._new_handle()
            self._sessions[self._default_handle] = SessionSnapshot(
                agent, transport, permission_mode, sandbox, env, continues_conversation=False
            )
        return self._default_handle

    def ask(self, handle: str, prompt: str) -> str:
        snapshot = self._snapshot_for(handle, "ask")
        request = AgentRequest(agent=snapshot.agent, prompt=prompt, env=snapshot.env)
        return self.ask_request(handle, request).content

    def ask_request(self, handle: str, request: AgentRequest) -> AgentResponse:
        """Dispatch *request* under this handle's fixed-at-open sandboxing.

        *request*'s own ``permission_mode``/``sandbox``/``transport``/``env`` are replaced
        with the snapshot's, exactly as the production host applies a
        session's fixed mode and environment rather than a per-ask value:
        there is no per-ask override.
        """
        snapshot = self._snapshot_for(handle, "ask")
        if self._dispatcher is None:
            raise SessionAskError(
                cause="no_dispatcher",
                exit_code=None,
                stderr_tail="",
                elapsed=0.0,
                call_info=None,
            )
        effective_request = replace(
            request,
            permission_mode=snapshot.permission_mode,
            sandbox=snapshot.sandbox,
            transport=SessionTransport(snapshot.transport),
            env=snapshot.env,
        )
        try:
            from agnette.agl.runtime.agents import dispatch_agent_value

            return dispatch_agent_value(effective_request, self._dispatcher)
        except AgentCallHostError as error:
            raise SessionAskError(
                cause=error.cause,
                exit_code=error.exit_code,
                stderr_tail=error.stderr_tail,
                elapsed=error.elapsed,
                call_info=error.call_info,
                detail=error.detail,
            ) from error

    def compact(self, _handle: str, _instructions: str = "") -> None:
        self._unavailable("compact")

    def reset(self, _handle: str) -> None:
        self._unavailable("reset")

    def fork(self, _handle: str) -> str:
        self._unavailable("fork")

    def set_name(self, _handle: str, _name: str) -> None:
        self._unavailable("set-name")

    def stats(self, _handle: str) -> SessionStats:
        self._unavailable("stats")

    def snapshot(self, handle: str) -> SessionSnapshot:
        try:
            return self._sessions[handle]
        except KeyError:
            raise SessionHostError("unknown session", "snapshot") from None

    def close(self, handle: str) -> None:
        self._require_owned(handle, "close")
        if handle in self._closed:
            return
        self._agent_for(handle, "close")
        if handle == self._default_handle:
            self._closed.add(handle)
        else:
            del self._sessions[handle]

    def reset_all(self) -> None:
        self._sessions.clear()
        self._closed.clear()
        self._inherited.clear()
        self._default_handle = None

    def close_all(self) -> None:
        self.reset_all()

    def portable(self, handles: Sequence[str]) -> PortableSet:
        if handles:
            self._unavailable("fork")
        return PortableSet()

    def enter_forked_child(self, portables: PortableSet) -> None:
        del portables  # always empty: ``portable`` refuses every session
        self._inherited.update(self._sessions)
        self._default_handle = None

    def _require_owned(self, handle: str, operation: str) -> None:
        if handle in self._inherited:
            raise SessionHostError("session belongs to another process", operation)

    def _new_handle(self) -> str:
        return f"ephemeral-{uuid4().hex}"

    def _agent_for(self, handle: str, operation: str) -> "AgentSpec":
        return self._snapshot_for(handle, operation).agent

    def _snapshot_for(self, handle: str, operation: str) -> SessionSnapshot:
        self._require_owned(handle, operation)
        if handle in self._closed:
            raise SessionHostError("closed session", operation)
        try:
            return self._sessions[handle]
        except KeyError:
            raise SessionHostError("unknown session", operation) from None

    @staticmethod
    def _unavailable(operation: str) -> NoReturn:
        raise SessionHostError("persistent session host is unavailable", operation)
