"""Same-image value transfer (``agl.transfer``) at the extern boundary."""

from __future__ import annotations

import itertools
import pickle
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol, cast

import pytest

from agnette.agl.ir.builtin_nominals import NO_BUILTIN_DECLARATIONS
from agnette.agl.ir.ids import FunctionId, Location, NominalId, SourceId, SymbolId
from agnette.agl.ir.program import (
    NominalDescriptor,
    NominalKind,
    ValueDescriptors,
    VariantDescriptor,
)
from agnette.agl.modules.ids import ENTRY_ID
from agnette.agl.runtime.boundary import AglException
from agnette.agl.runtime.externs import AglCallableProxy, ExternRegistry
from agnette.agl.runtime.transfer import TransferError
from agnette.agl.semantics.exceptions import AglRaise
from agnette.agl.semantics.values import (
    UNIT_VALUE,
    ArrayValue,
    BoolValue,
    Cell,
    ConstructorValue,
    DecimalValue,
    DictValue,
    ExceptionValue,
    FunctionValue,
    IntValue,
    IrClosureValue,
    IteratorValue,
    JsonValue,
    RecordValue,
    TextValue,
    Value,
)
from agnette.util.recursion import raised_recursion_limit

_NO_DESCRIPTORS = ValueDescriptors(nominals={}, functions={}, exception_field_encodes={})
_ids = itertools.count(9_300_000)
_SPAN = Location(SourceId(3), 10, 20, 2, 4)


class _Transfer(Protocol):
    def dumps(self, obj: object) -> bytes: ...
    def loads(self, data: bytes) -> object: ...


def _nominal(
    registry: ExternRegistry,
    name: str,
    kind: NominalKind,
    fields: tuple[str, ...] = (),
    *,
    mutable: tuple[str, ...] = (),
) -> NominalId:
    nominal = NominalId(next(_ids))
    registry.set_nominals(
        {
            nominal: NominalDescriptor(
                nominal=nominal,
                module_id=ENTRY_ID,
                scope_path=(),
                declared_name=name,
                kind=kind,
                fields=fields,
                field_json_names=fields,
                mutable_fields=frozenset(mutable),
            )
        }
    )
    return nominal


def _enum(
    registry: ExternRegistry, name: str, variants: dict[str, tuple[str, ...]]
) -> dict[str, NominalId]:
    """Register an enum and one record nominal per variant; return the variant ids."""
    enum = NominalId(next(_ids))
    ids = {variant: NominalId(next(_ids)) for variant in variants}
    registry.set_nominals(
        {
            enum: NominalDescriptor(
                nominal=enum,
                module_id=ENTRY_ID,
                scope_path=(),
                declared_name=name,
                kind=NominalKind.ENUM,
                variants=tuple(
                    VariantDescriptor(variant, fields, ids[variant], variant, fields)
                    for variant, fields in variants.items()
                ),
            ),
            **{
                ids[variant]: NominalDescriptor(
                    ids[variant], ENTRY_ID, (name,), variant, NominalKind.RECORD, fields, fields
                )
                for variant, fields in variants.items()
            },
        }
    )
    return ids


class _World:
    """A registry with one nominal of each shape, plus a round-trip driver."""

    def __init__(self) -> None:
        self.registry = ExternRegistry()
        self.point = _nominal(self.registry, "Point", NominalKind.RECORD, ("x", "y"))
        self.node = _nominal(
            self.registry, "Node", NominalKind.RECORD, ("label", "next"), mutable=("next",)
        )
        self.problem = _nominal(self.registry, "Problem", NominalKind.EXCEPTION, ("message", "why"))
        self.circle = _enum(self.registry, "Shape", {"Circle": ("radius",)})["Circle"]
        chain = _enum(self.registry, "Chain", {"End": (), "Link": ("value", "rest")})
        self.end = chain["End"]
        self.link = chain["Link"]
        self.transfer = cast(_Transfer, getattr(self.registry._agl_module(), "transfer"))

    def call(
        self,
        companion: Callable[[_Transfer, object], object],
        *args: Value,
        transfer: _Transfer | None = None,
    ) -> Value:
        """Run *companion* as an extern call over *args*; return its decoded result."""
        active = transfer if transfer is not None else self.transfer
        return self.registry.invoke(
            "companion",
            lambda arg: companion(active, arg),
            args,
            nominals=NO_BUILTIN_DECLARATIONS,
            descriptors=_NO_DESCRIPTORS,
            function_encoder=_proxy,
        )

    def roundtrip(self, value: Value) -> Value:
        return self.call(lambda transfer, arg: transfer.loads(transfer.dumps(arg)), value)


