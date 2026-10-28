"""Runtime program-argument binding: host-supplied raw values -> checked ``Value``s.

A program's arguments are host parameters like any
other (:mod:`agnette.agl.runtime.param_resolution` ranks and decodes them):
:func:`bind_positional_arguments` maps the positional ones onto parameter names
through the shared zone binder (``agnette.agl.semantics.arguments``),
:func:`decode_program_argument` and :func:`decode_host_param` decode a raw value
through its ``ParamDecoder``, and :func:`complete_program_arguments` fills the
remaining defaults and reports the missing required ones. Every violation is a
pre-execution :class:`~agnette.agl.diagnostics.Diagnostic`, anchored at the offending
parameter's declaration span — or the program's own span when no single
parameter is at fault (an excess or misdirected positional argument, an unknown name).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, assert_never, cast

from agnette.agl.diagnostics import Diagnostic, diagnostic_from_span
from agnette.agl.ir.nodes import UseDefault
from agnette.agl.ir.static_keys import (
    HostParamKey,
    host_param_label,
)
from agnette.agl.semantics.arguments import (
    ArgumentBindingError,
    ArgumentBindingErrorKind,
    BindParam,
    bind_arguments,
)
from agnette.util.unicode import require_scalar_text, surrogate_index, visible_text

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Set as AbstractSet

    from agnette.agl.ir.contracts import DecodeSchema, ParamDecoder
    from agnette.agl.ir.program import ExecutableProgram, IrProgramParam
    from agnette.agl.runtime.convert import DefaultResolver
    from agnette.agl.runtime.types import ProgramDeclInfo, ProgramParamInfo
    from agnette.agl.semantics.values import Value
    from agnette.agl.syntax.spans import SourceSpan
    from agnette.agl.zones import ParamZone

__all__ = [
    "InheritedParamValue",
    "OptionSome",
    "ParamTier",
    "bind_positional_arguments",
    "complete_program_arguments",
    "decode_host_param",
    "decode_param_value",
    "decode_program_argument",
    "default_program_arguments",
    "diagnose_process_environment",
    "duplicate_argument_diagnostic",
    "option_none_raw",
    "positional_only_by_name_diagnostic",
    "program_signature",
    "unknown_argument_diagnostic",
]


def diagnose_process_environment(
    environment: "Mapping[str, str] | None",
) -> Diagnostic | None:
    """Return a diagnostic for the first environment entry that is not valid Unicode.

    The host decodes the process environment with ``surrogateescape``, so a
    name or value can carry a surrogate that AgL ``text`` must never hold. It
    is reported here, before execution, rather than at whichever interpolation
    hole or ``std/env`` read first encodes it. Both the value and the name are
    rendered with :func:`visible_text`, so building the diagnostic cannot fail
    in turn.
    """
    if environment is None:
        return None
    for name, value in environment.items():
        if surrogate_index(name) is not None or surrogate_index(value) is not None:
            return Diagnostic(
                message=(
                    f"environment variable {visible_text(name)} is not "
                    f"valid UTF-8: '{visible_text(value)}'"
                )
            )
    return None


def _missing_required_message(name: str) -> str:
    """The diagnostic message for a missing required program argument.

    The sole place this wording is generated: every diagnostic that reports
    a missing required program argument, however it is reached, calls this.
    """
    return f"Missing required program argument: {name!r}"


class ParamTier(enum.IntEnum):
    """The sources a host parameter's value comes from, lowest precedence first."""

    #: The declaration route and a package manifest's ``[config]``.
    DECLARED = 0
    #: The selected program's own ``@config`` entries.
    CONFIG = 1
    #: An ``@inherit`` address.
    INHERITED = 2
    #: The selected program's route in the config tables.
    CONFIGURED = 3
    #: The command line or environment.
    SUPPLIED = 4


@dataclass(frozen=True, slots=True)
class InheritedParamValue:
    """A raw host value a parameter inherited, with the ``@inherit`` address it came from."""

    value: object
    address: str


