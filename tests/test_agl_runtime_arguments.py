"""Tests for ``agnette.agl.runtime.arguments``: host program-argument binding.

Covers ``ProgramSignature.fuse`` (pairing an executable's per-parameter
decoders with a declaration's spans/types, by name), the argument primitives
(``bind_positional_arguments``, ``decode_program_argument``,
``complete_program_arguments``: diagnostics for every binding/decode failure and
missing argument), and the ``decode_param_value`` boundary it
shares with the rest of the runtime.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agnette.agl.attributes import ProgramOptionSpec
from agnette.agl.diagnostics import Diagnostic
from agnette.agl.ir.builtin_nominals import NO_BUILTIN_DECLARATIONS
from agnette.agl.ir.contracts import ParamDecoder
from agnette.agl.ir.ids import NominalId
from agnette.agl.ir.nodes import UseDefault
from agnette.agl.ir.program import IrProgramParam
from agnette.agl.ir.reserved_nominals import (
    require_reserved_enum_member_id,
    require_reserved_nominal_id,
)
from agnette.agl.modules.ids import RESERVED_ID
from agnette.agl.runtime.arguments import (
    OptionSome,
    ProgramParameter,
    ProgramSignature,
    bind_positional_arguments,
    complete_program_arguments,
    decode_param_value,
    decode_program_argument,
    duplicate_argument_diagnostic,
    positional_only_by_name_diagnostic,
    unknown_argument_diagnostic,
)
from agnette.agl.runtime.option import some_value
from agnette.agl.runtime.types import ProgramParamInfo
from agnette.agl.semantics.type_table import TypeTable, create_seeded_type_table
from agnette.agl.semantics.types import (
    BUILTIN_PRELUDE_TYPES,
    BoolType,
    DecimalType,
    EnumType,
    IntType,
    JsonType,
    TextType,
)
from agnette.agl.semantics.types import Type as AglType
from agnette.agl.semantics.values import (
    BoolValue,
    DecimalValue,
    IntValue,
    JsonValue,
    RecordValue,
    TextValue,
    Value,
)
from agnette.agl.syntax.spans import SourceSpan
from agnette.agl.type_schema import build_param_decoder
from agnette.agl.zones import ParamZone
from tests.agl.module_graph import resolve_and_check_repl_entry

_TABLE = create_seeded_type_table()

# The program span is kept far from every parameter's declaration span, so a
# test that asserts a specific line number actually distinguishes which
# anchor was used.
_PROGRAM_LINE = 99


def _span(line: int) -> SourceSpan:
    return SourceSpan(line, 1, line, 2, 0, 1)


def _decoder(typ: AglType) -> ParamDecoder:
    return build_param_decoder(typ, _TABLE)


def _option_type(inner: AglType) -> EnumType:
    return EnumType(
        name="Option",
        type_args=(inner,),
        module_id=RESERVED_ID,
        decl_id=require_reserved_nominal_id("Option"),
    )


def _record_point_type() -> "tuple[AglType, TypeTable]":
    """A real typechecked ``record Point\\n  x: int`` type, for value-syntax decode tests."""
    checked = resolve_and_check_repl_entry("record Point\n  x: int\nlet p: Point = Point(x = 1)\np")
    last = checked.resolved.program.body.items[-1]
    return checked.node_types[last.node_id], checked.type_env.type_table


def _param_and_info(
    name: str,
    kind: ParamZone,
    typ: AglType,
    *,
    required: bool,
    line: int,
) -> tuple[IrProgramParam, ProgramParamInfo]:
    decoder = _decoder(typ)
    param = IrProgramParam(name=name, kind=kind, required=required, external_decoder=decoder)
    info = ProgramParamInfo(
        name=name,
        kind=kind,
        type=typ,
        has_default=not required,
        span=_span(line),
        cli=ProgramOptionSpec(name=name),
    )
    return param, info


def _signature(*entries: tuple[IrProgramParam, ProgramParamInfo]) -> ProgramSignature:
    return ProgramSignature.fuse(
        params=tuple(e[0] for e in entries),
        infos=tuple(e[1] for e in entries),
        span=_span(_PROGRAM_LINE),
    )


class TestProgramSignatureFuse:
    """``ProgramSignature.fuse`` pairs parameters by name, not by position."""

    def test_pairs_by_name_even_when_orders_differ(self) -> None:
        name_param, name_info = _param_and_info(
            "name", ParamZone.POSITIONAL_ONLY, TextType(), required=True, line=1
        )
        count_param, count_info = _param_and_info(
            "count", ParamZone.NAMED_ONLY, IntType(), required=True, line=2
        )
        # The executable's decoder tuple and the declaration's info tuple
        # arrive in different orders (as they would if built independently) —
        # `fuse` must still pair each decoder with its own parameter's span
        # rather than the parameter at the same position.
        signature = ProgramSignature.fuse(
            params=(count_param, name_param),
            infos=(name_info, count_info),
            span=_span(_PROGRAM_LINE),
        )
        by_name = {p.name: p for p in signature.parameters}
        assert by_name["name"].span == name_info.span
        assert by_name["count"].span == count_info.span

        # The decode failure for "count" must be anchored at count's own
        # span (line 2), not name's (line 1) — proving the pairing is by
        # name, not position.
        failure = decode_program_argument(by_name["count"], "not-an-int")
        assert isinstance(failure, Diagnostic)
        assert failure.line == 2


def _param(
    name: str, kind: ParamZone, typ: AglType, *, required: bool, line: int = 1
) -> ProgramParameter:
    return _signature(_param_and_info(name, kind, typ, required=required, line=line)).parameters[0]


def _decoded(param: ProgramParameter, raw: object) -> Value:
    value = decode_program_argument(param, raw)
    assert not isinstance(value, Diagnostic)
    return value


class TestBindPositional:
    """``bind_positional_arguments`` maps positional raw values onto parameter names."""

    def test_positional_only_supplied_positionally(self) -> None:
        name = _param_and_info("name", ParamZone.POSITIONAL_ONLY, TextType(), required=True, line=1)
        assert bind_positional_arguments(_signature(name), ("alice",)) == ({"name": "alice"}, ())

    def test_standard_parameter_supplied_positionally(self) -> None:
        count = _param_and_info("count", ParamZone.STANDARD, IntType(), required=True, line=1)
        assert bind_positional_arguments(_signature(count), (3,)) == ({"count": 3}, ())

    def test_mixed_zones_bind_in_order_leaving_named_only_unbound(self) -> None:
        name = _param_and_info("name", ParamZone.POSITIONAL_ONLY, TextType(), required=True, line=1)
        flag = _param_and_info("flag", ParamZone.NAMED_ONLY, BoolType(), required=False, line=2)
        assert bind_positional_arguments(_signature(name, flag), ("bob",)) == ({"name": "bob"}, ())

    def test_no_positional_values_bind_nothing(self) -> None:
        assert bind_positional_arguments(_signature(), ()) == ({}, ())

    def test_an_explicit_json_null_is_a_bound_value_not_an_omitted_one(self) -> None:
        payload = _param_and_info("payload", ParamZone.STANDARD, JsonType(), required=False, line=1)
        assert bind_positional_arguments(_signature(payload), (None,)) == ({"payload": None}, ())

    def test_named_only_rejected_positionally_at_the_program_span(self) -> None:
        flag = _param_and_info("flag", ParamZone.NAMED_ONLY, BoolType(), required=True, line=1)
        bound, diagnostics = bind_positional_arguments(_signature(flag), (True,))
        assert bound == {}
        # The violation names no single parameter: anchored at the program's own span.
        assert [d.line for d in diagnostics] == [_PROGRAM_LINE]

    def test_too_many_positional_values_are_rejected_at_the_program_span(self) -> None:
        name = _param_and_info("name", ParamZone.POSITIONAL_ONLY, TextType(), required=True, line=1)
        bound, diagnostics = bind_positional_arguments(_signature(name), ("alice", "extra"))
        assert bound == {}
        assert [d.line for d in diagnostics] == [_PROGRAM_LINE]


class TestDecodeProgramArgument:
    """A raw value decodes against its parameter's type, or fails at the parameter's span."""

    def test_a_valid_value_decodes(self) -> None:
        count = _param("count", ParamZone.NAMED_ONLY, IntType(), required=True)
        assert _decoded(count, 5) == IntValue(5)

    def test_an_explicit_json_null_decodes_rather_than_defaulting(self) -> None:
        payload = _param("payload", ParamZone.NAMED_ONLY, JsonType(), required=False)
        assert _decoded(payload, None) == JsonValue(None)

    def test_a_malformed_value_is_a_diagnostic_at_the_parameter(self) -> None:
        count = _param("count", ParamZone.NAMED_ONLY, IntType(), required=True, line=4)
        failure = decode_program_argument(count, "not-json-int")
        assert isinstance(failure, Diagnostic)
        assert failure.line == 4

    def test_an_inherited_value_decodes_the_same_way(self) -> None:
        count = _param("count", ParamZone.NAMED_ONLY, IntType(), required=True, line=4)
        failure = decode_program_argument(count, "nope", inherited_address="a.b")
        assert isinstance(failure, Diagnostic)
        assert failure.line == 4


class TestCompleteProgramArguments:
    """Completion fills defaults in declaration order and reports every missing required one."""

    def test_decoded_values_fill_their_parameters_in_declaration_order(self) -> None:
        name = _param_and_info("name", ParamZone.POSITIONAL_ONLY, TextType(), required=True, line=1)
        flag = _param_and_info("flag", ParamZone.NAMED_ONLY, BoolType(), required=False, line=2)
        entries, diagnostics = complete_program_arguments(
            _signature(name, flag), {"flag": BoolValue(True), "name": TextValue("bob")}
        )
        assert diagnostics == ()
        assert entries == (TextValue("bob"), BoolValue(True))

    def test_an_omitted_defaulted_parameter_yields_use_default(self) -> None:
        flag = _param_and_info("flag", ParamZone.NAMED_ONLY, BoolType(), required=False, line=1)
        assert complete_program_arguments(_signature(flag), {}) == (
            (UseDefault(param_index=0),),
            (),
        )

    def test_a_zero_parameter_program_completes_to_nothing(self) -> None:
        assert complete_program_arguments(_signature(), {}) == ((), ())

    def test_an_explicit_json_null_satisfies_a_required_parameter(self) -> None:
        payload = _param_and_info(
            "payload", ParamZone.NAMED_ONLY, JsonType(), required=True, line=1
        )
        entries, diagnostics = complete_program_arguments(
            _signature(payload), {"payload": JsonValue(None)}
        )
        assert diagnostics == ()
        assert entries == (JsonValue(None),)

    def test_every_missing_required_parameter_is_reported_at_its_own_span(self) -> None:
        count = _param_and_info("count", ParamZone.NAMED_ONLY, IntType(), required=True, line=7)
        flag = _param_and_info("flag", ParamZone.NAMED_ONLY, BoolType(), required=True, line=8)
        entries, diagnostics = complete_program_arguments(_signature(count, flag), {})
        assert entries == ()
        assert [d.line for d in diagnostics] == [7, 8]

    def test_a_missing_parameter_is_reported_beside_a_failed_one(self) -> None:
        count = _param_and_info("count", ParamZone.NAMED_ONLY, IntType(), required=True, line=7)
        flag = _param_and_info("flag", ParamZone.NAMED_ONLY, BoolType(), required=True, line=8)
        entries, diagnostics = complete_program_arguments(
            _signature(count, flag), {}, failed=frozenset({"flag"})
        )
        assert entries == ()
        assert [d.line for d in diagnostics] == [7]

    def test_a_parameter_that_failed_to_decode_is_not_reported_missing(self) -> None:
        count = _param_and_info("count", ParamZone.NAMED_ONLY, IntType(), required=True, line=7)
        assert complete_program_arguments(_signature(count), {}, failed=frozenset({"count"})) == (
            (),
            (),
        )


class TestArgumentDiagnostics:
    """Each argument diagnostic is anchored where it can be acted on."""

    def test_a_duplicate_and_a_positional_only_by_name_argument_are_anchored_at_the_parameter(
        self,
    ) -> None:
        name = _param("name", ParamZone.POSITIONAL_ONLY, TextType(), required=True, line=3)
        assert duplicate_argument_diagnostic(name).line == 3
        assert positional_only_by_name_diagnostic(name).line == 3

    def test_an_unknown_argument_is_anchored_at_the_program(self) -> None:
        assert unknown_argument_diagnostic(_signature(), "bogus").line == _PROGRAM_LINE


class TestDecodeParamValue:
    """``decode_param_value`` takes ``text`` verbatim, decodes every other type
    through the canonical JSON boundary, and raises on a strict-JSON parse
    failure or a type/shape mismatch."""

    def test_text_verbatim(self) -> None:
        assert decode_param_value(_decoder(TextType()), "hello") == TextValue("hello")

    def test_json_from_string(self) -> None:
        assert decode_param_value(_decoder(IntType()), "5") == IntValue(5)

    def test_json_from_native_value(self) -> None:
        assert decode_param_value(_decoder(IntType()), 5) == IntValue(5)

    @pytest.mark.parametrize(
        ("raw", "case", "fields"),
        [
            ("claude/sonnet:medium", "AgentClaude", {"model": "sonnet", "thinking": "medium"}),
            ("codex/o3:high", "AgentCodex", {"model": "o3", "thinking": "high"}),
            (
                "pi/openai/gpt-5:low",
                "AgentPi",
                {"provider": "openai", "model": "gpt-5", "thinking": "low"},
            ),
            ("worker --flag", "AgentCommand", {"command": "worker --flag"}),
        ],
    )
    def test_agent_text_uses_host_syntax(self, raw: str, case: str, fields: dict[str, str]) -> None:
        value = decode_param_value(_decoder(BUILTIN_PRELUDE_TYPES["Agent"]), raw)

        assert isinstance(value, RecordValue)
        assert value.nominal == NominalId(require_reserved_enum_member_id("Agent", case))
        assert value.fields == {name: TextValue(field) for name, field in fields.items()}

    def test_agent_keeps_canonical_json_shape(self) -> None:
        value = decode_param_value(
            _decoder(BUILTIN_PRELUDE_TYPES["Agent"]),
            '{"$case":"AgentClaude","model":"opus","thinking":"custom"}',
        )

        assert isinstance(value, RecordValue)
        assert value.nominal == NominalId(require_reserved_enum_member_id("Agent", "AgentClaude"))
        assert value.fields == {"model": TextValue("opus"), "thinking": TextValue("custom")}

    def test_rejects_non_json_shaped_native_value(self) -> None:
        with pytest.raises(ValueError, match="JSON-compatible"):
            decode_param_value(_decoder(IntType()), {1, 2, 3})

    @pytest.mark.parametrize(
        ("target", "raw"),
        [
            (JsonType(), float("inf")),
            (JsonType(), {"rate": [Decimal("-Infinity")]}),
            (DecimalType(), Decimal("NaN")),
            (_option_type(JsonType()), OptionSome(float("nan"))),
        ],
    )
    def test_rejects_a_native_non_finite_number(self, target: AglType, raw: object) -> None:
        with pytest.raises(ValueError):
            decode_param_value(_decoder(target), raw)

    def test_is_json_shaped_dict_with_non_str_key_is_false(self) -> None:
        """_is_json_shaped: a dict with non-str keys is not JSON-shaped (covers
        the dict branch of _is_json_shaped).
        """
        from agnette.agl.runtime.arguments import _is_json_shaped

        # Dict with non-str key.
        assert _is_json_shaped({1: "a"}) is False
        # Dict with str keys and JSON-shaped values.
        assert _is_json_shaped({"k": 1}) is True

    def test_option_some_decodes_a_native_value(self) -> None:
        value = decode_param_value(_decoder(_option_type(IntType())), OptionSome(5))

        assert value == some_value(IntValue(5), nominals=NO_BUILTIN_DECLARATIONS)

    def test_option_some_decodes_a_native_float_into_a_decimal(self) -> None:
        """A native (non-string) ``OptionSome`` payload crosses the same
        canonical JSON boundary as a top-level native value, so a native
        Python ``float`` (as a TOML number decodes) widens into ``Decimal``
        exactly as it would outside an ``Option``."""
        value = decode_param_value(_decoder(_option_type(DecimalType())), OptionSome(1.1))

        assert value == some_value(DecimalValue(Decimal("1.1")), nominals=NO_BUILTIN_DECLARATIONS)

    def test_option_some_rejects_a_non_json_shaped_native_payload(self) -> None:
        with pytest.raises(ValueError):
            decode_param_value(_decoder(_option_type(IntType())), OptionSome({1, 2}))

    def test_option_some_decodes_a_string_through_host_text(self) -> None:
        value = decode_param_value(_decoder(_option_type(IntType())), OptionSome("5"))

        assert value == some_value(IntValue(5), nominals=NO_BUILTIN_DECLARATIONS)

    def test_option_some_of_text_is_taken_verbatim(self) -> None:
        value = decode_param_value(_decoder(_option_type(TextType())), OptionSome("hello"))

        assert value == some_value(TextValue("hello"), nominals=NO_BUILTIN_DECLARATIONS)

    def test_option_some_of_agent_uses_host_syntax(self) -> None:
        value = decode_param_value(
            _decoder(_option_type(BUILTIN_PRELUDE_TYPES["Agent"])), OptionSome("codex/o3:high")
        )

        assert isinstance(value, RecordValue)
        payload = value.fields["value"]
        assert isinstance(payload, RecordValue)
        assert payload.nominal == NominalId(require_reserved_enum_member_id("Agent", "AgentCodex"))

    def test_option_some_of_a_record_value_syntax_string(self) -> None:
        typ, table = _record_point_type()
        decoder = build_param_decoder(_option_type(typ), table)

        value = decode_param_value(decoder, OptionSome("Point(x = 1)"))

        assert isinstance(value, RecordValue)
        payload = value.fields["value"]
        assert isinstance(payload, RecordValue)
        assert payload.fields == {"x": IntValue(1)}

    def test_value_syntax_decodes_a_record_string_directly(self) -> None:
        typ, table = _record_point_type()
        decoder = build_param_decoder(typ, table)

        value = decode_param_value(decoder, "Point(x = 1)")

        assert isinstance(value, RecordValue)
        assert value.fields == {"x": IntValue(1)}

    def test_lone_surrogate_raw_value_is_rejected(self) -> None:
        from agnette.util.unicode import LoneSurrogateError

        with pytest.raises(LoneSurrogateError):
            decode_param_value(_decoder(TextType()), "a" + "\udc00" + "b")

    def test_option_some_lone_surrogate_payload_is_rejected(self) -> None:
        from agnette.util.unicode import LoneSurrogateError

        with pytest.raises(LoneSurrogateError):
            decode_param_value(_decoder(_option_type(TextType())), OptionSome("a" + "\udc00"))


def test_program_parameter_dataclass_carries_all_fields() -> None:
    span = _span(1)
    decoder = _decoder(IntType())
    param = ProgramParameter(
        name="count",
        kind=ParamZone.STANDARD,
        has_default=False,
        span=span,
        decoder=decoder,
    )
    assert param.name == "count"
    assert param.kind == ParamZone.STANDARD
    assert param.has_default is False
    assert param.span == span
    assert param.decoder is decoder