def _proxy(function: FunctionValue) -> AglCallableProxy:
    return AglCallableProxy(
        arity=0,
        function=function,
        require_active_window=lambda: None,
        invoke=lambda args: UNIT_VALUE,
    )


@pytest.fixture
def world() -> _World:
    return _World()


def _header(transfer: _Transfer) -> bytes:
    """The envelope header (magic and image token) *transfer* writes: all but the pickled body."""
    data = transfer.dumps(None)
    body = pickle.dumps((False, None, None, []), protocol=pickle.HIGHEST_PROTOCOL)
    assert data.endswith(body)
    return data[: len(data) - len(body)]


def _array(*elements: Value) -> ArrayValue:
    return ArrayValue(list(elements))


@pytest.mark.parametrize(
    "value",
    [
        TextValue("héllo ☃"),
        IntValue(10**40),
        DecimalValue(Decimal("1.50")),
        BoolValue(True),
        UNIT_VALUE,
        JsonValue({"a": [1, Decimal("2.5"), None, True, "x"], "b": {}}),
        JsonValue(None),
    ],
    ids=["text", "int", "decimal", "bool", "unit", "json", "json-null"],
)
def test_scalar_kinds_round_trip(world: _World, value: Value) -> None:
    result = world.roundtrip(value)

    assert result == value
    assert type(result) is type(value)


def test_decimal_keeps_its_exact_representation(world: _World) -> None:
    result = world.roundtrip(DecimalValue(Decimal("0.10")))

    assert isinstance(result, DecimalValue)
    assert result.value.as_tuple() == Decimal("0.10").as_tuple()


def test_arrays_and_text_keyed_dicts_round_trip_as_deep_copies(world: _World) -> None:
    inner = _array(IntValue(1), IntValue(2))
    entries = DictValue({"k": inner, "z": TextValue("v")})
    original = _array(inner, entries)

    result = world.roundtrip(original)

    assert result == original
    assert isinstance(result, ArrayValue)
    assert result is not original
    assert result.elements[0] is not inner
    copied = result.elements[0]
    assert isinstance(copied, ArrayValue)
    copied.elements.append(IntValue(3))
    assert inner.elements == [IntValue(1), IntValue(2)]


def test_token_keyed_dicts_keep_their_keys_and_order(world: _World) -> None:
    key = RecordValue(world.point, {"x": IntValue(1), "y": IntValue(2)})
    table = DictValue()
    table.insert(IntValue(7), TextValue("seven"))
    table.insert(DecimalValue(Decimal("1.5")), TextValue("decimal"))
    table.insert(key, TextValue("record"))
    table.insert(BoolValue(True), TextValue("bool"))

    result = world.roundtrip(table)

    assert isinstance(result, DictValue)
    assert result == table
    assert [str(k) for k in result.keys()] == [str(k) for k in table.keys()]
    assert result.lookup(DecimalValue(Decimal("1.50"))) == TextValue("decimal")
    assert result.lookup(RecordValue(world.point, {"x": IntValue(1), "y": IntValue(2)})) == (
        TextValue("record")
    )
    assert result.lookup(IntValue(1)) is None


def test_empty_containers_round_trip(world: _World) -> None:
    assert world.roundtrip(_array()) == _array()
    assert world.roundtrip(DictValue()) == DictValue()


def test_records_enums_and_exceptions_round_trip(world: _World) -> None:
    point = RecordValue(world.point, {"x": IntValue(1), "y": IntValue(2)})
    circle = RecordValue(world.circle, {"radius": DecimalValue(Decimal("2.5"))})
    problem = ExceptionValue(world.problem, {"message": TextValue("bad"), "why": TextValue("why")})

    for value in (point, circle, problem):
        result = world.roundtrip(value)
        assert result == value
        assert result is not value


