"""Runtime layer plain service dataclasses."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agnette.agent.spec_defaults import AgentSpecResolver
    from agnette.agl.attributes import ProgramOptionSpec
    from agnette.agl.ir.static_keys import Reachable, StaticBindingKey
    from agnette.agl.modules.ids import ModuleId
    from agnette.agl.runtime.agents import AgentFn
    from agnette.agl.runtime.externs import ExternRegistry
    from agnette.agl.runtime.sessions import SessionHost
    from agnette.agl.semantics.types import Type as AglType
    from agnette.agl.syntax.spans import SourceSpan
    from agnette.agl.zones import ParamZone
    from agnette.sandbox.prepare import SandboxContext

__all__ = [
    "HostEnvironment",
    "HostParamInfo",
    "HostParamKind",
    "ProgramDeclInfo",
    "ProgramParamInfo",
]


@dataclass(frozen=True, slots=True)
class HostEnvironment:
    """Assembled host-runtime environment shared by ``run`` and the REPL session.

    Bundles the services that both the whole-program runner
    (``PipelineDriver.run``) and the incremental ``ReplSession`` use:

    ``agent_dispatcher``
        The value-driven host dispatcher for ``Agent`` enum values.
    ``session_host``
        The opaque lifecycle service used by persistent ``Session`` values.
    ``get_sandbox_context``
        Lazily builds the `SandboxContext` a sandboxed ``exec`` call needs
        (see `agnette.sandbox.prepare.lazy_sandbox_context`). ``None`` when the
        host wires no sandbox context, in which case a sandboxed ``exec``
        call cannot run.
    ``resolve_agent_spec``
        Applies host agent defaults (see
        `agnette.agent.spec_defaults.configured_defaults_resolver`) to every
        decoded ``Agent`` spec before it is dispatched. ``None`` dispatches
        specs as decoded.
    ``extern_registry``
        The ``ExternRegistry`` used to import extern-def companion modules
        and resolve their callables.  Built empty; the pipeline populates it
        from a program's loaded modules before evaluation.
    """

    agent_dispatcher: "AgentFn | None"
    session_host: "SessionHost | None"
    extern_registry: "ExternRegistry"
    get_sandbox_context: "Callable[[], SandboxContext] | None" = None
    resolve_agent_spec: "AgentSpecResolver | None" = None


@dataclass(frozen=True, slots=True)
class ProgramParamInfo:
    """Static summary of one ``program def`` value parameter, as the host sees it.

    ``name``         — the declared parameter name.
    ``kind``         — the parameter's AgL zone (positional-only, standard,
                        named-only); ``cli`` may override its CLI projection.
    ``type``         — the parameter's checked type.
    ``has_default``  — ``True`` when the parameter has a default expression.
    ``span``         — the parameter's declaration span, the anchor for a
                        binding or decode diagnostic naming this parameter.
    ``cli``          — how the parameter presents itself on a command line:
                        its external name and whatever the ``@opt-*``
                        attributes add to it.
    ``is_path``      — ``True`` when the annotation spells the builtin ``path``
                        alias (directly, through non-generic aliases, or as
                        ``Option[path]``), which its checked ``text`` type
                        no longer shows.
    ``enum_values``  — the preferred value-syntax spelling of each enum member,
                        using ``@name`` when present.
    """

    name: str
    kind: "ParamZone"
    type: "AglType"
    has_default: bool
    span: "SourceSpan"
    cli: "ProgramOptionSpec"
    is_path: bool = False
    enum_values: tuple[str, ...] = ()


class HostParamKind(Enum):
    """The kind of declaration a :class:`HostParamInfo` is a host parameter of."""

    MODULE = "module"
    ENGINE = "engine"


@dataclass(frozen=True, slots=True)
class HostParamInfo:
    """Static summary of one host-configurable parameter (not a program argument).

    ``key``          — the parameter's structured identity.
    ``kind``         — what declares it.
    ``module``       — the declaring module.
    ``route_path``   — the declaration route below ``module``: the scope path of
                        a module parameter; empty for an engine setting.
    ``cli``          — its CLI presentation (external name, ``@opt-*``).
    ``is_path``      — whether its annotation spells the builtin ``path`` alias
                        (including ``Option[path]``), for filesystem completion.
    ``enum_values``  — the preferred value-syntax spelling of each enum member,
                        using ``@name`` when present.
    ``owner``        — the root or mount ``module`` resolved through
                        (:meth:`~agnette.agl.modules.roots.RootSet.owner_of`);
                        ``None`` when it has none (always, for an engine setting).

    Built only by the per-kind factories, which derive ``key`` from the
    identity fields; a new kind adds its own factory.
    """

    key: "StaticBindingKey"
    kind: HostParamKind
    module: "ModuleId"
    route_path: tuple[str, ...]
    name: str
    type: "AglType"
    cli: "ProgramOptionSpec"
    is_path: bool = False
    enum_values: tuple[str, ...] = ()
    owner: "Path | None" = None

    @classmethod
    def module_param(
        cls,
        module: "ModuleId",
        scope_path: tuple[str, ...],
        name: str,
        type: "AglType",
        cli: "ProgramOptionSpec",
        *,
        is_path: bool = False,
        enum_values: tuple[str, ...] = (),
        owner: "Path | None" = None,
    ) -> "HostParamInfo":
        """Build the info of a ``@param`` module binding."""
        from agnette.agl.ir.static_keys import static_binding_key

        return cls(
            key=static_binding_key(module, scope_path, name),
            kind=HostParamKind.MODULE,
            module=module,
            route_path=scope_path,
            name=name,
            type=type,
            cli=cli,
            is_path=is_path,
            enum_values=enum_values,
            owner=owner,
        )

    @classmethod
    def engine_setting(
        cls, name: str, type: "AglType", cli: "ProgramOptionSpec", *, is_path: bool = False
    ) -> "HostParamInfo":
        """Build the info of a root ``builtin var`` of ``std/config``."""
        from agnette.agl.ir.static_keys import static_binding_key
        from agnette.agl.modules.ids import STD_CONFIG_ID

        return cls(
            key=static_binding_key(STD_CONFIG_ID, (), name),
            kind=HostParamKind.ENGINE,
            module=STD_CONFIG_ID,
            route_path=(),
            name=name,
            type=type,
            cli=cli,
            is_path=is_path,
        )

    @property
    def declaration_path(self) -> str:
        """Return the module-qualified spelling of this parameter."""
        path = "::".join((*self.route_path, self.name))
        return f"{self.module.display()}::{path}"

    @property
    def declared_spellings(self) -> tuple[str, ...]:
        """Return every spelling that can address this parameter, before any claim is resolved."""
        from agnette.config.qualified_keys import param_spellings_for

        return param_spellings_for(
            self.module.segments, self.route_path, self.cli.name, is_entry=self.module.is_entry
        )


@dataclass(frozen=True, slots=True)
class ProgramDeclInfo:
    """Static summary of one ``program def`` declaration.

    ``module`` and ``scope_path`` retain the declaration identity in structured
    form. ``declaration_path`` is the external spelling within that module.
    ``is_entry`` records whether the declaration belongs to the graph's
    host-selected entry, independently of whether that entry has an ordinary
    package module id.
    ``span`` is the declaration's own span, the anchor for a diagnostic that
    names no single parameter (an unknown argument name, or an excess
    positional argument). ``parameters`` is the program's own value-parameter
    signature, in declaration order. ``doc`` is the declaration's own ``@doc``
    text, or ``None`` when it carries none. ``reachable`` is what the program
    statically references and decides its module parameters. ``closure`` holds
    this program's module followed by its source-reachable modules in loader
    order, and serves declaration-route diagnostics only. ``owner`` is the
    root or mount ``module`` resolved through (``None`` when it has none).
    """

    module: "ModuleId"
    scope_path: tuple[str, ...]
    name: str
    node_id: int
    span: "SourceSpan"
    parameters: tuple[ProgramParamInfo, ...]
    is_entry: bool
    doc: str | None
    reachable: "Reachable"
    closure: tuple["ModuleId", ...] = ()
    owner: "Path | None" = None

    @property
    def declaration_path(self) -> str:
        """Return the program's scope-qualified declaration spelling."""
        return "::".join((*self.scope_path, self.name))