def _inherited_note(address: str | None) -> str:
    """Return the diagnostic suffix naming the ``@inherit`` *address* a value came from."""
    return "" if address is None else f" (inherited from '{address}')"


@dataclass(frozen=True, slots=True)
class ProgramParameter:
    """One ``program def`` value parameter, fused for binding and diagnostics.

    ``name``, ``kind``, and ``has_default`` drive zone binding; ``span`` is
    its declaration span, the anchor for a diagnostic naming this parameter; ``decoder`` decodes one
    raw host-supplied value into this parameter's checked type.
    """

    name: str
    kind: "ParamZone"
    has_default: bool
    span: "SourceSpan"
    decoder: "ParamDecoder"


@dataclass(frozen=True, slots=True)
class ProgramSignature:
    """A program's parameter signature, ready for binding and diagnostics.

    ``span`` anchors a diagnostic that names no single parameter (an unknown
    name, or an excess/misdirected positional argument). Build with
    :meth:`fuse` from the two independent descriptions a host has on hand —
    never pair ``IrProgramParam``/``ProgramParamInfo`` tuples by hand.
    """

    parameters: "tuple[ProgramParameter, ...]"
    span: "SourceSpan"

    @classmethod
    def fuse(
        cls,
        params: "tuple[IrProgramParam, ...]",
        infos: "tuple[ProgramParamInfo, ...]",
        span: "SourceSpan",
    ) -> "ProgramSignature":
        """Pair each executable parameter with its declaration info, by name.

        *params* (an executable's per-program decoders) and *infos* (a
        program declaration's spans and types) describe the same parameters
        independently. Pairing them by name — rather than assuming matching
        order — means a reordering between the two sources can never produce
        a wrong pairing. Both are built from the same declaration, so a name
        present in one and absent from the other is a compiler bug.
        """
        infos_by_name = {info.name: info for info in infos}
        parameters = []
        for param in params:
            info = infos_by_name[param.name]
            parameters.append(
                ProgramParameter(
                    name=param.name,
                    kind=param.kind,
                    has_default=info.has_default,
                    span=info.span,
                    decoder=param.external_decoder,
                )
            )
        return cls(parameters=tuple(parameters), span=span)


def option_none_raw() -> object:
    """Return the ``None`` envelope :func:`decode_param_value` takes for an ``Option[T]``."""
    return {"$case": "None"}


@dataclass(frozen=True, slots=True)
class OptionSome:
    """A host-supplied optional enum's ``Some`` raw payload, awaiting decode.

    Boxes the raw ``VALUE`` a CLI flag or config table supplied for an
    ``Option[T]`` or ``Optional[T]`` parameter
    (:func:`~agnette.host_params.projection.token_raw_value` and
    ``native_raw_value`` build it) so :func:`decode_param_value` can decode ``value`` against the
    ``Some`` variant's own field type before wrapping it back into the enum's
    JSON shape — a string is read through the same host-text dispatch as any
    other textual value; a native value is already decoded data.
    """

    value: object