def test_mutable_records_round_trip_as_independent_copies(world: _World) -> None:
    original = RecordValue(world.node, {"label": TextValue("a"), "next": UNIT_VALUE})

    result = world.roundtrip(original)

    assert isinstance(result, RecordValue)
    assert result == original
    result.fields["next"] = IntValue(1)
    assert original.fields["next"] == UNIT_VALUE


def test_internal_sharing_is_preserved(world: _World) -> None:
    shared = _array(IntValue(1))
    point = RecordValue(world.point, {"x": shared, "y": shared})
    original = _array(shared, point, shared)

    result = world.roundtrip(original)

    assert isinstance(result, ArrayValue)
    first, record, last = result.elements
    assert isinstance(record, RecordValue)
    assert first is last
    assert record.fields["x"] is first
    assert record.fields["y"] is first
    assert first is not shared


def test_array_cycle_is_preserved(world: _World) -> None:
    original = _array(IntValue(1))
    original.elements.append(original)

    result = world.roundtrip(original)

    assert isinstance(result, ArrayValue)
    assert result is not original
    assert result.elements[1] is result


def test_record_dict_and_mutual_cycles_are_preserved(world: _World) -> None:
    node = RecordValue(world.node, {"label": TextValue("n"), "next": UNIT_VALUE})
    table = DictValue({"node": node})
    holder = _array(table)
    node.fields["next"] = holder
    table_again = DictValue({"self": holder})
    holder.elements.append(table_again)

    result = world.roundtrip(node)

    assert isinstance(result, RecordValue)
    holder_copy = result.fields["next"]
    assert isinstance(holder_copy, ArrayValue)
    table_copy = holder_copy.elements[0]
    assert isinstance(table_copy, DictValue)
    assert table_copy.lookup(TextValue("node")) is result
    again_copy = holder_copy.elements[1]
    assert isinstance(again_copy, DictValue)
    assert again_copy.lookup(TextValue("self")) is holder_copy


def test_opaque_style_nominal_values_round_trip(world: _World) -> None:
    handle = _nominal(world.registry, "Handle", NominalKind.RECORD, ("id", "label"))
    value = RecordValue(handle, {"id": IntValue(4), "label": TextValue("h")})

    assert world.roundtrip(_array(value, value)) == _array(value, value)


def test_closures_carry_copied_captures_and_keep_their_function_identity(world: _World) -> None:
    items = _array(IntValue(1))
    counter = Cell(IntValue(5))
    closure = IrClosureValue(
        FunctionId(41),
        ((SymbolId(1), items), (SymbolId(2), counter), (SymbolId(3), TextValue("t"))),
    )
    sibling = IrClosureValue(FunctionId(42), ((SymbolId(2), counter),))

    result = world.roundtrip(_array(closure, sibling))

    assert isinstance(result, ArrayValue)
    first, second = result.elements
    assert isinstance(first, IrClosureValue)
    assert isinstance(second, IrClosureValue)
    assert first is not closure
    assert first.function_id == FunctionId(41)
    assert second.function_id == FunctionId(42)
    (_, copied_items), (_, copied_counter), (_, copied_text) = first.captures
    assert isinstance(copied_items, ArrayValue)
    assert copied_items is not items
    assert copied_items.elements == [IntValue(1)]
    assert isinstance(copied_counter, Cell)
    assert copied_counter is not counter
    assert copied_counter.value == IntValue(5)
    assert copied_text == TextValue("t")
    assert second.captures[0][1] is copied_counter
    copied_counter.value = IntValue(6)
    assert counter.value == IntValue(5)


def test_closures_sharing_a_captured_closure_keep_it_shared(world: _World) -> None:
    inner = IrClosureValue(FunctionId(44), ())
    left = IrClosureValue(FunctionId(45), ((SymbolId(1), inner), (SymbolId(2), inner)))
    right = IrClosureValue(FunctionId(46), ((SymbolId(1), inner),))

    result = world.roundtrip(_array(left, right))

    assert isinstance(result, ArrayValue)
    first, second = result.elements
    assert isinstance(first, IrClosureValue)
    assert isinstance(second, IrClosureValue)
    assert first.captures[0][1] is first.captures[1][1] is second.captures[0][1]
    assert first.captures[0][1] is not inner


