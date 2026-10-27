"""Host engine-config decoding helpers (CLI flags, config-file entries)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from agnette.agent.spec import AGENT_SPECS
from agnette.agl.ir.builtin_nominals import NO_BUILTIN_DECLARATIONS, BuiltinNominals
from agnette.agl.ir.builtin_vars import is_engine_builtin_var_key
from agnette.agl.ir.reserved_nominals import AGENT_SANDBOX_MEMBERS
from agnette.agl.ir.static_keys import HostParamKey, StaticBindingKey
from agnette.agl.runtime.arguments import ParamTier
from agnette.agl.semantics.values import IntValue, RecordValue
from agnette.config.engine_keys import ENGINE_KEYS, NON_NEGATIVE_ENGINE_KEYS, EngineKeyKind
from agnette.core.parse import normalize_option_text

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agnette.agl.ir.ids import NominalId
    from agnette.agl.runtime.types import HostParamInfo
    from agnette.agl.semantics.type_table import TypeTable
    from agnette.agl.semantics.types import Type as AglType
    from agnette.agl.semantics.values import Value

__all__ = [
    "EngineRangeError",
    "EngineSettings",
    "EngineValueFailure",
    "decode_engine_raw",
    "engine_default_settings",
    "engine_setting_params",
    "resolve_engine_settings",
    "restamp_engine_setting",
    "split_engine_values",
    "validate_engine_value",
    "validate_manifest_leaf_value",
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


def build_engine_config_seeds(raw_values: "Mapping[str, object]") -> "dict[str, Value]":
    """Decode explicitly supplied scalar, ``Option``, or ``Agent`` host engine settings.

    The returned mapping deliberately omits absent keys. This preserves the
    distinction between a host control and the runtime fallback, letting a
    ``builtin var`` initializer supply the latter.  A present value of
    ``None`` remains meaningful for ``Option`` settings such as ``timeout``.
    """
    from agnette.agl.semantics.engine_keys import ENGINE_KEY_TYPES
    from agnette.agl.semantics.type_table import create_seeded_type_table

    type_table = create_seeded_type_table()
    return {
        key_name: convert_config_value(key_name, raw, ENGINE_KEY_TYPES[key_name], type_table)
        for key_name, raw in raw_values.items()
    }


def engine_default_settings() -> "dict[str, Value]":
    """Build the typed engine-default value for every defaulted scalar engine key.

    Derives the raw values from the shared engine-key catalog, then decodes
    them via :func:`convert_config_value`, building its own fresh seeded
    ``TypeTable`` rather than requiring one from the caller.

    ``agent`` is an ``Agent`` value rather than a scalar one and has no
    host-side default: it comes from the ``std/config`` ``builtin var``
    declaration like any other declared default.
    """
    return build_engine_config_seeds(
        {spec.name: spec.default for spec in ENGINE_KEYS if spec.has_default}
    )


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
class EngineValueFailure:
    """An invalid engine setting value, which has no source location to report at."""

    name: str
    message: str


@dataclass(frozen=True, slots=True)
class EngineSettings:
    """The engine settings a host explicitly controls, decoded, with each one's winning tier.

    A setting no source sets is absent, so its ``builtin var`` initializer applies.
    """

    values: "Mapping[str, Value]"
    ranks: "Mapping[str, ParamTier]"


def _engine_origin(name: str, *, supplied: bool) -> str:
    """Name where engine setting *name*'s value came from: its flag or its configuration key."""
    return f"--{name}" if supplied else f"configuration key {name}"


def _invalid_engine_message(name: str, origin: str, error: ValueError) -> str:
    """Return the message for engine setting *name*'s invalid value from *origin*."""
    return f"invalid {name} value from {origin}: {error}"