def decode_param_value(
    decoder: "ParamDecoder", raw: object, *, default_resolver: "DefaultResolver | None" = None
) -> "Value":
    """Decode a raw host param value against *decoder* into a typed ``Value``.

    The single decode path shared by program-argument and parameter decoding
    (:func:`decode_program_argument`, :func:`decode_host_param`) and the host
    engine-config decode path
    (``runtime.engine_config.convert_host_value``). A textual value is read
    through the shared host-text dispatch
    (``runtime.value_decode.host_param_text_to_json``): ``text`` params
    verbatim, the standard ``Agent`` enum through its own text conventions, a
    plain enum member's bare JSON name as itself, everything else as strict
    JSON when it decodes into the slot, else AgL value syntax. An :class:`OptionSome` payload
    decodes its own ``value`` the same way (when textual) before being wrapped
    back into the optional enum's JSON shape. A native value's nested strings
    are read the same way for their own slots. Every value then crosses the
    canonical JSON boundary (JSON-Schema validation, then the typeless
    ``decode_value`` walk).
    *default_resolver*, when given, fills an omitted defaulted field nested in
    *raw* (see ``runtime.convert.decode_value``); omitted, such a field is an
    ordinary missing-field error. The host engine-config decode path supplies
    its own resolver, reading a reserved record's field defaults as host-side
    constants rather than evaluating IR (no program exists yet there) — see
    ``runtime.engine_config.convert_host_value``.

    :raises ValueError: on a type/shape mismatch, schema-validation failure, or a raw
        string that is not valid Unicode (:class:`~agnette.util.unicode.LoneSurrogateError`).
    """
    from agnette.agl.runtime.convert import (
        _clean_validation_message,
        decode_value,
        parse_json_strict,
        validator_for_schema,
    )
    from agnette.agl.runtime.serialize import JsonShaped, dumps_exact
    from agnette.agl.runtime.value_decode import (
        host_data_to_json,
        host_param_text_to_json,
        option_some_field_schema,
        option_some_json_name,
    )

    defs = dict(decoder.defs)

    def native_to_json(value: object, schema: "DecodeSchema") -> object:
        """Cross an already-native (non-string) host value into JSON-native form.

        Round-trips through the same strict-parse boundary a textual value's
        JSON branch uses, so a native ``float`` becomes an exact ``Decimal``,
        and a value with no JSON shape (a TOML datetime, say) or a non-finite
        number reports a clean error instead of reaching JSON-Schema
        validation as a foreign type. A string nested in it is then read as
        *schema*'s slot reads host text, so a string means the same at every
        depth. Shared by the top-level native branch and an ``OptionSome``
        payload's non-string inner value, so neither can drift from the other.
        """
        if not _is_json_shaped(value):
            raise ValueError(f"expected a JSON-compatible value, got {type(value).__name__}")
        return host_data_to_json(
            parse_json_strict(dumps_exact(cast(JsonShaped, value), indent=None)), schema, defs
        )

    if isinstance(raw, OptionSome):
        inner = raw.value
        field_schema = option_some_field_schema(decoder.decode, defs)
        inner_obj = (
            host_param_text_to_json(require_scalar_text(inner), field_schema, defs)
            if isinstance(inner, str)
            else native_to_json(inner, field_schema)
        )
        obj: object = {"$case": option_some_json_name(decoder.decode, defs), "value": inner_obj}
    elif isinstance(raw, str):
        obj = host_param_text_to_json(require_scalar_text(raw), decoder.decode, defs)
    else:
        obj = native_to_json(raw, decoder.decode)
    validation_errors = list(validator_for_schema(decoder.json_schema).iter_errors(obj))
    if validation_errors:
        raise ValueError(_clean_validation_message(validation_errors[0]))
    return decode_value(decoder.decode, obj, defs, default_resolver=default_resolver)


@dataclass(frozen=True, slots=True)
class _Supplied:
    """Box a raw host argument so ``None`` keeps meaning "not supplied".

    ``bind_arguments`` is generic over an opaque item type and returns
    ``None`` for a parameter slot the caller must default; a host may
    legitimately *supply* ``None`` itself (JSON ``null`` for a ``json``-typed
    parameter), so every raw value is boxed before it enters the pure binder
    and unboxed on the way out.  An absent ``Option[T]`` is not such a case:
    it crosses as the ``{"$case": "None"}`` envelope, never a bare ``None``.
    """

    value: object


def program_signature(
    executable: "ExecutableProgram", program: "ProgramDeclInfo"
) -> ProgramSignature:
    """Fuse *program*'s declaration info with *executable*'s signature.

    *program* and *executable* must describe the same selected program.
    """
    program_symbol = executable.program_symbols[program.node_id]
    return ProgramSignature.fuse(
        executable.program_signatures[program_symbol], program.parameters, program.span
    )