def test_closure_cycle_through_its_own_capture_is_preserved(world: _World) -> None:
    holder = _array()
    closure = IrClosureValue(FunctionId(43), ((SymbolId(1), holder),))
    holder.elements.append(closure)

    result = world.roundtrip(closure)

    assert isinstance(result, IrClosureValue)
    captured = result.captures[0][1]
    assert isinstance(captured, ArrayValue)
    assert captured.elements[0] is result


def test_constructor_values_round_trip(world: _World) -> None:
    constructor = ConstructorValue(world.point)

    result = world.roundtrip(constructor)

    assert result == constructor
    assert result is not constructor


def test_carrier_round_trips_with_value_and_span(world: _World) -> None:
    problem = ExceptionValue(world.problem, {"message": TextValue("bad"), "why": TextValue("w")})

    def companion(transfer: _Transfer, arg: object) -> object:
        restored = transfer.loads(transfer.dumps(AglException(arg, span=_SPAN)))
        assert isinstance(restored, AglException)
        raise restored

    with pytest.raises(AglRaise) as excinfo:
        world.call(companion, problem)

    assert excinfo.value.exc == problem
    assert excinfo.value.span == _SPAN


def test_carrier_without_a_span_comes_back_without_one(world: _World) -> None:
    problem = ExceptionValue(world.problem, {"message": TextValue("bad"), "why": TextValue("w")})

    def companion(transfer: _Transfer, arg: object) -> object:
        restored = transfer.loads(transfer.dumps(AglException(arg)))
        assert isinstance(restored, AglException)
        assert restored.span is None
        return restored.value is not arg

    assert world.call(companion, problem) == BoolValue(True)


def test_an_exception_value_is_not_confused_with_a_carrier(world: _World) -> None:
    problem = ExceptionValue(world.problem, {"message": TextValue("bad"), "why": TextValue("w")})

    def companion(transfer: _Transfer, arg: object) -> object:
        restored = transfer.loads(transfer.dumps(arg))
        assert not isinstance(restored, AglException)
        return restored

    assert world.call(companion, problem) == problem


def test_carrier_value_is_a_copy(world: _World) -> None:
    items = _array(IntValue(1))
    problem = ExceptionValue(world.problem, {"message": items, "why": items})
    seen: list[ExceptionValue] = []

    def companion(transfer: _Transfer, arg: object) -> object:
        carrier = AglException(arg)
        restored = transfer.loads(transfer.dumps(carrier))
        assert isinstance(restored, AglException)
        seen.append(restored.value)
        return None

    world.call(companion, problem)

    assert seen[0] == problem
    assert seen[0] is not problem
    assert seen[0].fields["message"] is seen[0].fields["why"]


def test_foreign_image_token_is_rejected(world: _World) -> None:
    other = _World()

    def companion(transfer: _Transfer, arg: object) -> object:
        data = other.transfer.dumps(arg)
        with pytest.raises(TransferError):
            transfer.loads(data)
        return None

    world.call(companion, IntValue(1))


def test_a_payload_loads_in_a_later_call_of_the_same_registry(world: _World) -> None:
    data = world.transfer.dumps(9)

    assert world.call(lambda transfer, arg: transfer.loads(data), UNIT_VALUE) == IntValue(9)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data, header: b"",
        lambda data, header: data[:3],
        lambda data, header: data[: len(header) - 1],
        lambda data, header: b"X" + data[1:],
        lambda data, header: data[:-3],
        lambda data, header: data + b"trailing",
        lambda data, header: header + b"not a pickle",
        lambda data, header: header,
    ],
    ids=[
        "empty",
        "short-magic",
        "short-token",
        "bad-magic",
        "truncated",
        "trailing",
        "garbage",
        "no-body",
    ],
)
def test_malformed_envelopes_are_rejected(
    world: _World, mutate: Callable[[bytes, bytes], bytes]
) -> None:
    def companion(transfer: _Transfer, arg: object) -> object:
        data = transfer.dumps(arg)
        with pytest.raises(TransferError):
            transfer.loads(mutate(data, _header(transfer)))
        return None

    world.call(companion, _array(IntValue(1)))