def resolve_engine_settings(
    tiers: "Mapping[ParamTier, Mapping[StaticBindingKey, object]]",
    config: "Mapping[StaticBindingKey, Value]",
    *,
    nominals: BuiltinNominals,
) -> "tuple[EngineSettings, list[EngineValueFailure], list[EngineValueFailure]]":
    """Rank the engine setting sources and decode each setting's winning value.

    *tiers* hold raw values by engine setting key, one mapping per :class:`ParamTier` (none
    for ``INHERITED``: an engine setting takes no ``@inherit``), in the
    shape :func:`~agnette.agl.runtime.arguments.decode_param_value` takes. *config* holds the
    program's ``@config`` values, already decoded onto *nominals*' identity; they rank
    :attr:`ParamTier.CONFIG` and are validated whether or not a higher tier overrides them.
    Only the winning raw value of any other tier is decoded, onto the standard identity.

    Returns the settings, then the failures of invalid configured values, then those of
    invalid :attr:`ParamTier.SUPPLIED` values (usage errors).
    """
    from agnette.agl.semantics.engine_keys import ENGINE_KEY_TYPES
    from agnette.agl.semantics.type_table import create_seeded_type_table

    configured_failures: list[EngineValueFailure] = []
    usage_failures: list[EngineValueFailure] = []
    from_config: dict[StaticBindingKey, object] = {}
    for key, value in config.items():
        name = key[2]
        value = restamp_engine_setting(
            name, value, from_table=nominals, to_table=NO_BUILTIN_DECLARATIONS
        )
        try:
            validate_engine_value(name, value)
        except ValueError as exc:
            configured_failures.append(
                EngineValueFailure(name, _invalid_engine_message(name, "@config", exc))
            )
        else:
            from_config[key] = value

    by_tier = {**tiers, ParamTier.CONFIG: from_config}
    winners: dict[StaticBindingKey, tuple[ParamTier, object]] = {}
    for tier in ParamTier:
        winners.update({key: (tier, raw) for key, raw in by_tier.get(tier, {}).items()})

    values: dict[str, Value] = {}
    ranks: dict[str, ParamTier] = {}
    type_table = create_seeded_type_table()
    for key, (tier, raw) in winners.items():
        name = key[2]
        if tier is ParamTier.CONFIG:
            values[name] = cast("Value", raw)
        else:
            try:
                values[name] = decode_engine_raw(name, raw, ENGINE_KEY_TYPES[name], type_table)
            except ValueError as exc:
                is_supplied = tier is ParamTier.SUPPLIED
                origin = _engine_origin(name, supplied=is_supplied)
                failure = EngineValueFailure(name, _invalid_engine_message(name, origin, exc))
                (usage_failures if is_supplied else configured_failures).append(failure)
                continue
        ranks[name] = tier
    return EngineSettings(values, ranks), configured_failures, usage_failures


def _reserved_default_resolver(nominal: "NominalId", field_index: int) -> "Value":
    """Fill an omitted defaulted field of a reserved record with its host-side constant.

    Passed to :func:`~agnette.agl.runtime.arguments.decode_param_value` as the
    ``default_resolver`` for every host engine-config decode
    (:func:`convert_host_value`). No program — and so no evaluator — exists
    yet at this boundary, unlike an ordinary program's own field default
    (filled by ``IrInterpreter.default_for_field`` against the real, fully
    linked ``NominalDescriptor`` table); a reserved record's default is
    instead a plain host-side constant in
    :data:`~agnette.agl.semantics.type_table.RESERVED_FIELD_DEFAULT_VALUES` (see
    :func:`~agnette.agl.semantics.type_table.reserved_field_default`).
    """
    from agnette.agl.semantics.type_table import reserved_field_default

    return reserved_field_default(nominal.value, field_index)


def convert_host_value(
    name: str, raw: object, type_obj: AglType, type_table: "TypeTable"
) -> "Value":
    """Decode a raw host value against a declared AgL type.

    Builds the same :class:`~agnette.agl.ir.contracts.ParamDecoder` the lowerer
    embeds in the compiled IR (via :func:`~agnette.agl.type_schema.build_param_decoder`)
    and runs the shared :func:`~agnette.agl.runtime.arguments.decode_param_value`
    path, so this and the compiled-IR value-decode boundary go through one
    mechanism. :func:`convert_config_value` and :func:`decode_engine_raw` decode
    host engine-setting values (CLI flags, config-file entries) through it.

    ``text`` values are taken verbatim; a JSON-compatible Python value (not a
    string) crosses the canonical JSON boundary, its nested strings read as a
    raw string is. A raw string is read through the shared host-text dispatch
    (:func:`~agnette.agl.runtime.value_decode.host_param_text_to_json`): strict
    JSON when it decodes into the slot, else one AgL value-syntax literal — no repair of user typos
    either way. *raw* may instead be an
    :class:`~agnette.agl.runtime.arguments.OptionSome` box, for an ``Option[T]``
    *type_obj*: the boxed payload decodes against ``T``'s own field schema and
    is wrapped into the enum's ``Some`` shape, exactly as a program's own
    ``Option[T]`` parameter decodes. *type_obj* is a checked engine-setting
    type with a wire schema; the builtin ``Agent`` enum's schema is dispatched
    through its own shorthand and constructor-call reading. *type_table*
    resolves record/enum field/variant shapes for *type_obj*. An omitted defaulted field (e.g.
    ``Sandbox``'s or an ``Agent`` member's) fills through
    :func:`_reserved_default_resolver`.
    """
    from agnette.agl.runtime.arguments import decode_param_value
    from agnette.agl.runtime.convert import StrictJsonParseError
    from agnette.agl.type_schema import build_param_decoder

    decoder = build_param_decoder(type_obj, type_table)
    try:
        return decode_param_value(decoder, raw, default_resolver=_reserved_default_resolver)
    except (StrictJsonParseError, ValueError) as exc:
        raise ValueError(f"Setting {name!r}: could not parse as {type_obj!r}: {exc}") from exc