def bind_positional_arguments(
    signature: ProgramSignature, positional: "tuple[object, ...]"
) -> "tuple[dict[str, object], tuple[Diagnostic, ...]]":
    """Map *positional* raw values onto *signature*'s parameter names, in zone order.

    The shared zone binder is positional-greedy; a host supplies plain values rather
    than AST expressions, so no item is ever treated as a bare name. Returns the raw
    value of each positionally bound parameter, or one diagnostic for an excess or
    misdirected positional argument.
    """
    bind_params = [
        BindParam(name=p.name, kind=p.kind, has_default=True) for p in signature.parameters
    ]
    try:
        bound = bind_arguments(bind_params, [_Supplied(value) for value in positional], [])
    except ArgumentBindingError as exc:
        return {}, (_diagnose_binding_error(exc, signature),)
    return {
        param.name: box.value
        for param, box in zip(signature.parameters, bound, strict=True)
        if box is not None
    }, ()


def decode_program_argument(
    param: ProgramParameter,
    raw: object,
    *,
    default_resolver: "DefaultResolver | None" = None,
    inherited_address: str | None = None,
) -> "Value | Diagnostic":
    """Decode *raw* against *param*'s decoder; a failure is a diagnostic at its declaration.

    *default_resolver*, when given, fills an omitted defaulted field nested in *raw*
    (see :func:`decode_param_value`); *inherited_address* names the ``@inherit``
    address *raw* came from.
    """
    from agnette.agl.runtime.convert import StrictJsonParseError

    try:
        return decode_param_value(param.decoder, raw, default_resolver=default_resolver)
    except (StrictJsonParseError, ValueError) as exc:
        return diagnostic_from_span(
            f"Program argument {param.name!r}: could not parse as "
            f"{param.decoder.target_type_label}: {exc}{_inherited_note(inherited_address)}",
            param.span,
        )


def unknown_argument_diagnostic(signature: ProgramSignature, name: str) -> Diagnostic:
    """Return the diagnostic for a value of a program argument *signature* does not declare."""
    return diagnostic_from_span(f"Unknown program argument: {name!r}", signature.span)


def duplicate_argument_diagnostic(param: ProgramParameter) -> Diagnostic:
    """Return the diagnostic for the argument *param* given both positionally and by name."""
    return diagnostic_from_span(
        f"Program argument {param.name!r} supplied more than once", param.span
    )


def positional_only_by_name_diagnostic(param: ProgramParameter) -> Diagnostic:
    """Return the diagnostic for a value of the positional-only *param* supplied by name."""
    return diagnostic_from_span(
        f"Program argument {param.name!r} is positional-only and cannot be supplied by name",
        param.span,
    )


def complete_program_arguments(
    signature: ProgramSignature,
    values: "Mapping[str, Value]",
    *,
    failed: "AbstractSet[str]" = frozenset(),
) -> "tuple[tuple[Value | UseDefault, ...], tuple[Diagnostic, ...]]":
    """Return one entry per parameter, in declaration order, from the decoded *values*.

    A parameter *values* lacks takes ``UseDefault`` when it has a default and is
    otherwise reported missing, every missing required one in a single pass; a name in
    *failed* already has a decode diagnostic and is skipped. Returns ``(entries, ())``
    ready for ``IrInterpreter.run``'s ``arguments`` — a zero-parameter program returns
    ``((), ())``, so a caller must key success off the diagnostics tuple — or
    ``((), diagnostics)``.
    """
    entries: "list[Value | UseDefault]" = []
    diagnostics: list[Diagnostic] = []
    for index, param in enumerate(signature.parameters):
        if param.name in values:
            entries.append(values[param.name])
        elif param.name in failed:
            continue
        elif param.has_default:
            entries.append(UseDefault(param_index=index))
        else:
            diagnostics.append(
                diagnostic_from_span(_missing_required_message(param.name), param.span)
            )
    if diagnostics or failed:
        return (), tuple(diagnostics)
    return tuple(entries), ()


