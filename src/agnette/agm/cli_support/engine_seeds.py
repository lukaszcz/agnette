"""Host-side engine-setting rules shared by ``agm exec`` and ``agm repl``.

``agm exec`` resolves the engine settings as host parameters; preflight returns them as
:class:`~agnette.agl.runtime.engine_config.EngineSettings`, and
:func:`engine_seed_values` and :func:`trace_decision` read the derived ``trace`` setting
back from them, so a run's log file and its readable ``trace`` setting can never disagree.
``agm repl`` has no program: :func:`build_host_engine_seeds` layers its CLI flags over
``[exec]``, the declaration route, into an :class:`EngineSeedTiers`, which reads through the
same two functions.
:func:`host_agent_spec_resolver` builds the dispatch-time agent defaults both hosts apply
from ``[agent]``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agnette.agent.spec_defaults import AgentSpecResolver, configured_defaults_resolver
from agnette.agl.ir.builtin_nominals import NO_BUILTIN_DECLARATIONS
from agnette.agl.ir.static_keys import HostParamKey, static_binding_key
from agnette.agl.modules.ids import STD_CONFIG_ID
from agnette.agl.runtime.arguments import OptionSome, ParamTier, option_none_raw
from agnette.agl.runtime.engine_config import (
    EngineSettings,
    engine_origin,
    invalid_engine_message,
    split_engine_values,
    validate_engine_leaf_value,
)
from agnette.agl.runtime.option import option_text
from agnette.agl.semantics.values import BoolValue, RecordValue
from agnette.agm.cli_support.program_options import engine_command
from agnette.config.engine_keys import (
    ENGINE_KEYS,
    ENGINE_REGISTERS,
    EngineKeyKind,
    engine_keys_for_register,
    trace_write_implies_enabled,
)
from agnette.core.log import TraceDecision
from agnette.core.parse import normalize_option_text

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from agnette.agl.semantics.type_table import TypeTable
    from agnette.agl.semantics.values import Value
    from agnette.config.general import ExecConfig
    from agnette.core.toml import TomlDict
    from agnette.host_params.surface import ParamSurfaceEntry

__all__ = [
    "EngineSeedTiers",
    "build_host_engine_seeds",
    "engine_seed_values",
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


def parse_engine_flags(tokens: "Sequence[str]") -> dict[str, object | None]:
    """Parse *tokens* as the bare engine-setting flags of a host with no program.

    Returns the explicit values by setting name, unboxed to the native shape config
    values take (a present ``None`` is an explicit empty ``Option``, as ``--no-timeout``
    gives); raises ``ValueError`` for a malformed flag, a conflicting pair, or a leftover
    positional token.
    """
    command = engine_command()
    parsed = command.parse(tokens)
    if parsed.arguments.positional:
        raise ValueError(f"unexpected argument: {parsed.arguments.positional[0]!r}")
    reject_register_conflicts(command.surface.entries, parsed.params)
    _, engine = split_engine_values(parsed.params)
    return {key[2]: _unboxed(raw) for key, raw in engine.items()}


def _unboxed(raw: object) -> object | None:
    """Return a parsed engine flag's native value: its payload, or ``None`` for a cleared one."""
    if isinstance(raw, OptionSome):
        return raw.value
    return None if raw == option_none_raw() else raw


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
        print(f"Error: invalid agent configuration: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _decode_engine_value(
    key_name: str, raw: object, origin: str, type_table: "TypeTable"
) -> "Value":
    """Decode one engine value; on failure print an origin-tagged error and exit 1.

    Decoding — including ``timeout``'s duration-syntax check — is
    :func:`~agnette.agl.runtime.engine_config.validate_engine_leaf_value`'s rule,
    so a CLI flag, a config-file entry, and a package manifest leaf fail
    identically.
    """
    try:
        return validate_engine_leaf_value(key_name, raw, type_table)
    except ValueError as exc:
        print(f"Error: {invalid_engine_message(key_name, origin, exc)}", file=sys.stderr)
        raise SystemExit(1) from exc


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


@dataclass(frozen=True)
class EngineSeedTiers:
    """``cli`` (explicit CLI flags) above ``upper`` (the ``[exec]`` declaration route).

    A config value the CLI overrides is absent from ``upper``.
    """

    cli: "Mapping[str, Value]"
    upper: "Mapping[str, Value]"

    def settings(self) -> EngineSettings:
        """Flatten the tiers into resolved settings: ``upper`` < ``cli``."""
        values: dict[str, Value] = {}
        ranks: dict[str, ParamTier] = {}
        for tier, values_of_tier in (
            (ParamTier.DECLARED, self.upper),
            (ParamTier.SUPPLIED, self.cli),
        ):
            values.update(values_of_tier)
            ranks.update(dict.fromkeys(values_of_tier, tier))
        return EngineSettings(values, ranks)

    def merged(self) -> "dict[str, Value]":
        """Flatten to the one seed mapping the engine seeds from, ``trace`` derived."""
        return engine_seed_values(self.settings())

    def trace_decision(self) -> TraceDecision:
        """Resolve the trace decision; see :func:`trace_decision`."""
        return trace_decision(self.settings())


def build_host_engine_seeds(
    *,
    config: "ExecConfig",
    primary_table: "Mapping[str, object]",
    cli_values: "Mapping[str, object | None]",
) -> "EngineSeedTiers":
    """Decode the engine settings the host explicitly controls into a two-tier seed set.

    ``cli`` holds every explicitly supplied CLI value; ``upper`` holds every
    *primary_table* (``[exec]``) value not overridden by the CLI, which is
    not decoded. A setting left to its default in every source stays absent
    from every tier, so a ``builtin var`` initializer supplies it instead of
    being suppressed by a host-side floor. ``cli_values`` contains only
    explicitly supplied CLI values; a present ``None`` is an explicit empty
    ``Option``. ``trace`` is seeded like any other key here;
    :meth:`EngineSeedTiers.merged` recomputes it once the ``trace-file``
    implication is folded in.

    Every key, ``agent`` included, decodes through
    :func:`~agnette.agl.runtime.engine_config.validate_engine_leaf_value` against
    one shared seeded ``TypeTable``. A decode failure prints an error naming
    the offending key and its origin to stderr and exits 1 here, before
    anything runs.
    """
    from agnette.agl.semantics.type_table import create_seeded_type_table

    type_table = create_seeded_type_table()

    cli: dict[str, Value] = {}
    upper: dict[str, Value] = {}
    for spec in ENGINE_KEYS:
        if spec.name in cli_values:
            cli[spec.name] = _decode_engine_value(
                spec.name,
                cli_values[spec.name],
                engine_origin(spec.name, supplied=True),
                type_table,
            )
        elif spec.name in primary_table:
            value: object
            if spec.kind is EngineKeyKind.OPTION_TEXT:
                # A blank or non-positive value is an explicit none, as in ``agm exec``.
                value = normalize_option_text(primary_table[spec.name])
            else:
                value = cast(object, getattr(config, spec.config_attr))
            upper[spec.name] = _decode_engine_value(
                spec.name, value, engine_origin(spec.name, supplied=False), type_table
            )

    return EngineSeedTiers(cli=cli, upper=upper)
