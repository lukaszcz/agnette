"""Host engine-config decoding helpers (CLI flags, config-file entries)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agnette.agent.spec import AGENT_SPECS
from agnette.agl.diagnostics import Diagnostic
from agnette.agl.ir.builtin_nominals import NO_BUILTIN_DECLARATIONS, BuiltinNominals
from agnette.agl.ir.builtin_vars import is_engine_builtin_var_key
from agnette.agl.ir.reserved_nominals import AGENT_SANDBOX_MEMBERS
from agnette.agl.ir.static_keys import HostParamKey, StaticBindingKey
from agnette.agl.runtime.arguments import ParamTier
from agnette.agl.semantics.values import IntValue, RecordValue
from agnette.config.engine_keys import ENGINE_KEYS, NON_NEGATIVE_ENGINE_KEYS, EngineKeyKind

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agnette.agl.ir.contracts import ParamDecoder
    from agnette.agl.ir.ids import NominalId
    from agnette.agl.runtime.types import HostParamInfo
    from agnette.agl.semantics.values import Value

__all__ = [
    "EngineRangeError",
    "EngineSettings",
    "decode_engine_raw",
    "engine_default_settings",
    "engine_setting_decoder",
    "engine_setting_name",
    "engine_setting_params",
    "engine_settings_from",
    "prepare_engine_config",
    "restamp_engine_setting",
    "split_engine_values",
    "validate_engine_value",
]


def _engine_key_shape(kind: EngineKeyKind) -> tuple[str, tuple[str, ...]] | None:
    """Return the ``(enum name, member names)`` an engine key *kind* restamps, if any."""
    if kind is EngineKeyKind.AGENT:
        return ("Agent", tuple(AGENT_SPECS))
    if kind is EngineKeyKind.AGENT_SANDBOX:
        return ("AgentSandbox", AGENT_SANDBOX_MEMBERS)
    if kind is EngineKeyKind.OPTION_TEXT:
        return ("Option", ("None", "Some"))
    return None


#: Engine key name -> ``(enum name, member names)``, built once from ``ENGINE_KEYS``.
_ENGINE_KEY_ENUM_SHAPES: dict[str, tuple[str, tuple[str, ...]]] = {
    spec.name: shape for spec in ENGINE_KEYS if (shape := _engine_key_shape(spec.kind)) is not None
}

#: Engine key name -> kind, built once from ``ENGINE_KEYS``.
_ENGINE_KEY_KINDS_BY_NAME: dict[str, EngineKeyKind] = {spec.name: spec.kind for spec in ENGINE_KEYS}


def _restamp_value_tree(
    value: "Value", *, from_table: "BuiltinNominals", to_table: "BuiltinNominals"
) -> "Value":
    """Recursively restamp every nominal identity in a record tree from *from_table* to *to_table*.

    A restamped record can itself carry other host-known nominal values (e.g.
    ``AgentSandbox``'s ``Sandbox`` member has ``Optional``/``Option``-typed
    fields), so restamping the outer identity alone would leave a nested
    value's identity unrecognized by *to_table*'s owner. Only records recurse:
    every engine-setting value is a scalar, an ``Option``/``Optional``, or a
    record built from those (see ``semantics.engine_keys.ENGINE_KEY_TYPES``)
    -- never an array or dict -- so this never needs to look inside a
    collection. A non-record value, including one nested in a field this
    table has no name for, crosses unchanged.
    """
    if not isinstance(value, RecordValue):
        return value
    fields = {
        name: _restamp_value_tree(field_value, from_table=from_table, to_table=to_table)
        for name, field_value in value.fields.items()
    }
    located = from_table.reverse(value.nominal)
    if located is None:
        return RecordValue(nominal=value.nominal, fields=fields)
    name, member_name = located
    target = (
        to_table.resolve(name)
        if member_name is None
        else to_table.resolve_standard_member(name, member_name)
    )
    return RecordValue(nominal=target, fields=fields)


def _restamp_host_enum_member(
    value: RecordValue,
    *,
    enum_name: str,
    member_names: tuple[str, ...],
    from_table: "BuiltinNominals",
    to_table: "BuiltinNominals",
) -> RecordValue:
    """Restamp *value*'s identity from *from_table* to *to_table*, if it is an *enum_name* member.

    Used both to bind a persisted engine-setting seed (reserved fallback
    identity) onto a program's own nominal table, and to persist a
    post-run engine-setting value (a program's own identity) back onto the
    reserved fallback table so it survives past that program's own lifetime.
    Once membership is established, restamping the outer identity together
    with its nested fields is exactly :func:`_restamp_value_tree`'s job, so
    this delegates to it rather than rebuilding the fields dict itself.
    """
    for member_name in member_names:
        source = from_table.resolve_standard_member(enum_name, member_name)
        if value.nominal == source:
            restamped = _restamp_value_tree(value, from_table=from_table, to_table=to_table)
            assert isinstance(restamped, RecordValue)
            return restamped
    return value


def restamp_engine_setting(
    key: str, value: "Value", *, from_table: "BuiltinNominals", to_table: "BuiltinNominals"
) -> "Value":
    """Restamp *value* onto *to_table*'s identity when *key* is enum-backed.

    An enum-backed engine key (``AGENT``/``AGENT_SANDBOX``/``OPTION_TEXT`` kind) carries the
    identity of whichever nominal table stamped it; every reader of such a
    value needs it in its own table's identity to recognize the value's
    members with :func:`~agnette.agl.ir.builtin_nominals.resolve_standard_member_name`
    (or, as here, directly). A non-enum-backed key, or a value that is not a
    ``RecordValue``, crosses through unchanged.
    """
    if not isinstance(value, RecordValue):
        return value
    shape = _ENGINE_KEY_ENUM_SHAPES.get(key)
    if shape is None:
        return value
    enum_name, member_names = shape
    return _restamp_host_enum_member(
        value,
        enum_name=enum_name,
        member_names=member_names,
        from_table=from_table,
        to_table=to_table,
    )


def engine_default_settings() -> "dict[str, Value]":
    """Build the typed engine-default value for every defaulted engine key.

    Decodes the shared engine-key catalog's defaults through :func:`decode_engine_raw` against
    the reserved defs; a ``None`` default is an ``Option`` setting's none.
    ``agent`` and ``sandbox`` have no host-side default: they come from the ``std/config``
    ``builtin var`` declarations like any other declared default.
    """
    from agnette.agl.runtime.arguments import option_none_raw
    from agnette.agl.type_schema import engine_setting_decoders

    decoders = engine_setting_decoders({})
    return {
        spec.name: decode_engine_raw(
            spec.name,
            option_none_raw() if spec.default is None else spec.default,
            decoders[spec.name],
        )
        for spec in ENGINE_KEYS
        if spec.has_default
    }


_engine_setting_params: "tuple[HostParamInfo, ...] | None" = None


def engine_setting_params() -> "tuple[HostParamInfo, ...]":
    """Return every engine setting as a host parameter, in catalog order.

    Built once (a manual cache: the project's mypy configuration disallows the
    ``Any`` ``functools.cache`` introduces).
    """
    global _engine_setting_params
    if _engine_setting_params is None:
        from agnette.agl.attributes import ProgramOptionSpec
        from agnette.agl.runtime.types import HostParamInfo
        from agnette.agl.semantics.engine_keys import ENGINE_KEY_TYPES

        _engine_setting_params = tuple(
            HostParamInfo.engine_setting(
                spec.name,
                ENGINE_KEY_TYPES[spec.name],
                ProgramOptionSpec(name=spec.name, metavar=spec.metavar, doc=spec.doc),
                is_path=spec.is_path,
                config_option_text=spec.kind is EngineKeyKind.OPTION_TEXT,
            )
            for spec in ENGINE_KEYS
        )
    return _engine_setting_params


def split_engine_values[V](
    values: "Mapping[HostParamKey, V]",
) -> "tuple[dict[HostParamKey, V], dict[StaticBindingKey, V]]":
    """Split host parameter *values* into the non-engine ones and the engine settings'.

    An engine setting is a root ``std/config`` binding, so its key is
    ``(module, (), name)`` and the last element names the setting.
    """
    others: dict[HostParamKey, V] = {}
    engine: dict[StaticBindingKey, V] = {}
    for key, value in values.items():
        if isinstance(key, tuple) and is_engine_builtin_var_key(key):
            engine[key] = value
        else:
            others[key] = value
    return others, engine


@dataclass(frozen=True, slots=True)
class EngineSettings:
    """The engine settings a host explicitly controls, decoded, with each one's winning tier.

    A setting no source sets is absent, so its ``builtin var`` initializer applies.
    """

    values: "Mapping[str, Value]"
    ranks: "Mapping[str, ParamTier]"


def engine_setting_name(key: HostParamKey) -> str | None:
    """Return the engine setting *key* names, or ``None`` for any other host parameter."""
    return key[2] if isinstance(key, tuple) and is_engine_builtin_var_key(key) else None


def prepare_engine_config(
    config: "Mapping[StaticBindingKey, Value]", *, nominals: BuiltinNominals
) -> "tuple[dict[StaticBindingKey, Value], list[Diagnostic]]":
    """Restamp a program's ``@config`` engine values onto the standard identity and validate them.

    *config* holds the values already decoded onto *nominals*' identity. Every one is
    validated, whether or not a higher tier overrides it; returns the valid ones, then a
    diagnostic for each invalid one.
    """
    valid: dict[StaticBindingKey, Value] = {}
    diagnostics: list[Diagnostic] = []
    for key, value in config.items():
        name = key[2]
        value = restamp_engine_setting(
            name, value, from_table=nominals, to_table=NO_BUILTIN_DECLARATIONS
        )
        try:
            validate_engine_value(name, value)
        except ValueError as exc:
            diagnostics.append(Diagnostic(message=_invalid_engine_message(name, "@config", exc)))
        else:
            valid[key] = value
    return valid, diagnostics


def _invalid_engine_message(name: str, origin: str, error: ValueError) -> str:
    """Return the message for engine setting *name*'s invalid value from *origin*."""
    return f"invalid {name} value from {origin}: {error}"


def engine_setting_decoder(
    decoders: "Mapping[str, ParamDecoder]",
) -> "Callable[[HostParamKey, ParamTier, object], Value | Diagnostic]":
    """Return the decoder of an engine setting's raw winning value, for ``resolve_host_params``.

    Decodes through *decoders* (:func:`~agnette.agl.type_schema.engine_setting_decoders`)
    onto the standard identity; an invalid value is a diagnostic without a source location
    naming its origin (the flag, or the configuration key).
    """

    def decode(key: HostParamKey, tier: ParamTier, raw: object) -> "Value | Diagnostic":
        name = cast(str, engine_setting_name(key))
        try:
            return decode_engine_raw(name, raw, decoders[name])
        except ValueError as exc:
            origin = f"--{name}" if tier is ParamTier.SUPPLIED else f"configuration key {name}"
            return Diagnostic(message=_invalid_engine_message(name, origin, exc))

    return decode


def engine_settings_from[K: HostParamKey](
    decoded: "Mapping[K, tuple[ParamTier, Value]]",
) -> EngineSettings:
    """Collect the engine settings among the *decoded* host parameter values."""
    values: dict[str, Value] = {}
    ranks: dict[str, ParamTier] = {}
    for key, (tier, value) in decoded.items():
        name = engine_setting_name(key)
        if name is not None:
            values[name] = value
            ranks[name] = tier
    return EngineSettings(values, ranks)


def _reserved_default_resolver(nominal: "NominalId", field_index: int) -> "Value":
    """Fill an omitted defaulted field of a reserved record with its host-side constant.

    Passed to :func:`~agnette.agl.runtime.arguments.decode_param_value` as the
    ``default_resolver`` for every host engine-config decode
    (:func:`decode_engine_raw`). No program — and so no evaluator — exists
    yet at this boundary, unlike an ordinary program's own field default
    (filled by ``IrInterpreter.default_for_field`` against the real, fully
    linked ``NominalDescriptor`` table); a reserved record's default is
    instead a plain host-side constant in
    :data:`~agnette.agl.semantics.type_table.RESERVED_FIELD_DEFAULT_VALUES` (see
    :func:`~agnette.agl.semantics.type_table.reserved_field_default`).
    """
    from agnette.agl.semantics.type_table import reserved_field_default

    return reserved_field_default(nominal.value, field_index)


def _decode_host_value(name: str, raw: object, decoder: "ParamDecoder") -> "Value":
    """Decode a raw host value through *decoder*, raising ``ValueError`` naming setting *name*.

    Runs the shared :func:`~agnette.agl.runtime.arguments.decode_param_value` path over
    the decoder shape the lowerer embeds in the compiled IR
    (:func:`~agnette.agl.type_schema.build_param_decoder`), so host and compiled-IR value
    decoding share one mechanism. An omitted defaulted field (e.g. ``Sandbox``'s or an
    ``Agent`` member's) fills through :func:`_reserved_default_resolver`.
    """
    from agnette.agl.runtime.arguments import decode_param_value
    from agnette.agl.runtime.convert import StrictJsonParseError

    try:
        return decode_param_value(decoder, raw, default_resolver=_reserved_default_resolver)
    except (StrictJsonParseError, ValueError) as exc:
        raise ValueError(
            f"Setting {name!r}: could not parse as {decoder.target_type_label}: {exc}"
        ) from exc


def decode_engine_raw(name: str, raw: object, decoder: "ParamDecoder") -> "Value":
    """Decode and validate one engine setting's raw argument value, raising ``ValueError``.

    *raw* has the shape a program argument's does (an
    :class:`~agnette.agl.runtime.arguments.OptionSome` box or the ``None`` envelope
    for an ``Option`` setting), as the host-parameter surfaces produce it. *decoder* is the
    setting's (:func:`~agnette.agl.type_schema.engine_setting_decoders`).
    """
    value = _decode_host_value(name, raw, decoder)
    validate_engine_value(name, value)
    return value


class EngineRangeError(ValueError):
    """A decoded engine value outside its key's accepted range."""


def validate_engine_value(
    name: str, value: "Value", *, nominals: BuiltinNominals = NO_BUILTIN_DECLARATIONS
) -> None:
    """Check one decoded engine setting *value*, raising ``ValueError`` when it is invalid.

    The one post-decode rule shared by host decoding (CLI, config tables,
    manifest, ``@config``) and a runtime source write: a present
    ``Option[text]`` must not be blank, ``timeout`` must parse as a duration
    (:func:`~agnette.core.parse.parse_timeout`), and a non-negative key must not be
    negative (:class:`EngineRangeError`). *nominals* is the table that stamped
    *value*'s ``Option`` identity.
    """
    if _ENGINE_KEY_KINDS_BY_NAME.get(name) is EngineKeyKind.OPTION_TEXT and isinstance(
        value, RecordValue
    ):
        from agnette.agl.runtime.option import option_text
        from agnette.core.parse import parse_timeout

        text = option_text(value, nominals=nominals)
        if text is not None and not text.strip():
            raise ValueError(f"Setting {name!r}: value must not be blank.")
        if text is not None and name == "timeout":
            parse_timeout(text)
    if name in NON_NEGATIVE_ENGINE_KEYS and isinstance(value, IntValue) and value.value < 0:
        raise EngineRangeError(f"setting {name!r} must not be negative, got {value.value}")