def decode_engine_raw(
    name: str, raw: object, key_type: AglType, type_table: "TypeTable"
) -> "Value":
    """Decode and validate one engine setting's raw argument value, raising ``ValueError``.

    *raw* has the shape a program argument's does (an
    :class:`~agnette.agl.runtime.arguments.OptionSome` box or the ``None`` envelope
    for an ``Option`` setting), as the host-parameter surfaces produce it.
    """
    value = convert_host_value(name, raw, key_type, type_table)
    validate_engine_value(name, value)
    return value


def convert_config_value(
    name: str, raw: object, key_type: AglType, type_table: "TypeTable | None" = None
) -> "Value":
    """Convert a raw scalar, ``Option``, or ``Agent`` host engine value to its AgL type.

    ``agent`` is an ``Agent`` value, not a scalar or ``Option`` setting;
    its host-supplied text or JSON-shaped data (``--agent``/config)
    decodes through :func:`convert_host_value` exactly like every other key.
    For ``Option[T]`` engine keys (``timeout``, ``trace-file``) a present *raw*
    is boxed as an :class:`~agnette.agl.runtime.arguments.OptionSome` and decoded
    through :func:`convert_host_value` against the *whole* ``Option[T]``
    type — the same deferred-decode path a program's own ``Option[T]``
    parameter uses — and an absent (``None``) *raw* is ``none`` directly.
    Non-Option keys decode directly through :func:`convert_host_value`.
    *type_table* is threaded through to it.

    The settings accepted here are built-in scalar, ``Option[text]``, or the
    builtin ``Agent`` enum, never user-declared nominal types, so *type_table*
    defaults to a fresh
    seeded ``TypeTable`` when the caller has none in hand (e.g. CLI-flag config
    projection); callers that already hold the session/program table (the
    REPL) pass it explicitly.
    """
    from agnette.agl.ir.builtin_nominals import NO_BUILTIN_DECLARATIONS
    from agnette.agl.runtime.arguments import OptionSome
    from agnette.agl.runtime.option import none_value
    from agnette.agl.semantics.type_table import create_seeded_type_table
    from agnette.agl.semantics.types import is_standard_option_enum

    table = type_table if type_table is not None else create_seeded_type_table()
    if is_standard_option_enum(key_type):
        if raw is None:
            return none_value(nominals=NO_BUILTIN_DECLARATIONS)
        return convert_host_value(name, OptionSome(raw), key_type, table)
    return convert_host_value(name, raw, key_type, table)


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


def validate_manifest_leaf_value(name: str, raw: object, type_table: "TypeTable") -> "Value":
    """Decode one manifest ``[config]`` engine leaf, raising ``ValueError`` on any failure.

    An ``Option[text]``-kind key is first normalized by
    :func:`normalize_option_text`, as a config-file entry is: positive
    numbers become their string spelling, a blank or non-positive value is absent.
    A value of any other type (bool, array, table) is an error, not absence.
    The result is decoded through :func:`convert_config_value` (``None`` is an empty
    ``Option``) against ``ENGINE_KEY_TYPES`` and checked with :func:`validate_engine_value`.
    """
    from agnette.agl.semantics.engine_keys import ENGINE_KEY_TYPES

    if _ENGINE_KEY_KINDS_BY_NAME.get(name) is EngineKeyKind.OPTION_TEXT:
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
            raise ValueError(f"Setting {name!r}: expected a string or number, got {raw!r}.")
        raw = normalize_option_text(raw)
    value = convert_config_value(name, raw, ENGINE_KEY_TYPES[name], type_table)
    validate_engine_value(name, value)
    return value