def decode_host_param(
    executable: "ExecutableProgram",
    key: HostParamKey,
    raw: object,
    *,
    default_resolver: "DefaultResolver | None" = None,
    inherited_address: str | None = None,
) -> "Value | Diagnostic":
    """Decode a module- or function-parameter value *raw* against *executable*'s tables.

    An unknown *key* or a malformed value is a diagnostic (the latter at the parameter's
    declaration). *default_resolver*, when given, fills a defaulted field a value omits
    (built by the caller over *executable*'s own real nominal table); a failure of a
    value from an ``@inherit`` address names *inherited_address*.
    """
    from agnette.agl.runtime.convert import StrictJsonParseError

    decoder = executable.param_decoders.get(key)
    if decoder is None:
        return Diagnostic(message=f"Unknown parameter: {host_param_label(key)}")
    try:
        return decode_param_value(decoder, raw, default_resolver=default_resolver)
    except (StrictJsonParseError, ValueError) as exc:
        message = (
            f"Parameter {host_param_label(key)}: "
            f"could not parse as {decoder.target_type_label}: {exc}"
            f"{_inherited_note(inherited_address)}"
        )
        return diagnostic_from_span(message, cast("SourceSpan", executable.param_spans[key]))


def default_program_arguments(
    signature: "tuple[IrProgramParam, ...]",
) -> "tuple[tuple[Value | UseDefault, ...], tuple[Diagnostic, ...]]":
    """Derive an argument list for a program invoked with no host-supplied values.

    Used when a caller runs a selected program without going through
    :func:`complete_program_arguments` — no argument value was ever
    collected, only the lowered signature is on hand. Every parameter defers
    to its own default (``UseDefault``); a parameter with none is reported
    with the same message :func:`complete_program_arguments` uses for an
    explicitly missing argument, without a location since no declaration span
    is reachable from an ``IrProgramParam`` alone.

    Returns ``(values, ())`` on success — one entry per parameter, in
    declaration order — or ``((), diagnostics)``, one diagnostic per missing
    required parameter.
    """
    diagnostics = tuple(
        Diagnostic(message=_missing_required_message(param.name))
        for param in signature
        if param.required
    )
    if diagnostics:
        return (), diagnostics
    return tuple(UseDefault(param_index=i) for i in range(len(signature))), ()


#: :func:`bind_positional_arguments` binds positional values only, with every parameter
#: defaulted, so only the two positional-territory violations can occur.
type StructuralBindingErrorKind = Literal[
    ArgumentBindingErrorKind.TOO_MANY_POSITIONAL,
    ArgumentBindingErrorKind.POSITIONAL_IN_NAMED_ONLY,
]


def _diagnose_binding_error(exc: ArgumentBindingError, signature: ProgramSignature) -> Diagnostic:
    """Translate one positional zone-binding violation into a pre-execution diagnostic."""
    kind = cast(StructuralBindingErrorKind, exc.kind)
    match kind:
        case ArgumentBindingErrorKind.TOO_MANY_POSITIONAL:
            return diagnostic_from_span("Too many positional program arguments", signature.span)
        case ArgumentBindingErrorKind.POSITIONAL_IN_NAMED_ONLY:
            return diagnostic_from_span(
                "Remaining program arguments must be supplied by name", signature.span
            )
        case _ as unreachable:  # pragma: no cover
            assert_never(unreachable)


def _is_json_shaped(obj: object) -> bool:
    """Return ``True`` iff *obj* is a JSON-compatible Python value.

    The closed set: ``None``, ``bool``, ``int``, a finite ``float`` or
    ``decimal.Decimal``, ``str``, ``list`` (elements recursively JSON-shaped),
    and ``dict`` (str keys, values recursively JSON-shaped).

    Used by :func:`decode_param_value` to detect non-JSON-shaped host objects
    (e.g. sets or custom classes) before attempting serialisation, so the
    caller can emit a clean diagnostic instead of a cryptic traceback.
    """
    import decimal as _decimal_mod
    import math

    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, _decimal_mod.Decimal):
        return obj.is_finite()
    if obj is None or isinstance(obj, (bool, int, str)):
        return True
    if isinstance(obj, list):
        return all(_is_json_shaped(e) for e in obj)
    if isinstance(obj, dict):
        return all(isinstance(k, str) and _is_json_shaped(v) for k, v in obj.items())
    return False
