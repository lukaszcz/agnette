"""Host-side engine-setting rules shared by ``agm exec`` and ``agm repl``.

``agm exec`` resolves the engine settings as host parameters; preflight returns them as
:class:`~agnette.agl.runtime.engine_config.EngineSettings`, and
:func:`engine_seed_values` and :func:`trace_decision` read the derived ``trace`` setting
back from them, so a run's log file and its readable ``trace`` setting can never disagree.
``agm repl`` has no program:
:func:`~agnette.host_params.config.programless_engine_values` ranks its CLI flags
over ``[exec]``, the declaration route, through the same ranking and decoding into the same
settings.
:func:`host_agent_spec_resolver` and :func:`host_agent_sandbox_resolver` build the
dispatch-time agent defaults both hosts apply from ``[agent]``.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, NoReturn

from agnette.agent.spec_defaults import (
    AgentConfig,
    AgentSpecResolver,
    configured_defaults_resolver,
)
from agnette.agl.ir.builtin_nominals import NO_BUILTIN_DECLARATIONS
from agnette.agl.ir.static_keys import HostParamKey, StaticBindingKey, static_binding_key
from agnette.agl.modules.ids import STD_CONFIG_ID
from agnette.agl.runtime.arguments import ParamTier
from agnette.agl.runtime.engine_config import (
    AgentSandboxResolver,
    EngineSettings,
    agent_sandbox_resolver,
    split_engine_values,
)
from agnette.agl.runtime.option import option_text
from agnette.agl.semantics.values import BoolValue, RecordValue
from agnette.agm.cli_support.program_options import engine_command
from agnette.config.engine_keys import (
    ENGINE_REGISTERS,
    engine_keys_for_register,
    trace_write_implies_enabled,
)
from agnette.core.log import TraceDecision

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from collections.abc import Set as AbstractSet

    from agnette.agl.ir.contracts import ParamDecoder
    from agnette.agl.semantics.values import Value
    from agnette.core.toml import TomlDict
    from agnette.host_params.surface import ParamSurfaceEntry

__all__ = [
    "engine_seed_values",
    "host_agent_sandbox_resolver",
    "host_agent_spec_resolver",
    "parse_engine_flags",
    "reject_register_conflicts",
    "resolve_strict_json",
    "resolve_timeout",
    "trace_decision",
]


def reject_register_conflicts(
    entries: "Iterable[ParamSurfaceEntry]", supplied: "Mapping[HostParamKey, object]"
) -> None:
    """Reject two engine settings of one register supplied together.

    Keys sharing a register name one destination, so ``--trace-file`` excludes
    ``--trace`` and ``--no-trace``, whatever spellings were typed. Raises
    ``ValueError`` naming the *entries*' primary flags.
    """
    flags = {entry.param.key: entry.primary_flag for entry in entries}
    for register in ENGINE_REGISTERS:
        given = [
            flags[key]
            for name in sorted(engine_keys_for_register(register))
            if (key := static_binding_key(STD_CONFIG_ID, (), name)) in supplied
        ]
        if len(given) > 1:
            raise ValueError(f"Options {' and '.join(given)} are mutually exclusive")


def parse_engine_flags(tokens: "Sequence[str]") -> dict[StaticBindingKey, object]:
    """Parse *tokens* as the bare engine-setting flags of a host with no program.

    Returns the explicit raw values by setting key, as ``agm exec`` parses them; raises
    ``ValueError`` for a malformed flag, a conflicting pair, or a leftover positional token.
    """
    command = engine_command()
    parsed = command.parse(tokens)
    if parsed.arguments.positional:
        raise ValueError(f"unexpected argument: {parsed.arguments.positional[0]!r}")
    reject_register_conflicts(command.surface.entries, parsed.params)
    return split_engine_values(parsed.params)[1]


def resolve_timeout(engine_seeds: "Mapping[str, Value]") -> float | None:
    """Parse the merged ``timeout`` seed into seconds; ``None`` when unset or cleared.

    Every tier's value is validated when decoded, so it always parses.
    """
    from agnette.core.parse import parse_timeout

    seed = engine_seeds.get("timeout")
    if not isinstance(seed, RecordValue):
        return None
    text = option_text(seed, nominals=NO_BUILTIN_DECLARATIONS)
    return None if text is None else parse_timeout(text)


def resolve_strict_json(engine_seeds: "Mapping[str, Value]") -> bool:
    seed = engine_seeds.get("strict-json")
    return isinstance(seed, BoolValue) and seed.value


def host_agent_spec_resolver(merged_config: "TomlDict") -> AgentSpecResolver:
    """Build the ``[agent]`` dispatch defaults; exit 1 naming the key when it is invalid."""
    try:
        return configured_defaults_resolver(merged_config)
    except ValueError as exc:
        _invalid_agent_configuration(exc)


def host_agent_sandbox_resolver(
    merged_config: "TomlDict",
    settings: EngineSettings,
    decoders: "Mapping[str, ParamDecoder]",
    *,
    from_manifest: "AbstractSet[HostParamKey]" = frozenset(),
) -> AgentSandboxResolver | None:
    """Build the ``[agent]`` sandbox defaults, decoded through the ``sandbox`` decoder.

    ``None`` when *settings* rank ``sandbox`` above ``[exec]``: any tier above the
    declaration route, or a manifest value (*from_manifest*). Every configured value is
    validated regardless; exit 1 naming the key when one is invalid.
    """
    try:
        resolver = agent_sandbox_resolver(
            AgentConfig.from_merged(merged_config), decoders["sandbox"]
        )
    except ValueError as exc:
        _invalid_agent_configuration(exc)
    rank = settings.ranks.get("sandbox")
    if rank is None or (
        rank is ParamTier.DECLARED
        and static_binding_key(STD_CONFIG_ID, (), "sandbox") not in from_manifest
    ):
        return resolver
    return None


def _invalid_agent_configuration(error: ValueError) -> NoReturn:
    print(f"Error: invalid agent configuration: {error}", file=sys.stderr)
    raise SystemExit(1) from error


def _trace_file_text(values: "Mapping[str, Value]") -> str | None:
    """Return the trace path *values* carries, or ``None`` for an empty/absent one."""
    value = values.get("trace-file")
    if not isinstance(value, RecordValue):
        return None
    return option_text(value, nominals=NO_BUILTIN_DECLARATIONS)


def _resolve_trace(settings: EngineSettings) -> "Value | None":
    """Return the derived ``trace`` setting, or ``None`` when nothing configures it.

    A supplied (command-line) ``trace`` or ``trace-file`` wins outright. Otherwise,
    when ``trace`` or ``trace-file`` is set at all, ``trace`` is true iff its ``trace``
    is true or its ``trace-file`` resolves to a real path (``Some``) — each key
    independently carrying its winning tier. Otherwise an effective ``debug`` turns
    ``trace`` on. A ``trace-file`` of none (a blank configured value included) is a
    configured trace setting at any tier, so with it set ``debug`` does not turn
    tracing on.
    """
    values, ranks = settings.values, settings.ranks
    if ranks.get("trace") is ParamTier.SUPPLIED:
        return values["trace"]
    if ranks.get("trace-file") is ParamTier.SUPPLIED:
        return BoolValue(True)
    if "trace" in values or "trace-file" in values:
        trace = values.get("trace")
        is_trace_true = isinstance(trace, BoolValue) and trace.value
        # The configuration follows the declared register relation a runtime
        # write follows: a real destination implies the switch.
        return BoolValue(
            is_trace_true
            or trace_write_implies_enabled("trace-file", _trace_file_text(values) is not None)
        )
    debug = values.get("debug")
    return BoolValue(True) if isinstance(debug, BoolValue) and debug.value else None


def engine_seed_values(settings: EngineSettings) -> "dict[str, Value]":
    """Return the engine seeds: the resolved *settings* plus the derived ``trace``."""
    trace = _resolve_trace(settings)
    return dict(settings.values) if trace is None else {**settings.values, "trace": trace}


def trace_decision(settings: EngineSettings) -> TraceDecision:
    """Resolve the trace decision the host's own log file starts from.

    The same resolution :func:`engine_seed_values` seeds ``trace`` with, read back as
    an on/off state plus the explicit destination (``None`` → the auto-generated
    timestamped path).  Left unconfigured everywhere, tracing is off. An explicit none
    ``trace-file`` (a blank configured value or ``@config(trace-file = None)``) at any tier counts
    as configuring the trace, so it keeps ``debug`` from turning tracing on, and a
    lower tier's ``trace-file`` it masks no longer applies.

    A program's own ``std/config::trace``/``trace-file`` write is not a
    layer here: it takes effect at runtime, from its program point onward,
    through the host settings reconfigurer, and overrides this decision —
    see ``docs/agl/reference/host-environment.md``.
    """
    trace = _resolve_trace(settings)
    return TraceDecision(
        enabled=isinstance(trace, BoolValue) and trace.value,
        explicit_path=_trace_file_text(settings.values),
    )
