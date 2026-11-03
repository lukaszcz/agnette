"""Corpus scenario harness: runs a program under a scenario and asserts its expectations.

Shared by ``tests/test_agl_e2e.py`` and ``tests/agl/isolated_runner.py``. The data format is
documented in ``tests/agl/README.md``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import traceback
import unittest.mock
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple, Protocol, Self, get_args

import pytest

from agnette.agent.session.protocol import (
    PortableSet,
    SessionAgentError,
    SessionHostError,
    SessionOperation,
    SessionOperations,
    unsupported_operation,
)
from agnette.agent.session.service import SessionService
from agnette.agent.spec import PermissionMode
from agnette.agl.diagnostics import Diagnostic
from agnette.agl.ir.builtin_vars import builtin_var_key
from agnette.agl.ir.contracts import JsonContractRequest
from agnette.agl.modules.ids import STD_CONFIG_ID
from agnette.agl.pipeline import PreparedProgram, ProgramDiscovery
from agnette.agl.runtime import host_session
from agnette.agl.runtime.fork_participants import register_built_ins
from agnette.agl.runtime.processes import (
    Disposition,
    ForkParticipant,
    ForkRequest,
    ProcessHost,
)
from agnette.agl.runtime.request import AgentCancelled
from agnette.agl.semantics.values import IntValue, Value
from agnette.agl.warning_policy import WarningPolicy
from agnette.agl.warnings import WARNING_CATALOG, WarningLevel
from agnette.core.fs import write_text_atomic
from agnette.sandbox.prepare import SandboxContext
from agnette.sandbox.request import Default, SandboxLimits
from tests._agl_helpers import (
    REPO_STDLIB_ROOT,
    agl_roots,
    function_param_values,
    module_param_values,
    prepare_inline_code,
    program_config_engine_seeds,
    session_sandbox_context,
    write_sandbox_home,
    write_transparent_sandbox_shims,
)
from tests._http_helpers import FakeHttp, fake_session
from tests._jev_helpers import install_jev_transport, jev_roots
from tests._package_helpers import package_info
from tests._process_helpers import FakeShell

AGL_DIR = Path(__file__).parent
PROGRAMS_DIR = AGL_DIR / "programs"
EXTERNS_PROGRAMS_DIR = PROGRAMS_DIR / "externs"
RESOURCE_PROGRAMS_DIR = PROGRAMS_DIR / "resources"
SESSION_STATIC_DECLARATIONS = """\
builtin def Session::open(
  agent: Agent,
  transport: Option[SessionTransport] = None,
  name: text = "",
  sandbox: AgentSandbox = std/config::sandbox,
  env: Environ = std/env::environ,
) -> Session

builtin def Session::default() -> Session
"""


def _contract_schema(contract: Any) -> Any:
    """The JSON Schema an agent receives for *contract*, or ``None`` for a text target."""
    if not isinstance(contract, JsonContractRequest):
        return None
    return json.loads(contract.json_schema)


def load_json(path: Path) -> Any:
    # parse_float=Decimal: AgL has no binary floats; scenario
    # params and expected exception fields must round-trip decimals exactly.
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def scenario_named(program: Path, name: str) -> dict[str, Any]:
    """The scenario *name* of *program*'s sidecar, as the corpus runs it."""
    scenarios = load_json(program.with_name(program.stem + ".scenarios.json"))["scenarios"]
    return next(scenario for scenario in scenarios if scenario["name"] == name)


def spec_warning_policy(spec: dict[str, Any]) -> WarningPolicy:
    """The warning levels a scenario or rejection sets, simulating ``--warning NAME=LEVEL``.

    Raises ``ValueError`` for a name outside the catalog or a level that is not one.
    """
    levels = spec.get("warning_levels", {})
    valid_levels = get_args(WarningLevel.__value__)
    for name, level in levels.items():
        if name not in WARNING_CATALOG:
            raise ValueError(f"unknown warning {name!r} in warning_levels")
        if level not in valid_levels:
            raise ValueError(f"invalid level {level!r} for warning {name!r}")
    return WarningPolicy(levels)


def fixture_roots(spec: dict[str, Any]) -> Any | None:
    """Build the module roots selected by one scenario or rejection fixture.

    ``packages`` mount as development checkouts and ``installed_packages`` as store-installed
    ones; ``stdlib_installed`` marks the ``stdlib_root`` as an installed standard library.
    """
    module_root_paths = [(AGL_DIR / str(root)).resolve() for root in spec.get("module_roots", [])]
    packages = tuple(
        replace(package_info(AGL_DIR / str(root)), installed=installed)
        for key, installed in (("packages", False), ("installed_packages", True))
        for root in spec.get(key, [])
    )
    if "stdlib_root" in spec or packages:
        from agnette.agl.modules.roots import RootSet

        stdlib_root = (
            AGL_DIR / str(spec["stdlib_root"]) if "stdlib_root" in spec else REPO_STDLIB_ROOT
        ).resolve()
        return RootSet(
            roots=frozenset(module_root_paths),
            packages=packages,
            stdlib_roots=frozenset({stdlib_root}),
            stdlib_installed=bool(spec.get("stdlib_installed")),
        )
    if module_root_paths:
        return agl_roots(*module_root_paths)
    return None


@dataclass
class _ScriptedSession:
    """One deterministic session observation owned by a scripted agent.

    ``permission_mode``/``sandbox`` are the mode this session's backend was
    opened under -- fixed then, for its whole lifetime, exactly like the
    production host's own session entry.
    """

    tag: str
    parent: str | None
    transport: str = ""
    single_prompt: bool = False
    opened: bool = False
    closed: bool = False
    prompts: list[str] = field(default_factory=list)
    operations: list[tuple[str, str | None, str]] = field(default_factory=list)
    permission_mode: str | None = None
    sandbox: SandboxLimits | None = None
    backend: Any = field(init=False, repr=False)


_SESSION_CAPABILITY_NAMES = frozenset(operation.value for operation in SessionOperation)


def _outcome_name(outcome: Any) -> str:
    return str(outcome.get("outcome", "success")) if isinstance(outcome, dict) else str(outcome)


class _PromptEvent(NamedTuple):
    """One recorded prompt observation: everything a scenario's ``prompts[]`` entry checks.

    ``sandbox_mode``/``sandbox_limits`` are the decoded ``PermissionMode``
    value and ``SandboxLimits`` an ``ask``/``ask-request``-style call's
    request carried; a plain session ask (``Session::ask``, no such request in
    hand) records both as ``None``. Recording every field of one observation
    in a single tuple, appended exactly once per call, makes a schema/mode
    mismatch between two separately indexed lists structurally impossible.
    """

    prompt: str
    schema: Any
    sandbox_mode: str | None
    sandbox_limits: SandboxLimits | None