def test_payload_naming_foreign_classes_is_rejected(world: _World) -> None:
    def companion(transfer: _Transfer, arg: object) -> object:
        with pytest.raises(TransferError):
            transfer.loads(_header(transfer) + pickle.dumps(print))
        return None

    world.call(companion, UNIT_VALUE)


def test_loads_outside_an_extern_call_is_rejected(world: _World) -> None:
    data = world.transfer.dumps(1)

    with pytest.raises(TransferError):
        world.transfer.loads(data)


def test_values_without_a_boundary_representation_are_rejected(world: _World) -> None:
    for unsupported in ([1, 2], {"a": 1}, object()):
        with pytest.raises(TransferError):
            world.transfer.dumps(unsupported)


# -- Nesting depth: transfer has no limit of its own ----------------------------------

_DEEP = 10_000


def _depth(value: object, step: Callable[[Any], object]) -> int:
    """Count the nodes reachable from *value* by applying *step* until it yields ``None``."""
    count = 0
    node: object = value
    while node is not None:
        count += 1
        node = step(node)
    return count


def _inner_array(value: object) -> object:
    if isinstance(value, ArrayValue) and value.elements:
        return value.elements[0]
    return None


def _inner_closure(value: object) -> object:
    if isinstance(value, IrClosureValue) and value.captures:
        return value.captures[0][1]
    return None


def _field(name: str) -> Callable[[object], object]:
    def step(value: object) -> object:
        if isinstance(value, RecordValue):
            return value.fields.get(name)
        return None

    return step


def _inner_json(value: object) -> object:
    return cast("list[dict[str, object]]", value)[0]["k"]


def test_deep_immutable_record_chain_round_trips(world: _World) -> None:
    length = 5_000
    chain: Value = RecordValue(world.end, {})
    for n in range(length):
        chain = RecordValue(world.link, {"value": IntValue(n), "rest": chain})

    # Boundary conversion is recursive Python, covered by the raised limit as in a run.
    with raised_recursion_limit(10 * length):
        result = world.roundtrip(chain)

    assert isinstance(result, RecordValue)
    assert _depth(result, _field("rest")) == length + 1
    assert result.fields["value"] == IntValue(length - 1)


def test_deep_mutable_record_chain_round_trips(world: _World) -> None:
    chain: Value = UNIT_VALUE
    for n in range(_DEEP):
        chain = RecordValue(world.node, {"label": TextValue(str(n)), "next": chain})

    result = world.roundtrip(chain)

    assert isinstance(result, RecordValue)
    assert _depth(result, _field("next")) == _DEEP + 1


def test_deep_array_nesting_round_trips(world: _World) -> None:
    value = _array()
    for _ in range(_DEEP):
        value = _array(value)

    result = world.roundtrip(value)

    assert isinstance(result, ArrayValue)
    assert _depth(result, _inner_array) == _DEEP + 1


def test_deep_closure_capture_chain_round_trips(world: _World) -> None:
    closure = IrClosureValue(FunctionId(50), ())
    for _ in range(_DEEP):
        closure = IrClosureValue(FunctionId(51), ((SymbolId(1), closure),))

    result = world.roundtrip(closure)

    assert isinstance(result, IrClosureValue)
    assert _depth(result, _inner_closure) == _DEEP + 1


def test_deep_json_round_trips(world: _World) -> None:
    raw: object = None
    for _ in range(_DEEP):
        raw = [{"k": raw}]

    result = world.roundtrip(JsonValue(raw))

    assert isinstance(result, JsonValue)
    assert _depth(result.raw, _inner_json) == _DEEP


def test_unrepresentable_json_and_internal_values_are_rejected(world: _World) -> None:
    problem = ExceptionValue(
        world.problem, {"message": TextValue("m"), "why": IteratorValue(elements=[])}
    )

    for unsupported in (JsonValue({"a": (1,)}), JsonValue({1: 2})):
        with pytest.raises(TransferError):
            world.transfer.dumps(
                AglException(ExceptionValue(world.problem, {"message": unsupported}))
            )
    with pytest.raises(TransferError):
        world.transfer.dumps(AglException(problem))