@dataclass
class ScriptedAgent:
    """Replays scripted responses and records ordinary and session prompts.

    ``schemas`` records, alongside each ordinary call's ``prompt``, the
    structured JSON Schema from the output contract for that same call. It is
    separate from ``prompts`` so existing literal-prompt assertions are
    unaffected. Session observations use deterministic tags, letting
    scenarios assert conversation identity without depending on host handles.
    """

    name: str
    responses: list[str]
    repeat_last: bool = False
    interrupt_on_call: int | None = None
    session_capabilities: frozenset[str] = field(default_factory=lambda: _SESSION_CAPABILITY_NAMES)
    session_operations: dict[str, list[Any]] = field(default_factory=dict)
    session_ask_outcomes: list[Any] | None = None
    prompts: list[str] = field(default_factory=list)
    schemas: list[Any] = field(default_factory=list)
    prompt_events: list[_PromptEvent] = field(default_factory=list)
    sessions: list[_ScriptedSession] = field(default_factory=list)
    overflowed: bool = False
    session_operations_overflowed: bool = False
    session_ask_outcomes_overflowed: bool = False
    require_session_id: bool = False
    _response_count: int = 0

    def __post_init__(self) -> None:
        if any(
            _outcome_name(outcome) == "unsupported"
            for outcome in self.session_operations.get("reset", [])
        ):
            raise ValueError("reset does not support an unsupported outcome")

    def __call__(self, request: Any) -> str:
        self.prompts.append(request.prompt)
        contract = request.output_contract
        schema = _contract_schema(contract)
        self.schemas.append(schema)
        self.prompt_events.append(
            _PromptEvent(request.prompt, schema, request.permission_mode.value, request.sandbox)
        )
        self._interrupt_if_scheduled()
        return self._next_response()

    def _interrupt_if_scheduled(self) -> None:
        """Raise ``KeyboardInterrupt`` on the ``interrupt_on_call``-th recorded prompt."""
        if self.interrupt_on_call == len(self.prompt_events):
            raise KeyboardInterrupt

    def session_service(self) -> Any:
        """Create an observable wrapper around the production session service."""
        return _ScriptedSessionService(self)

    def _new_session_backend(self, request: Any) -> Any:
        """Open the same transport-specific backend production would select.

        The request's agent is already the resolved host specification: the evaluator is
        the sole seam that decodes an AgL ``Agent`` value, so no session-host
        layer below it ever sees the value or has to decode it.
        """
        from agnette.agent.runner import command_targets_session_id
        from agnette.agent.spec import AgentClaude, AgentCodex, AgentCommand, AgentPi
        from agnette.util.interp import InterpolationError

        spec = request.agent
        transport = request.transport
        backend_type: type[_ScriptedSessionBackend] = _ScriptedSessionBackend
        continues_conversation = True
        command_without_session_id = False
        if transport == "scripted":
            capabilities = frozenset(SessionOperation)
            supports_name = True
        elif transport == "rpc":
            if not isinstance(spec, AgentPi):
                raise SessionHostError("RPC transport is only supported by AgentPi", "open")
            capabilities = frozenset(SessionOperation)
            backend_type = _ScriptedPiRpcSessionBackend
            supports_name = True
        elif transport == "cli":
            if isinstance(spec, AgentCommand):
                try:
                    continues_conversation = command_targets_session_id(spec.argv())
                except (ValueError, InterpolationError) as error:
                    raise SessionAgentError(str(error), "open") from error
                command_without_session_id = self.require_session_id and not continues_conversation
                capabilities = frozenset({SessionOperation.ASK})
                supports_name = False
            elif isinstance(spec, AgentClaude):
                capabilities = frozenset(
                    {SessionOperation.ASK, SessionOperation.COMPACT, SessionOperation.FORK}
                )
                supports_name = True
            elif isinstance(spec, AgentCodex):
                capabilities = frozenset({SessionOperation.ASK})
                supports_name = False
            elif isinstance(spec, AgentPi):
                capabilities = frozenset({SessionOperation.ASK, SessionOperation.FORK})
                supports_name = True
            else:
                raise SessionHostError("unsupported session agent", "open")
        else:
            raise SessionHostError(f"unsupported session transport {transport!r}", "open")
        if command_without_session_id and not (request.single_prompt or request.ephemeral):
            raise SessionHostError("command session requires a session placeholder", "open")
        if request.name and not supports_name:
            raise SessionHostError("scripted session does not support names", "set-name")
        session = _ScriptedSession(
            tag=f"session-{len(self.sessions) + 1}",
            parent=None,
            transport=transport,
            single_prompt=request.single_prompt,
            permission_mode=request.permission_mode.value,
            sandbox=request.sandbox,
            opened=True,
        )
        self.sessions.append(session)
        return backend_type(
            self,
            session,
            capabilities,
            supports_name=supports_name,
            continues_conversation=continues_conversation,
            command_without_session_id=command_without_session_id,
        )

    def _fork_session(self, parent: _ScriptedSession) -> Any:
        session = _ScriptedSession(
            tag=f"session-{len(self.sessions) + 1}",
            parent=parent.tag,
            opened=True,
            permission_mode=parent.permission_mode,
            sandbox=parent.sandbox,
        )
        self.sessions.append(session)
        backend = parent.backend
        return type(backend)(
            self,
            session,
            backend._native_capabilities,
            supports_name=backend._supports_name,
            continues_conversation=backend.continues_conversation,
            command_without_session_id=backend._command_without_session_id,
        )

    def _next_response(self) -> str:
        index = self._response_count
        self._response_count += 1
        if index < len(self.responses):
            return self.responses[index]
        if self.repeat_last and self.responses:
            return self.responses[-1]
        self.overflowed = True
        return ""

    def _next_session_ask_outcome(self) -> Any:
        if self.session_ask_outcomes is None:
            return "success"
        if not self.session_ask_outcomes:
            self.session_ask_outcomes_overflowed = True
            return "success"
        return self.session_ask_outcomes.pop(0)

    def _next_operation(self, operation: str) -> Any:
        script = self.session_operations.get(operation)
        if script is None:
            return "success"
        if not script:
            self.session_operations_overflowed = True
            return "success"
        return script.pop(0)

    def _operation_outcome(self, operation: str) -> str:
        script = self.session_operations.get(operation)
        if not script:
            return "success"
        return _outcome_name(script[0])

    def _supports_operation(self, operation: str) -> bool:
        return (
            operation in self.session_capabilities
            and self._operation_outcome(operation) != "unsupported"
        )

    def _consume_capability_rejection(self, operation: str) -> None:
        if self._operation_outcome(operation) == "unsupported":
            self._next_operation(operation)


class _ScriptedSessionService:
    """Record test-only session attempts while delegating behavior to ``SessionService``."""

    def __init__(self, agent: ScriptedAgent) -> None:
        self._agent = agent
        self._service = SessionService(self._backend_factory)
        self._sessions: dict[str, _ScriptedSession] = {}
        self._backends: dict[str, _ScriptedSessionBackend] = {}
        self._ephemeral_handles: set[str] = set()

    def _backend_factory(self, request: Any) -> Any:
        return self._agent._new_session_backend(request)

    def open(
        self,
        agent: object,
        transport: str,
        *,
        name: str = "",
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: SandboxLimits | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        handle = self._service.open(
            agent,
            transport,
            name=name,
            permission_mode=permission_mode,
            sandbox=sandbox,
            env=env,
        )
        self._sessions[handle] = self._agent.sessions[-1]
        self._backends[handle] = self._agent.sessions[-1].backend
        return handle

    def open_ephemeral(
        self,
        agent: object,
        transport: str,
        *,
        single_prompt: bool = False,
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: SandboxLimits | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        handle = self._service.open(
            agent,
            transport,
            ephemeral=True,
            single_prompt=single_prompt,
            permission_mode=permission_mode,
            sandbox=sandbox,
            env=env,
        )
        self._sessions[handle] = self._agent.sessions[-1]
        self._backends[handle] = self._agent.sessions[-1].backend
        self._ephemeral_handles.add(handle)
        return handle

    def with_ephemeral(
        self,
        agent: object,
        transport: str,
        action: Callable[[str], Any],
        *,
        on_closed: Callable[[str], None] | None = None,
        single_prompt: bool = False,
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: SandboxLimits | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        def register(handle: str) -> Any:
            self._sessions[handle] = self._agent.sessions[-1]
            self._backends[handle] = self._agent.sessions[-1].backend
            self._ephemeral_handles.add(handle)
            return action(handle)

        def retire(handle: str) -> None:
            self._retire_ephemeral(handle)
            if on_closed is not None:
                on_closed(handle)

        return self._service.with_ephemeral(
            agent,
            transport,
            register,
            on_closed=retire,
            single_prompt=single_prompt,
            permission_mode=permission_mode,
            sandbox=sandbox,
            env=env,
        )

    def default(
        self,
        agent: object,
        transport: str,
        *,
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: SandboxLimits | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        handle = self._service.default(
            agent,
            transport,
            permission_mode=permission_mode,
            sandbox=sandbox,
            env=env,
        )
        if handle not in self._sessions:
            self._sessions[handle] = self._agent.sessions[-1]
            self._backends[handle] = self._agent.sessions[-1].backend
        return handle

    def ask(self, handle: str, request: Any) -> Any:
        return self._service.ask(handle, request)

    def compact(self, handle: str, instructions: str = "") -> None:
        self._attempt(
            handle, "compact", instructions, lambda: self._service.compact(handle, instructions)
        )

    def reset(self, handle: str) -> None:
        self._attempt(handle, "reset", None, lambda: self._service.reset(handle))

    def fork(self, handle: str) -> str:
        forked = self._attempt(handle, "fork", None, lambda: self._service.fork(handle))
        self._sessions[forked] = self._agent.sessions[-1]
        self._backends[forked] = self._agent.sessions[-1].backend
        return forked

    def set_name(self, handle: str, name: str) -> None:
        self._attempt(handle, "set-name", name, lambda: self._service.set_name(handle, name))

    def stats(self, handle: str) -> Any:
        return self._attempt(handle, "stats", None, lambda: self._service.stats(handle))

    def continues_conversation(self, handle: str) -> bool:
        return self._service.continues_conversation(handle)

    def snapshot_one(self, handle: str) -> Any:
        """Snapshot *handle*, with the capability bookkeeping of a native ``fork``."""
        return self._attempt(
            handle, "fork", None, lambda: self._service.portable([handle]).sessions[handle]
        )

    def enter_forked_child(self, snapshots: dict[str, _ScriptedPortable]) -> None:
        """Rebind the handles of *snapshots* to lazily materialized sessions of this service."""
        self._service.enter_forked_child(
            PortableSet({handle: snap.adopt(self, handle) for handle, snap in snapshots.items()})
        )

    def close(self, handle: str) -> None:
        self._service.close(handle)
        self._retire_ephemeral(handle)

    def close_all(self) -> None:
        try:
            self._service.close_all()
        finally:
            for handle in tuple(self._ephemeral_handles):
                if not self._service.is_known(handle):
                    self._retire_ephemeral(handle)

    def _retire_ephemeral(self, handle: str) -> None:
        if handle in self._ephemeral_handles:
            self._ephemeral_handles.remove(handle)
            del self._sessions[handle]
            del self._backends[handle]

    def _attempt(
        self,
        handle: str,
        operation: str,
        arg: str | None,
        action: Callable[[], Any],
    ) -> Any:
        try:
            return action()
        except SessionHostError as error:
            # Looked up only now: an adopted snapshot registers when ``action`` materializes it.
            session = self._sessions.get(handle)
            backend = self._backends.get(handle)
            if (
                session is not None
                and backend is not None
                and operation != "reset"
                and not backend.supports(operation)
            ):
                session.operations.append((operation, arg, "unsupported"))
                if error.operation == operation:
                    self._agent._consume_capability_rejection(operation)
            raise


class ScenarioSessionHost:
    """Route session requests to the scripted agent selected by an AgL value."""

    def __init__(self, agents: dict[str, ScriptedAgent]) -> None:
        self._services = {name: agent.session_service() for name, agent in agents.items()}
        self._handles: dict[str, _ScriptedSessionService] = {}
        self._snapshots: dict[
            str, tuple[Any, str, PermissionMode, SandboxLimits | None, dict[str, str], bool]
        ] = {}
        self._default_handle: str | None = None
        self._child_services: dict[str, _ScriptedSessionService] | None = None

    def script_child(self, agents: dict[str, ScriptedAgent]) -> None:
        """Serve the next forked child from *agents* alone, not the parent's scripts."""
        self._child_services = {name: agent.session_service() for name, agent in agents.items()}

    def open(
        self,
        agent: Any,
        transport: str,
        *,
        name: str = "",
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: SandboxLimits | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        service = self._service_for(agent)
        try:
            handle = service.open(
                agent,
                transport.lower(),
                name=name,
                permission_mode=permission_mode,
                sandbox=sandbox,
                env=env,
            )
        except SessionHostError as error:
            self._raise_host_error(error)
        self._handles[handle] = service
        self._snapshots[handle] = (
            agent,
            transport,
            permission_mode,
            sandbox,
            env or {},
            service.continues_conversation(handle),
        )
        return handle

    def open_ephemeral(
        self,
        agent: Any,
        transport: str,
        *,
        single_prompt: bool = False,
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: SandboxLimits | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        service = self._service_for(agent)
        try:
            handle = service.open_ephemeral(
                agent,
                transport.lower(),
                single_prompt=single_prompt,
                permission_mode=permission_mode,
                sandbox=sandbox,
                env=env,
            )
        except SessionHostError as error:
            self._raise_host_error(error)
        self._handles[handle] = service
        self._snapshots[handle] = (
            agent,
            transport,
            permission_mode,
            sandbox,
            env or {},
            service.continues_conversation(handle),
        )
        return handle

    def with_ephemeral(
        self,
        agent: Any,
        transport: str,
        action: Callable[[str], Any],
        *,
        single_prompt: bool = False,
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: SandboxLimits | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        service = self._service_for(agent)

        def register(handle: str) -> Any:
            self._handles[handle] = service
            self._snapshots[handle] = (
                agent,
                transport,
                permission_mode,
                sandbox,
                env or {},
                service.continues_conversation(handle),
            )
            return action(handle)

        def retire(handle: str) -> None:
            del self._handles[handle]
            del self._snapshots[handle]

        try:
            return service.with_ephemeral(
                agent,
                transport.lower(),
                register,
                on_closed=retire,
                single_prompt=single_prompt,
                permission_mode=permission_mode,
                sandbox=sandbox,
                env=env,
            )
        except SessionHostError as error:
            self._raise_host_error(error)

    def default(
        self,
        agent: Any,
        transport: str,
        *,
        permission_mode: PermissionMode = PermissionMode.NONE,
        sandbox: SandboxLimits | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        if self._default_handle is not None:
            return self._default_handle
        service = self._service_for(agent)
        try:
            handle = service.default(
                agent,
                transport.lower(),
                permission_mode=permission_mode,
                sandbox=sandbox,
                env=env,
            )
        except SessionHostError as error:
            self._raise_host_error(error)
        self._handles[handle] = service
        self._snapshots[handle] = (
            agent,
            transport,
            permission_mode,
            sandbox,
            env or {},
            service.continues_conversation(handle),
        )
        self._default_handle = handle
        return handle

    def ask(self, handle: str, prompt: str) -> str:
        from agnette.agent.session.protocol import SessionAskRequest
        from agnette.agl.runtime.sessions import SessionAskError as AglSessionAskError

        try:
            return (
                self._service_for_handle(handle, "ask")
                .ask(handle, SessionAskRequest(prompt))
                .content
            )
        except SessionHostError as error:
            self._raise_host_error(error)
        except Exception as error:
            if hasattr(error, "cause"):
                raise AglSessionAskError(
                    cause=error.cause,
                    exit_code=error.exit_code,
                    stderr_tail=error.stderr_tail,
                    elapsed=error.elapsed,
                    call_info=None,
                ) from error
            raise

    def ask_request(self, handle: str, request: Any) -> Any:
        from agnette.agl.runtime.request import AgentResponse

        content = self.ask(handle, request.prompt)
        service = self._service_for_handle(handle, "ask")
        schema = _contract_schema(request.output_contract)
        # The underlying session ask (above) recorded a bare prompt event with
        # no schema/sandbox in hand; this call's full AgentRequest carries
        # both, so replace that entry with the complete observation in one
        # write rather than appending to a second, separately indexed list.
        service._agent.prompt_events[-1] = _PromptEvent(
            request.prompt, schema, request.permission_mode.value, request.sandbox
        )
        return AgentResponse(content)

    def compact(self, handle: str, instructions: str = "") -> None:
        self._operation(handle, "compact", instructions)

    def reset(self, handle: str) -> None:
        self._operation(handle, "reset")

    def fork(self, handle: str) -> str:
        service = self._service_for_handle(handle, "fork")
        try:
            forked = service.fork(handle)
        except SessionHostError as error:
            self._raise_host_error(error)
        self._handles[forked] = service
        self._snapshots[forked] = self._snapshots[handle]
        return forked

    def set_name(self, handle: str, name: str) -> None:
        self._operation(handle, "set_name", name)

    def stats(self, handle: str) -> Any:
        try:
            return self._service_for_handle(handle, "stats").stats(handle)
        except SessionHostError as error:
            self._raise_host_error(error)

    def snapshot(self, handle: str) -> Any:
        from agnette.agl.runtime.sessions import SessionHostError as AglSessionHostError
        from agnette.agl.runtime.sessions import SessionSnapshot

        try:
            agent, transport, permission_mode, sandbox, env, continues = self._snapshots[handle]
        except KeyError:
            raise AglSessionHostError("unknown session", "snapshot") from None
        return SessionSnapshot(
            agent,
            transport,
            permission_mode,
            sandbox,
            env=env,
            continues_conversation=continues,
        )

    def close(self, handle: str) -> None:
        service = self._service_for_handle(handle, "close")
        try:
            service.close(handle)
        except SessionHostError as error:
            self._raise_host_error(error)
        if handle not in service._sessions:
            del self._handles[handle]
            del self._snapshots[handle]

    def portable(self, handles: Sequence[str]) -> PortableSet:
        sessions: dict[str, Any] = {}
        for handle in dict.fromkeys(handles):
            try:
                sessions[handle] = self._service_for_handle(handle, "fork").snapshot_one(handle)
            except SessionHostError as error:
                self._raise_host_error(error)
        default = self._default_handle if self._default_handle in sessions else None
        return PortableSet(sessions, default)

    def enter_forked_child(self, portables: PortableSet) -> None:
        if self._child_services is not None:
            for service in self._services.values():
                service.enter_forked_child({})
            self._services, self._child_services = self._child_services, None
        listed: dict[str, dict[str, Any]] = {name: {} for name in self._services}
        for handle, portable in portables.sessions.items():
            name = _scripted_agent_name(self._snapshots[handle][0])
            if name not in self._services:
                raise ValueError(f"the forked child scripts no agent {name!r} for a listed session")
            listed[name][handle] = portable
            self._handles[handle] = self._services[name]
        for name, service in self._services.items():
            service.enter_forked_child(listed[name])
        self._default_handle = portables.default

    def close_all(self) -> None:
        failures: list[Exception] = []
        for service in self._services.values():
            try:
                service.close_all()
            except ExceptionGroup as error:
                failures.extend(error.exceptions)
        if failures:
            raise ExceptionGroup("failed to close one or more scripted sessions", failures)

    def _operation(self, handle: str, name: str, arg: str = "") -> None:
        service = self._service_for_handle(handle, name.replace("_", "-"))
        try:
            if name == "compact":
                service.compact(handle, arg)
            elif name == "set_name":
                service.set_name(handle, arg)
            else:
                service.reset(handle)
        except SessionHostError as error:
            self._raise_host_error(error)

    def _service_for(self, agent: Any) -> _ScriptedSessionService:
        from agnette.agl.runtime.sessions import SessionHostError as AglSessionHostError

        try:
            return self._services[_scripted_agent_name(agent)]
        except KeyError:
            raise AglSessionHostError("unknown scripted agent", "open") from None

    def _service_for_handle(self, handle: str, operation: str) -> _ScriptedSessionService:
        try:
            return self._handles[handle]
        except KeyError:
            from agnette.agl.runtime.sessions import SessionHostError as AglSessionHostError

            raise AglSessionHostError("unknown session", operation) from None

    @staticmethod
    def _raise_host_error(error: SessionHostError) -> None:
        from agnette.agl.runtime.sessions import SessionAgentError as AglSessionAgentError
        from agnette.agl.runtime.sessions import SessionHostError as AglSessionHostError

        if isinstance(error, SessionAgentError):
            raise AglSessionAgentError(error.message, error.operation) from error
        raise AglSessionHostError(error.message, error.operation) from error


class _ScriptedSessionBackend:
    """In-memory backend constrained to one production transport's surface.

    ``command_without_session_id`` mirrors a command without a session placeholder:
    it opens only for a single-prompt or ephemeral session.
    """

    def __init__(
        self,
        agent: ScriptedAgent,
        session: _ScriptedSession,
        native_capabilities: frozenset[SessionOperation],
        *,
        supports_name: bool,
        continues_conversation: bool,
        command_without_session_id: bool = False,
    ) -> None:
        self.continues_conversation = continues_conversation
        self._command_without_session_id = command_without_session_id
        self._agent = agent
        self._session = session
        self._native_capabilities = native_capabilities
        self._supports_name = supports_name
        session.backend = self

    @property
    def operations(self) -> SessionOperations:
        def native(operation: SessionOperation, method: Any) -> Any:
            return method if self.supports(operation.value) else None

        return SessionOperations(
            compact=native(SessionOperation.COMPACT, self.compact),
            fork=native(SessionOperation.FORK, self.fork),
            set_name=native(SessionOperation.SET_NAME, self.set_name),
            stats=native(SessionOperation.STATS, self.stats),
        )

    def supports(self, operation: str) -> bool:
        return SessionOperation(
            operation
        ) in self._native_capabilities and self._agent._supports_operation(operation)

    def ask(self, request: Any) -> Any:
        from agnette.agent.session.protocol import SessionAskError, SessionAskResponse
        from agnette.agent.transport import AgentCallInfo

        self._session.prompts.append(request.prompt)
        self._agent.prompt_events.append(
            _PromptEvent(request.prompt, None, self._session.permission_mode, self._session.sandbox)
        )
        self._agent._interrupt_if_scheduled()
        outcome = self._agent._next_session_ask_outcome()
        if isinstance(outcome, dict):
            elapsed = float(outcome.get("elapsed", 0.0))
            exit_code = outcome.get("exit_code")
            if not isinstance(exit_code, int | None):
                raise ValueError("session ask outcome exit_code must be an integer or null")
            raise SessionAskError(
                cause=outcome.get("cause", "timeout"),
                exit_code=exit_code,
                stderr_tail=str(outcome.get("stderr_tail", "")),
                elapsed=elapsed,
                call_info=AgentCallInfo(
                    argv=[],
                    prompt_via_stdin=False,
                    elapsed=elapsed,
                    exit_code=exit_code,
                    sandboxed=False,
                    permission_mode="none",
                ),
            )
        if outcome != "success":
            raise ValueError("session ask outcome must be 'success' or a transport failure")
        return SessionAskResponse(content=self._agent._next_response())

    def compact(self, instructions: str) -> None:
        self._apply_operation("compact", instructions)

    def reset(self) -> None:
        self._apply_operation("reset", None)

    def fork(self) -> Any:
        self._apply_operation("fork", None)
        return self._agent._fork_session(self._session)

    def portable(self) -> Any:
        if not self.supports(SessionOperation.FORK.value):
            raise unsupported_operation(SessionOperation.FORK)
        self._apply_operation("fork", None)
        return _ScriptedPortable(self._session)

    def set_name(self, name: str) -> None:
        self._apply_operation("set-name", name)

    def stats(self) -> Any:
        from agnette.agent.session.protocol import SessionStats

        outcome = self._apply_operation("stats", None)
        if not isinstance(outcome, dict):
            return SessionStats(0, 0, Decimal("0"), Decimal("0"))
        return SessionStats(
            input_tokens=int(outcome.get("input_tokens", 0)),
            output_tokens=int(outcome.get("output_tokens", 0)),
            cost=Decimal(str(outcome.get("cost", "0"))),
            context_percent=Decimal(str(outcome.get("context_percent", "0"))),
        )

    def close(self) -> None:
        self._session.closed = True

    def release_inherited(self) -> None:
        """Hold no descriptors to release; the session stays unclosed."""

    def _apply_operation(self, operation: str, arg: str | None) -> Any:
        outcome = self._agent._next_operation(operation)
        outcome_name = _outcome_name(outcome)
        self._session.operations.append((operation, arg, outcome_name))
        if outcome_name == "unsupported":
            from agnette.agent.session.protocol import SessionHostError

            raise SessionHostError(f"scripted session does not support {operation}", operation)
        return outcome


@dataclass
class _ScriptedPortable:
    """A snapshot of a scripted session, adopted by the service of the process that uses it."""

    parent: _ScriptedSession

    def adopt(self, service: _ScriptedSessionService, handle: str) -> _AdoptedPortable:
        return _AdoptedPortable(service, handle, self.parent)


@dataclass
class _AdoptedPortable:
    """Opens a fork of the parent session on the adopting service's own agent, on first use."""

    service: _ScriptedSessionService
    handle: str
    parent: _ScriptedSession

    def materialize(self) -> Any:
        agent = self.service._agent
        backend = agent._fork_session(self.parent)
        self.service._sessions[self.handle] = agent.sessions[-1]
        self.service._backends[self.handle] = backend
        return backend


class _ScriptedPiRpcSessionBackend(_ScriptedSessionBackend):
    """Native Pi RPC mock used by lifecycle scenarios without a real agent."""


def _scripted_agent_name(agent: Any) -> str:
    """Map an agent specification onto one scenario's scripted service."""
    import shlex

    from agnette.agent.spec import AgentCommand, AgentPi

    if isinstance(agent, AgentCommand):
        try:
            return shlex.split(agent.command)[0]
        except ValueError:
            return agent.command
    if isinstance(agent, AgentPi) and agent.provider:
        return agent.provider
    return "ask"


def agent_from_spec(name: str, spec: Any) -> ScriptedAgent:
    if isinstance(spec, list):
        return ScriptedAgent(name=name, responses=[str(r) for r in spec])
    session = spec.get("session", {})
    capability_spec = session.get("capabilities")
    capabilities = (
        _SESSION_CAPABILITY_NAMES
        if capability_spec is None
        else frozenset(str(operation) for operation in capability_spec)
    )
    unknown_capabilities = capabilities - _SESSION_CAPABILITY_NAMES
    if unknown_capabilities:
        raise ValueError(f"unknown session capabilities: {sorted(unknown_capabilities)}")
    operations = {
        str(operation): list(outcomes)
        for operation, outcomes in session.get("operations", {}).items()
    }
    ask_outcomes = session.get("ask")
    if ask_outcomes is not None and not isinstance(ask_outcomes, list):
        raise ValueError("session ask outcomes must be a list")
    if capability_spec is not None:
        for operation, outcomes in operations.items():
            if any(_outcome_name(outcome) == "unsupported" for outcome in outcomes):
                raise ValueError(
                    "unsupported outcomes must be expressed through session capabilities"
                )
    interrupt_on_call = spec.get("interrupt_on_call")
    if interrupt_on_call is not None and (
        isinstance(interrupt_on_call, bool)
        or not isinstance(interrupt_on_call, int)
        or interrupt_on_call < 1
    ):
        raise ValueError("interrupt_on_call must be a positive integer")
    return ScriptedAgent(
        name=name,
        responses=[str(r) for r in spec["responses"]],
        repeat_last=bool(spec.get("repeat_last", False)),
        interrupt_on_call=interrupt_on_call,
        session_capabilities=capabilities,
        session_operations=operations,
        session_ask_outcomes=None if ask_outcomes is None else list(ask_outcomes),
    )


type CompilationCache = dict[tuple[bool, bool], list[tuple[PreparedProgram, ProgramDiscovery]]]


def _run_prepared_entry(
    runtime: Any,
    prepared: Any,
    *,
    param_values: dict[str, Any],
    module_params: dict[str, Any] | None = None,
    function_params: dict[str, Any] | None = None,
    positional: list[Any] | None = None,
    process_environment: dict[str, str] | None = None,
    engine_seeds: dict[Any, Value] | None = None,
    discovery: ProgramDiscovery | None = None,
    trace_file: Path | None = None,
) -> Any:
    """Run the sole selected file-style entry through the public pipeline seams.

    Always routes through :meth:`PipelineDriver.preflight_arguments` (binding
    *positional*/*param_values* as the entry program's own value arguments),
    so a parameterless entry's own ``@config`` — module parameter or engine
    setting — is evaluated and seeded exactly like one that declares
    parameters or receives module parameters.
    """
    if discovery is None:
        discovery = runtime.discover_programs(prepared)
    if discovery.compiled is None:
        return runtime.run_prepared(
            prepared,
            builtin_var_seeds=engine_seeds,
            process_environment=process_environment,
            trace_file=trace_file,
        )
    entry_programs = [item for item in discovery.programs if item.module.is_entry]
    assert len(entry_programs) == 1
    entry_program = entry_programs[0]

    if not entry_program.parameters and module_params is None and function_params is None:
        assert not positional and not param_values, (
            "scenario supplied 'positional'/'params' but the entry program declares "
            "no value parameters to receive them - check the fixture's program "
            "signature"
        )

    argument_preflight = runtime.preflight_arguments(
        prepared,
        entry_program,
        positional=tuple(positional) if positional else (),
        compiled=discovery.compiled,
        param_values={
            **module_param_values(discovery, entry_program, module_params),
            **function_param_values(entry_program, function_params),
            **{
                entry_program.param_key(name): value for name, value in (param_values or {}).items()
            },
        },
    )
    if not argument_preflight.result.ok:
        return argument_preflight.result
    assert argument_preflight.executable is not None
    return runtime.run_prepared(
        prepared,
        compiled=discovery.compiled,
        executable=argument_preflight.executable,
        program_symbol=argument_preflight.executable.program_symbols[entry_program.node_id],
        arguments=argument_preflight.arguments,
        param_seeds=argument_preflight.param_seeds,
        function_param_seeds=argument_preflight.function_param_seeds,
        builtin_var_seeds={
            **program_config_engine_seeds(argument_preflight),
            **(engine_seeds or {}),
        }
        or None,
        process_environment=process_environment,
        trace_file=trace_file,
    )


def run_source_entry(
    runtime: Any,
    source: str,
    *,
    roots: Any | None = None,
    default_stdlib: bool = True,
) -> Any:
    """Prepare and invoke a sole explicit entry for focused file-style tests."""
    from agnette.agl import PipelineDriver

    prepared = PipelineDriver.prepare_program(source, roots=roots, default_stdlib=default_stdlib)
    return _run_prepared_entry(runtime, prepared, param_values={})


def _apply_sandbox_home(
    scenario: dict[str, Any], tmp_path: Path
) -> tuple[dict[str, Any], Callable[[], SandboxContext] | None]:
    """Materialize a scenario's ``sandbox_home`` fixture, when present.

    Writes a real ``[home]/.agm/sandbox`` settings tree and, unless
    ``unavailable`` is set, real (transparent, never-invoked) ``systemd-run``/
    ``srt`` shims onto a ``PATH`` fed to the program's ``std/env::environ``
    default -- so a sandboxed ``exec`` resolves backend availability and
    settings through the real sandbox library, exactly as production does,
    while ``run_capture_result`` stays mocked (the scripted ``shell`` sees the
    wrapped argv; no shim ever actually runs). ``unavailable`` points at an
    empty ``PATH`` instead, for a preparation-failure scenario. Returns the
    scenario with ``process_environment``/``params`` filled in (a literal
    ``$SANDBOX_SETTINGS_FILE`` param value becomes the real explicit-settings
    path, mirroring `prepare_temp_filesystem`'s ``$TEMP_ROOT``) and the
    ``get_sandbox_context`` callable to thread into the runtime -- ``None``
    when the scenario has no ``sandbox_home``, so an ordinary scenario never
    builds one. ``proj_dir: true`` gives the context a real project directory
    -- needed for a scenario to observe ``patch``, which ``SrtBackend`` only
    consults when a project directory is present.
    """
    spec = scenario.get("sandbox_home")
    if spec is None:
        return scenario, None
    home = tmp_path / "sandbox-home"
    write_sandbox_home(
        home,
        run_toml=spec.get("run_toml", ""),
        extra_settings_files=tuple(spec.get("extra_settings_files", ())),
    )
    proj_dir = tmp_path / "proj" if spec.get("proj_dir") else None
    explicit_settings = home / "explicit-settings.json"
    explicit_settings.write_text("{}", encoding="utf-8")
    process_environment = dict(scenario.get("process_environment") or {})
    if spec.get("unavailable"):
        process_environment.setdefault("PATH", str(tmp_path / "no-sandbox-binaries"))
    else:
        shim_dir = tmp_path / "sandbox-shims"
        write_transparent_sandbox_shims(shim_dir, log_dir=tmp_path / "sandbox-shim-log")
        process_environment.setdefault("PATH", str(shim_dir))
    scenario = _substitute_params(scenario, {"$SANDBOX_SETTINGS_FILE": str(explicit_settings)})
    scenario = {**scenario, "process_environment": process_environment}
    return scenario, session_sandbox_context(home, proj_dir=proj_dir)


class _Script(Protocol):
    def assert_complete(self) -> None: ...


@dataclass
class ScriptedBoundaries:
    """One process's scripted agents, shell and HTTP, built from a scenario or fork section."""

    agents: dict[str, ScriptedAgent]
    shell: FakeShell
    http_session: Any
    http: FakeHttp

    @classmethod
    def from_spec(cls, spec: dict[str, Any], *, require_session_id: bool = False) -> Self:
        agents = {
            name: agent_from_spec(name, item) for name, item in spec.get("agents", {}).items()
        }
        for agent in agents.values():
            agent.require_session_id = require_session_id
        http_session, http = fake_session(spec.get("http", []))
        return cls(agents, FakeShell(spec.get("shell", [])), http_session, http)


_FORK_SECTION_KEYS = frozenset({"agents", "shell", "http", "expect"})
_FORK_EXPECT_KEYS = frozenset(
    {"calls", "prompts", "sessions", "session_prompts", "session_operations", "ends"}
)


def assert_boundaries(
    boundaries: ScriptedBoundaries, expect: dict[str, Any], extra_scripts: list[_Script]
) -> None:
    """Check one process's scripts: every script consumed, every call and session as expected."""
    _assert_calls(boundaries.agents, expect)
    assert_sessions(boundaries.agents, expect)
    for script in [boundaries.shell, boundaries.http, *extra_scripts]:
        script.assert_complete()


class ActiveBoundaries:
    """The scripted boundaries of the running process: a forked child replaces them."""

    def __init__(self, boundaries: ScriptedBoundaries) -> None:
        self.current = boundaries


class ScriptedForks(ForkParticipant[tuple[int, ...]]):
    """Gives each forked process the scripted boundaries of its ``forks`` path.

    A path names the n-th fork of a process (``"2.1"``: the first fork of the second fork of the
    root). A process without a section gets empty scripts, which allow no calls. Each child that
    entered its fork hooks checks its own scripts as it exits, after its sessions closed, and
    writes the verdict atomically to ``reports/<path>.json``. The forking process first records
    each fork as ``reports/<path>.forked`` and each child records its pid as
    ``reports/<path>.pid`` (:func:`fork_pids`). :func:`assert_forks_reported` waits for the verdicts
    of the scripted paths that were forked and reads all that exist. A scripted process must
    report: it ends (is waited for, settled or terminated) by the end of the run, or runs to its
    end on its own. An unscripted one may be terminated before it reports.
    """

    def __init__(
        self,
        active: ActiveBoundaries,
        sections: dict[str, dict[str, Any]],
        boundaries: dict[str, ScriptedBoundaries],
        unscripted: ScriptedBoundaries,
        reports: Path,
        session_host: ScenarioSessionHost | None,
    ) -> None:
        self._active = active
        self._sections = sections
        self._boundaries = boundaries
        self._unscripted = unscripted
        self._reports = reports
        self._session_host = session_host
        self._path: tuple[int, ...] = ()
        self._forks = 0

    def prepare(self, request: ForkRequest) -> tuple[int, ...]:
        return (*self._path, self._forks + 1)

    def parent(self, state: tuple[int, ...]) -> None:
        self._forks += 1
        (self._reports / f"{_dotted(state)}{_FORKED_SUFFIX}").touch()

    def child(self, state: tuple[int, ...]) -> None:
        self._path, self._forks = state, 0
        write_text_atomic(self._reports / f"{self._name}{_PID_SUFFIX}", str(os.getpid()))
        self._active.current = self._boundaries.get(self._name, self._unscripted)
        if self._session_host is not None:
            self._session_host.script_child(self._active.current.agents)

    def child_exit(self, disposition: Disposition) -> None:
        expect = self._sections.get(self._name, {}).get("expect", {})
        try:
            ends = expect.get("ends", disposition.value)
            assert ends == disposition.value, (
                f"expected the process to end by {ends!r}, it ended by {disposition.value!r}"
            )
            assert_boundaries(self._active.current, expect, [])
            error = None
        except Exception:
            error = traceback.format_exc()
        write_text_atomic(self._reports / f"{self._name}.json", json.dumps({"error": error}))

    @property
    def _name(self) -> str:
        return _dotted(self._path)


def _dotted(path: tuple[int, ...]) -> str:
    return ".".join(map(str, path))


_REPORT_TIMEOUT = 30.0
_FORKED_SUFFIX = ".forked"
_PID_SUFFIX = ".pid"


def _never_forked(reports: Path, path: str) -> bool:
    """Whether *path* was not forked and can no longer be.

    Its forker (the path without the last component; the root for a top-level path) can no
    longer fork once it is the root (the run is over) or has reported, or if it was never
    forked itself. The forker's state is read before the marker: it records its forks before
    it reports.
    """
    forker = path.rpartition(".")[0]
    forker_done = forker == "" or (reports / f"{forker}.json").exists()
    if (reports / f"{path}.json").exists() or (reports / f"{path}{_FORKED_SUFFIX}").exists():
        return False
    return forker_done or _never_forked(reports, forker)


def _await_reports(reports: Path, paths: Collection[str], timeout: float) -> None:
    """Wait up to *timeout* seconds until every one of *paths* has reported.

    A path that was never forked fails at once. A forked scripted process may be detached and
    still running; it reports as it exits.
    """
    deadline = time.monotonic() + timeout
    while True:
        missing = sorted(path for path in paths if not (reports / f"{path}.json").exists())
        if not missing:
            return
        unforked = [path for path in missing if _never_forked(reports, path)]
        assert not unforked, f"scripted forks {unforked} were never forked"
        assert time.monotonic() < deadline, f"scripted forks {missing} never reported"
        time.sleep(0.01)


def assert_forks_reported(
    sections: dict[str, dict[str, Any]], reports: Path, timeout: float = _REPORT_TIMEOUT
) -> None:
    """Every scripted fork path was forked and reports, and every process that reported passed.

    Only scripted paths are awaited: a process terminated before it entered its fork hooks never
    reports, so an unscripted one may be missing. Every existing report is checked, so an
    unscripted process that made a call fails its empty scripts.
    """
    _await_reports(reports, sections, timeout)
    for report in sorted(reports.glob("*.json")):
        error = json.loads(report.read_text())["error"]
        assert error is None, f"forked process {report.stem!r} failed its checks:\n{error}"


def fork_pids(reports: Path) -> dict[str, int]:
    """The pid of the running process (``"root"``) and of every forked one, by fork path."""
    pids = {"root": os.getpid()}
    for record in reports.glob(f"*{_PID_SUFFIX}"):
        pids[record.name.removesuffix(_PID_SUFFIX)] = int(record.read_text())
    return pids


_PID_PLACEHOLDER = re.compile(r"\$\{(root|\d+(?:\.\d+)*)\}")


def _resolve_pids(text: str, pids: dict[str, int]) -> str:
    """Replace each ``${root}`` and ``${<fork path>}`` in *text* by that process's pid."""

    def pid_of(match: re.Match[str]) -> str:
        path = match.group(1)
        assert path in pids, f"no process {path!r} forked (pids known: {sorted(pids)})"
        return str(pids[path])

    return _PID_PLACEHOLDER.sub(pid_of, text)


def _resolve_record(spec: dict[str, Any], pids: dict[str, int]) -> dict[str, Any]:
    """*spec* with a pid placeholder as a whole value replaced by the integer pid."""
    resolved: dict[str, Any] = {}
    for key, value in spec.items():
        if isinstance(value, str):
            text = _resolve_pids(value, pids)
            whole = _PID_PLACEHOLDER.fullmatch(value) is not None
            resolved[key] = int(text) if whole else text
        else:
            resolved[key] = value
    return resolved


def _assert_stderr(err: str | None, expect: dict[str, Any], pids: dict[str, int]) -> None:
    """Check the stderr expectations; ``${...}`` placeholders stand for pids."""
    keys = {"stderr", "stderr_contains", "stderr_not_contains"} & expect.keys()
    if err is None:
        assert not keys, f"{sorted(keys)} need an isolated scenario"
        return
    if "stderr" in expect:
        want = _resolve_pids(expect["stderr"], pids)
        assert err == want, f"expected stderr {want!r}, got {err!r}"
    for needle in expect.get("stderr_contains", []):
        assert _resolve_pids(needle, pids) in err, f"{needle!r} not in stderr {err!r}"
    for needle in expect.get("stderr_not_contains", []):
        assert _resolve_pids(needle, pids) not in err, f"{needle!r} unexpectedly in stderr {err!r}"


def _assert_trace(trace: Path | None, expect: dict[str, Any], pids: dict[str, int]) -> None:
    """Check ``expect.trace`` and ``expect.trace_absent`` against the trace file's records.

    Records are matched as field subsets. Each ``trace`` entry names its writer's ``pid`` and
    must be found after the entry matched before it for the same pid: a process's records are
    in order, those of different processes are not.
    """
    wanted = expect.get("trace", [])
    absent = expect.get("trace_absent", [])
    if trace is None:
        return
    records = [json.loads(line) for line in trace.read_text().splitlines()]

    def matches(record: dict[str, Any], spec: dict[str, Any]) -> bool:
        return all(record.get(key) == value for key, value in spec.items())

    cursors: dict[int, int] = {}
    for entry in wanted:
        spec = _resolve_record(entry, pids)
        assert "pid" in spec, f"trace entry {entry!r} names no `pid`"
        own = [record for record in records if record["pid"] == spec["pid"]]
        start = cursors.get(spec["pid"], 0)
        found = next((i for i in range(start, len(own)) if matches(own[i], spec)), None)
        assert found is not None, (
            f"trace entry {entry!r} (as {spec!r}) not found after record {start} of its "
            f"process in {own!r}"
        )
        cursors[spec["pid"]] = found + 1
    for entry in absent:
        spec = _resolve_record(entry, pids)
        unexpected = [record for record in records if matches(record, spec)]
        assert not unexpected, f"trace has records matching {entry!r}: {unexpected!r}"


_INTERRUPTS: dict[str, type[BaseException]] = {
    "KeyboardInterrupt": KeyboardInterrupt,
    "AgentCancelled": AgentCancelled,
}
_TERMINATIONS = (SystemExit, KeyboardInterrupt)


def _run_program(
    source: str,
    scenario: dict[str, Any],
    program: Path,
    monkeypatch: pytest.MonkeyPatch,
    get_sandbox_context: Callable[[], SandboxContext] | None,
    reports: Path,
    compilations: CompilationCache | None = None,
    trace_file: Path | None = None,
) -> tuple[Any, ScriptedBoundaries, list[_Script], tuple[Diagnostic, ...]]:
    from agnette.agl import PipelineDriver
    from agnette.agl.runtime.externs import ExternRegistry

    runtime_options: dict[str, Any] = {}
    runtime_config = scenario.get("runtime", {})
    enforce = runtime_config.get("enforce_command_sessions") is True
    sections: dict[str, dict[str, Any]] = scenario.get("forks", {})
    for path, section in sections.items():
        unknown = (section.keys() - _FORK_SECTION_KEYS) | (
            section.get("expect", {}).keys() - _FORK_EXPECT_KEYS
        )
        assert not unknown, f"fork {path!r}: unsupported keys {sorted(unknown)}"
    boundaries = ScriptedBoundaries.from_spec(scenario, require_session_id=enforce)
    child_boundaries = {
        path: ScriptedBoundaries.from_spec(section, require_session_id=enforce)
        for path, section in sections.items()
    }
    unscripted = ScriptedBoundaries.from_spec({})
    active = ActiveBoundaries(boundaries)
    agents = boundaries.agents
    if "default_call_depth_limit" in runtime_config:
        runtime_options["default_call_depth_limit"] = runtime_config["default_call_depth_limit"]
    if "default_strict_json" in runtime_config:
        runtime_options["default_strict_json"] = runtime_config["default_strict_json"]
    engine_seeds: dict[Any, Value] = {}
    if "parse_error_retries" in runtime_config:
        engine_seeds[builtin_var_key(STD_CONFIG_ID, (), "parse-error-retries")] = IntValue(
            runtime_config["parse_error_retries"]
        )

    def dispatch_agent(request: Any) -> str:
        return active.current.agents[_scripted_agent_name(request.agent)](request)

    session_host: ScenarioSessionHost | None = None
    if agents or any(item.agents for item in child_boundaries.values()):
        runtime_options["agent_dispatcher"] = dispatch_agent
        session_host = ScenarioSessionHost(agents)
        runtime_options["session_host"] = session_host
    runtime_options["get_sandbox_context"] = get_sandbox_context
    scripted = ScriptedForks(active, sections, child_boundaries, unscripted, reports, session_host)

    def build_scripted_host(**built_ins: Any) -> ProcessHost:
        host = ProcessHost(reporter=built_ins.pop("reporter"))
        # First: its `child` hook runs before the built-ins', its `child_exit` after theirs.
        host.register(scripted)
        register_built_ins(host, **built_ins)
        return host

    monkeypatch.setattr(host_session, "build_process_host", build_scripted_host)
    registry = ExternRegistry()
    runtime = PipelineDriver(
        extern_registry=registry,
        resolve_agent_spec=None,
        warning_policy=spec_warning_policy(scenario),
        **runtime_options,
    )
    scripts: list[_Script] = []
    default_stdlib = not scenario.get("no_stdlib", False)
    entry_path: Path | None = None
    roots = fixture_roots(scenario)
    if "jev" in scenario:
        if "module_roots" in scenario:
            pytest.fail("a scenario sets both `jev` and `module_roots`")
        scripts.append(install_jev_transport(monkeypatch, runtime, registry, scenario["jev"]))
        roots = jev_roots()
    if roots is None and (
        program.is_relative_to(EXTERNS_PROGRAMS_DIR)
        or program.is_relative_to(RESOURCE_PROGRAMS_DIR)
    ):
        entry_path = program
        roots = agl_roots(program.parent.resolve())
    # `inline_entry` sources carry no `program def`: they run through the same
    # synthetic-entry transform as `agm exec -c`.
    prepare = (
        prepare_inline_code if scenario.get("inline_entry") else PipelineDriver.prepare_program
    )
    with (
        unittest.mock.patch(
            "agnette.core.process.run_capture_result",
            side_effect=lambda args, **options: active.current.shell(args, **options),
        ),
        unittest.mock.patch("agnette.core.http.open_session", lambda: active.current.http_session),
    ):
        key = (default_stdlib, bool(scenario.get("inline_entry")))
        cached = next(
            (
                (prepared, discovery)
                for prepared, discovery in (compilations or {}).get(key, [])
                if discovery.compiled is not None
            ),
            None,
        )
        if cached is None:
            prepared = prepare(
                source, entry_path=entry_path, roots=roots, default_stdlib=default_stdlib
            )
            discovery = runtime.discover_programs(prepared)
            if compilations is not None and discovery.compiled is not None:
                compilations.setdefault(key, []).append((prepared, discovery))
        else:
            prepared, discovery = cached

        try:
            result = _run_prepared_entry(
                runtime,
                prepared,
                param_values=scenario.get("params", {}),
                module_params=scenario.get("module_params"),
                function_params=scenario.get("function_params"),
                positional=scenario.get("positional"),
                process_environment=scenario.get("process_environment"),
                engine_seeds=engine_seeds,
                discovery=discovery,
                trace_file=trace_file,
            )
        except _TERMINATIONS as exc:
            result = exc
    # A terminated run reports no result; discovery holds the static warnings.
    warnings = discovery.warnings if isinstance(result, _TERMINATIONS) else tuple(result.warnings)
    return result, boundaries, scripts, warnings


def _assert_interrupted(result: Any, name: Any) -> None:
    if name not in _INTERRUPTS:
        raise ValueError(f"unknown interrupted expectation: {name!r}")
    assert type(result) is _INTERRUPTS[name], f"expected {name} interrupt, got {result!r}"


def _assert_host_error(result: Any, agents: dict[str, ScriptedAgent], spec: dict[str, Any]) -> None:
    assert not result.ok, "expected the run to fail param validation"
    assert result.error is None, "param validation failure is not an AgL exception"
    messages = " | ".join(d.message for d in result.diagnostics)
    for needle in spec.get("message_contains", []):
        assert needle.lower() in messages.lower(), (
            f"no diagnostic mentions {needle!r}; diagnostics: {messages!r}"
        )
    for agent in agents.values():
        assert agent.prompts == [], (
            f"agent {agent.name!r} was called despite param validation failing"
        )
        assert agent.sessions == [], (
            f"agent {agent.name!r} opened a session despite param validation failing"
        )


def _fields_match(actual: Any, expected: Any) -> bool:
    """``expected`` matches ``actual``, recursing into nested dicts as a subset.

    A dict-shaped expected value is an exact-match subset of the matching
    actual dict's keys (e.g. asserting only a nested exception field's
    ``status``/``body`` while ignoring its nondeterministic ``elapsed``);
    every other value compares with ``==``, exactly as ``expect.raises.fields``
    already does at the top level.
    """
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _fields_match(actual[key], value) for key, value in expected.items()
        )
    return bool(actual == expected)


def _assert_outcome(result: Any, expect: dict[str, Any]) -> None:
    diags = " | ".join(d.message for d in result.diagnostics)
    assert list(result.diagnostics) == [], f"unexpected static diagnostics: {diags}"
    if "raises" in expect:
        spec = expect["raises"]
        assert result.error is not None, f"expected uncaught {spec['type']}, got none"
        assert result.error.type_name == spec["type"], (
            f"expected uncaught {spec['type']}, got {result.error.type_name}"
        )
        if "line" in spec:
            assert result.error.line == spec["line"], (
                f"expected error line {spec['line']}, got {result.error.line}"
            )
        if "source_contains" in spec:
            assert result.error.source is not None
            assert spec["source_contains"] in result.error.source, (
                f"{spec['source_contains']!r} not in error source {result.error.source!r}"
            )
        for key, value in spec.get("fields", {}).items():
            actual = result.error.fields[key]
            assert _fields_match(actual, value), (
                f"{spec['type']}.{key}: expected {value!r}, got {actual!r}"
            )
        message = str(result.error.fields.get("message", ""))
        for needle in spec.get("message_contains", []):
            assert needle in message, f"{needle!r} not in message {message!r}"
        assert not result.ok, "expected the run to fail"
    else:
        if result.error is not None:
            raise AssertionError(
                f"unexpected uncaught {result.error.type_name}: {result.error.fields!r}"
            )
        assert result.ok, "expected the run to succeed"


def _source_ref(label: str | None) -> str | None:
    """Name a diagnostic's source: a path under ``tests/agl/`` when it is one, else its label."""
    if label is None:
        return None
    path = Path(label)
    return path.relative_to(AGL_DIR).as_posix() if path.is_relative_to(AGL_DIR) else label


def _assert_warnings(warnings: tuple[Diagnostic, ...], expect: dict[str, Any]) -> None:
    """Match the static warnings exactly against ``expect.warnings`` (default: none).

    An expected entry's optional ``column`` and ``source`` are checked too; entries without
    one match any column or source.
    """
    expected = expect.get("warnings", [])
    actual = [
        {"name": warning.warning_name, "line": warning.line}
        | ({"column": warning.column} if "column" in want else {})
        | ({"source": _source_ref(warning.source_label)} if "source" in want else {})
        for warning, want in zip(warnings, expected, strict=False)
    ]
    assert len(warnings) == len(expected) and actual == expected, f"static warnings: {warnings!r}"


def _assert_output(out: str, expect: dict[str, Any]) -> None:
    if "stdout" in expect:
        assert out == expect["stdout"], f"expected stdout {expect['stdout']!r}, got {out!r}"
    for needle in expect.get("stdout_contains", []):
        assert needle in out, f"{needle!r} not in stdout {out!r}"
    for needle in expect.get("stdout_not_contains", []):
        assert needle not in out, f"{needle!r} unexpectedly in stdout {out!r}"


def _schema_contains(schema: Any, needle: str) -> bool:
    if isinstance(schema, dict):
        return any(
            str(key) == needle or _schema_contains(value, needle) for key, value in schema.items()
        )
    if isinstance(schema, list):
        return any(_schema_contains(item, needle) for item in schema)
    return schema == needle


def assert_schema_paths(schema: Any, assertions: list[dict[str, Any]]) -> None:
    """Assert exact JSON Schema values at dictionary-key paths."""
    for assertion in assertions:
        actual = schema
        path = assertion["path"]
        for key in path:
            assert isinstance(actual, dict), f"{path!r} is not a dictionary path in {schema!r}"
            assert key in actual, f"{path!r} is absent from schema {schema!r}"
            actual = actual[key]
        assert actual == assertion["equals"], (
            f"schema at {path!r}: expected {assertion['equals']!r}, got {actual!r}"
        )


def _assert_prompt_text(prompt: str, spec: dict[str, Any]) -> None:
    if "equals" in spec:
        assert prompt == spec["equals"], f"expected prompt {spec['equals']!r}, got {prompt!r}"
    if "starts_with" in spec:
        assert prompt.startswith(spec["starts_with"]), (
            f"prompt {prompt!r} does not start with {spec['starts_with']!r}"
        )
    for needle in spec.get("contains", []):
        assert needle in prompt, f"{needle!r} not in prompt {prompt!r}"
    for needle in spec.get("not_contains", []):
        assert needle not in prompt, f"{needle!r} unexpectedly in prompt {prompt!r}"


def _assert_calls(agents: dict[str, ScriptedAgent], expect: dict[str, Any]) -> None:
    for name, agent in agents.items():
        assert not agent.overflowed, f"agent {name!r} was called more times than scripted"
    expected_calls = expect.get("calls", {})
    assert set(expected_calls) == set(agents), (
        "every scripted agent must have an exact call-count assertion; "
        f"expected entries for {sorted(agents)}, got {sorted(expected_calls)}"
    )
    for name, agent in agents.items():
        count = expected_calls[name]
        actual = len(agent.prompt_events) if "session_prompts" not in expect else len(agent.prompts)
        assert actual == count, f"agent {name!r}: expected {count} calls, got {actual}"
    for spec in expect.get("prompts", []):
        agent_spec = spec["agent"]
        if isinstance(agent_spec, dict):
            name = agent_spec.get("command", "ask")
        else:
            name = agent_spec
        assert isinstance(name, str)
        prompt_events = agents[name].prompt_events
        prompts = [event.prompt for event in prompt_events]
        call = spec["call"]
        assert call < len(prompts), (
            f"agent {spec['agent']!r} made only {len(prompts)} calls, no call {call}"
        )
        _assert_prompt_text(prompts[call], spec)
        event = prompt_events[call]
        for needle in spec.get("schema_contains", []):
            assert _schema_contains(event.schema, needle), (
                f"{needle!r} not in schema {event.schema!r}"
            )
        assert_schema_paths(event.schema, spec.get("schema_paths", []))
        if "sandbox" in spec:
            _assert_sandbox_expectation(
                event.sandbox_mode,
                event.sandbox_limits,
                spec["sandbox"],
                context=f"agent {name!r} call {call}",
            )


def _assert_sandbox_expectation(
    mode: str | None, limits: SandboxLimits | None, expected: Any, *, context: str
) -> None:
    """Check one recorded ``(permission_mode, limits)`` pair against a scenario's expectation.

    *expected* is either the bare permission-mode string (as decoded by
    ``agent.spec.PermissionMode``), or an object whose ``mode`` key checks the
    same thing and whose remaining keys (``memory``/``swap``/``patch``/
    ``settings``) check the decoded ``SandboxLimits`` field by field --
    ``Default`` compares equal to the JSON string ``"default"``, an absent
    ``settings_file`` to ``None``, and a present one to its ``str()``. Shared
    by a ``prompts[]`` entry's per-call check and a ``sessions[]`` entry's
    open-time check -- one recorded pair, one expectation shape, either way.
    """
    if isinstance(expected, str):
        assert mode == expected, f"{context}: expected permission mode {expected!r}, got {mode!r}"
        return
    if "mode" in expected:
        assert mode == expected["mode"], (
            f"{context}: expected permission mode {expected['mode']!r}, got {mode!r}"
        )
    limit_fields = {"memory", "swap", "patch"}.intersection(expected)
    if limit_fields or "settings" in expected:
        assert limits is not None, f"{context}: expected decoded sandbox limits, got none"
        for field_name in limit_fields:
            actual = getattr(limits, field_name)
            actual = "default" if actual is Default else actual
            assert actual == expected[field_name], (
                f"{context}: expected sandbox {field_name} {expected[field_name]!r}, got {actual!r}"
            )
        if "settings" in expected:
            actual_settings = None if limits.settings_file is None else str(limits.settings_file)
            assert actual_settings == expected["settings"], (
                f"{context}: expected sandbox settings {expected['settings']!r}, "
                f"got {actual_settings!r}"
            )


def _substitute_params(scenario: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """Replace each ``scenario["params"]`` value found in *mapping* with its real path.

    Shared by every fixture that hands a scenario a real filesystem path
    through a literal sentinel value (``$TEMP_ROOT``, ``$SANDBOX_SETTINGS_FILE``),
    so the substitution logic exists once.
    """
    params = {
        name: mapping.get(value, value) if isinstance(value, str) else value
        for name, value in scenario.get("params", {}).items()
    }
    return {**scenario, "params": params}


def prepare_temp_filesystem(scenario: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    """Create a scenario's isolated filesystem fixture and bind its root parameter."""
    fixture = scenario.get("filesystem")
    if fixture is None:
        return scenario

    root = tmp_path / "filesystem"
    root.mkdir()
    for directory in fixture.get("directories", []):
        (root / directory).mkdir(parents=True)
    for relative_path, content in fixture.get("text_files", {}).items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for relative_path, content in fixture.get("hex_files", {}).items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes.fromhex(content))
    for relative_path, target in fixture.get("directory_symlinks", {}).items():
        try:
            (root / relative_path).symlink_to(root / target, target_is_directory=True)
        except OSError:
            pytest.skip("symbolic links are unavailable")

    return _substitute_params(scenario, {"$TEMP_ROOT": str(root)})


def assert_sessions(agents: dict[str, ScriptedAgent], expect: dict[str, Any]) -> None:
    for name, agent in agents.items():
        assert not agent.session_operations_overflowed, (
            f"session operation script for agent {name!r} was used more times than scripted"
        )
        assert not agent.session_ask_outcomes_overflowed, (
            f"session ask script for agent {name!r} was used more times than scripted"
        )
        assert not agent.session_ask_outcomes, (
            f"session ask script for agent {name!r} was not fully consumed"
        )
        unconsumed = {
            operation: len(outcomes)
            for operation, outcomes in agent.session_operations.items()
            if outcomes
        }
        assert not unconsumed, (
            f"session operation script for agent {name!r} was not fully consumed: {unconsumed}"
        )

    def session_for(spec: dict[str, Any]) -> _ScriptedSession:
        agent_name = spec["agent"]
        tag = spec.get("session", spec.get("tag"))
        assert isinstance(agent_name, str)
        assert isinstance(tag, str)
        matches = [session for session in agents[agent_name].sessions if session.tag == tag]
        assert len(matches) == 1, f"no unique session {tag!r} for agent {agent_name!r}"
        return matches[0]

    if "sessions" in expect:
        expected_sessions = {
            (spec["agent"], spec.get("session", spec.get("tag"))) for spec in expect["sessions"]
        }
        observed_sessions = {
            (agent_name, session.tag)
            for agent_name, agent in agents.items()
            for session in agent.sessions
        }
        assert expected_sessions == observed_sessions, (
            f"session set: expected {sorted(expected_sessions)}, got {sorted(observed_sessions)}"
        )

    for spec in expect.get("sessions", []):
        session = session_for(spec)
        for key in ("opened", "closed", "parent", "transport", "single_prompt"):
            if key in spec:
                assert getattr(session, key) == spec[key], (
                    f"session {session.tag!r} {key}: expected {spec[key]!r}, "
                    f"got {getattr(session, key)!r}"
                )
        if "sandbox" in spec:
            _assert_sandbox_expectation(
                session.permission_mode,
                session.sandbox,
                spec["sandbox"],
                context=f"session {session.tag!r}",
            )

    prompt_specs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for spec in expect.get("session_prompts", []):
        agent_name = spec["agent"]
        tag = spec.get("session", spec.get("tag"))
        assert isinstance(agent_name, str)
        assert isinstance(tag, str)
        prompt_specs.setdefault((agent_name, tag), []).append(spec)

    if "sessions" in expect or "session_prompts" in expect:
        for agent_name, agent in agents.items():
            for session in agent.sessions:
                specs = prompt_specs.get((agent_name, session.tag), [])
                assert len(specs) == len(session.prompts), (
                    f"session {session.tag!r} prompt count: expected {len(specs)}, "
                    f"got {len(session.prompts)}"
                )
                calls = sorted(spec["call"] for spec in specs)
                assert calls == list(range(len(session.prompts))), (
                    f"session {session.tag!r} prompt calls must cover each prompt exactly once"
                )

    for spec in expect.get("session_prompts", []):
        session = session_for(spec)
        call = spec["call"]
        assert isinstance(call, int)
        if "follow_up" in spec:
            assert call > 0, "a follow-up must be later than the first session prompt"
            follow_up = spec["follow_up"]
            assert isinstance(follow_up, dict)
            _assert_prompt_text(session.prompts[call], follow_up)
        else:
            _assert_prompt_text(session.prompts[call], spec)

    if "session_operations" not in expect:
        return

    operation_specs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for spec in expect["session_operations"]:
        agent_name = spec["agent"]
        tag = spec.get("session", spec.get("tag"))
        operation = spec["operation"]
        call = spec.get("call", 0)
        assert isinstance(agent_name, str)
        assert isinstance(tag, str)
        assert isinstance(operation, str)
        assert isinstance(call, int)
        operation_specs.setdefault((agent_name, tag), []).append(spec)

    for agent_name, agent in agents.items():
        for session in agent.sessions:
            specs = operation_specs.get((agent_name, session.tag), [])
            assert len(specs) == len(session.operations), (
                f"session {session.tag!r} operation count: expected {len(specs)}, "
                f"got {len(session.operations)}"
            )
            operation_names = {operation for operation, _, _ in session.operations}
            operation_names.update(spec["operation"] for spec in specs)
            for operation in operation_names:
                calls = [call for call in session.operations if call[0] == operation]
                expected_calls = sorted(
                    spec.get("call", 0) for spec in specs if spec["operation"] == operation
                )
                assert expected_calls == list(range(len(calls))), (
                    f"session {session.tag!r} {operation!r} operation calls must cover each "
                    "operation exactly once"
                )

    for spec in expect["session_operations"]:
        session = session_for(spec)
        operation = spec["operation"]
        call = spec.get("call", 0)
        assert isinstance(call, int)
        calls = [recorded for recorded in session.operations if recorded[0] == operation]
        _, arg, outcome = calls[call]
        if "arg" in spec:
            assert arg == spec["arg"], (
                f"{operation} argument: expected {spec['arg']!r}, got {arg!r}"
            )
        if "outcome" in spec:
            assert outcome == spec["outcome"], (
                f"{operation} outcome: expected {spec['outcome']!r}, got {outcome!r}"
            )


def assert_program_scenario(
    program: Path,
    scenario: dict[str, Any],
    read_stdout: Callable[[], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compilations: CompilationCache | None,
    read_stderr: Callable[[], str] | None = None,
) -> None:
    # Pinned so a sandboxed exec's ``cwd or Path.cwd()`` fallback never picks
    # up the real process cwd -- a checkout with a real ``.sandbox/`` would
    # otherwise merge in extra settings candidates non-hermetically.
    monkeypatch.chdir(tmp_path)
    # Explicit order: the filesystem fixture's ``$TEMP_ROOT`` and the sandbox
    # home's ``$SANDBOX_SETTINGS_FILE`` are independent sentinel substitutions
    # over the same scenario, so either order is safe -- but making it
    # explicit here (rather than one nested inside `_run_program`) means a
    # scenario using both never depends on incidental call order.
    scenario = prepare_temp_filesystem(scenario, tmp_path)
    scenario, get_sandbox_context = _apply_sandbox_home(scenario, tmp_path)
    reports = Path(tempfile.mkdtemp(prefix="fork-reports-", dir=tmp_path))
    trace = (
        Path(tempfile.mkdtemp(prefix="trace-", dir=tmp_path)) / "trace.jsonl"
        if {"trace", "trace_absent"} & scenario["expect"].keys()
        else None
    )
    result, boundaries, scripts, warnings = _run_program(
        program.read_text(encoding="utf-8"),
        scenario,
        program,
        monkeypatch,
        get_sandbox_context,
        reports,
        compilations,
        trace,
    )
    agents = boundaries.agents
    out = read_stdout()
    err = None if read_stderr is None else read_stderr()
    expect = scenario["expect"]
    if expect.get("shell_sandbox_units_distinct"):
        units = [argv[argv.index("--unit") + 1] for argv in boundaries.shell.argvs]
        assert len(set(units)) == len(units), f"expected distinct scope names, got {units}"
    if "host_error" in expect:
        _assert_host_error(result, agents, expect["host_error"])
        expect = {"stdout": "", "calls": dict.fromkeys(agents, 0), **expect}
    elif "exit_code" in expect:
        assert isinstance(result, SystemExit), f"expected exit, got {result!r}"
        assert result.code == expect["exit_code"], (
            f"expected exit code {expect['exit_code']!r}, got {result.code!r}"
        )
    elif "interrupted" in expect:
        _assert_interrupted(result, expect["interrupted"])
    else:
        assert not isinstance(result, _TERMINATIONS), f"unexpected termination: {result!r}"
        _assert_outcome(result, expect)
    _assert_warnings(warnings, expect)
    _assert_output(out, expect)
    assert_boundaries(boundaries, expect, scripts)
    assert_forks_reported(scenario.get("forks", {}), reports)
    pids = fork_pids(reports)
    _assert_stderr(err, expect, pids)
    _assert_trace(trace, expect, pids)
