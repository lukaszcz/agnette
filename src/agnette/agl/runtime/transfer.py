"""Same-image value transfer: AgL values as bytes between processes sharing one program image.

Raw values (:mod:`agnette.agl.semantics.values`) name every program entity -- nominal,
function, symbol -- by its linker-allocated integer id and never hold IR or descriptor
objects, so a copy refers to the image by id instead of duplicating it. Ids are
meaningful only in the image that allocated them, which an *image token* guards: every
payload starts with a magic number and the sending registry's token, and
:meth:`ImageTransfer.loads` rejects any other before it unpickles. A forked child shares
its parent's token, because memory is copied; any other process has its own.

The value graph is flattened iteratively into a *node table*, so neither walk is
bounded by nesting depth, and only that table (builtins and ``Decimal``) is pickled.
Each heap node -- array, dict, record, exception, constructor, closure, ``var`` cell,
``json`` value, and each list or dict inside a ``json`` payload -- gets one table entry
on first visit, which preserves internal sharing and cycles. A body is
``(is_carrier, root, span, table)`` where an entry is a tuple led by a kind tag:

- ``("A", refs)`` array; ``("D", [(key, value)])`` dict, in insertion order;
- ``("R" | "E", nominal, [(field, ref)])`` record or exception; ``("K", nominal)`` constructor;
- ``("F", function, [(symbol, ref)])`` closure; ``("C", ref)`` cell; ``("J", json_ref)`` json value;
- ``("jl", json_refs)`` and ``("jd", [(key, json_ref)])`` json list and object.

A reference to a value is ``None`` (unit), a ``bool``, ``int``, ``str`` or ``Decimal``
scalar, or ``(index,)`` for a node. A json reference is a json scalar or ``(index,)``.

Rebuilding allocates every mutable node first (arrays, dicts, records, exceptions, cells,
json containers), then closures in capture-dependency order, then fills the mutable
nodes, and inserts dict entries last because a key's token reads its fields. Cycles
always pass through a mutable node, since a closure's captures and a json value's payload
are fixed at creation.

A payload that passes the header check and unpickles was written by :meth:`dumps` of this
image, so :meth:`ImageTransfer.loads` trusts the table's structure; its content is the
producing companion's responsibility, as with any companion return value.
"""

from __future__ import annotations

import io
import pickle
import secrets
from collections.abc import Callable
from decimal import Decimal
from typing import cast

from agnette.agl.ir.ids import FunctionId, Location, NominalId, SourceId, SymbolId
from agnette.agl.runtime.boundary import (
    AglException,
    BoundaryViolation,
    current_descriptors,
    decode_boundary_value,
    encode_boundary_value,
    extern_call_active,
)
from agnette.agl.semantics.values import (
    UNIT_VALUE,
    ArrayValue,
    BoolValue,
    Cell,
    ConstructorValue,
    DecimalValue,
    DictValue,
    ExceptionValue,
    IntValue,
    IrClosureValue,
    JsonValue,
    RecordValue,
    TextValue,
    UnitValue,
    Value,
)

_MAGIC = b"AGLT"
_TOKEN_SIZE = 16
_HEADER_SIZE = len(_MAGIC) + _TOKEN_SIZE

_JSON_SCALARS = (type(None), bool, int, float, str, Decimal)


class TransferError(Exception):
    """A value cannot be transferred, or a payload is not one of this image's."""


class _Unpickler(pickle.Unpickler):
    """Resolves only ``Decimal``; every other class a payload names is refused."""

    def find_class(self, module: str, name: str) -> type[Decimal]:
        if (module, name) != (Decimal.__module__, Decimal.__qualname__):
            raise pickle.UnpicklingError(f"{module}.{name} is not transferable")
        return Decimal


class _Flattener:
    """Flattens a value graph into a node table without recursion."""

    def __init__(self) -> None:
        self.table: list[tuple[object, ...]] = []
        self._index: dict[int, int] = {}
        self._pending: list[tuple[int, object]] = []

    def flatten(self, root: Value) -> tuple[object, list[tuple[object, ...]]]:
        """Return *root*'s reference and the table of everything reachable from it."""
        ref = self._value(root)
        while self._pending:
            index, node = self._pending.pop()
            self.table[index] = self._entry(node)
        return ref, self.table

    def _node(self, node: object) -> tuple[int]:
        index = self._index.get(id(node))
        if index is None:
            index = self._index[id(node)] = len(self.table)
            self.table.append(())
            self._pending.append((index, node))
        return (index,)

    def _value(self, value: object) -> object:
        if isinstance(value, (TextValue, IntValue, BoolValue, DecimalValue)):
            return value.value
        if isinstance(value, UnitValue):
            return None
        return self._node(value)

    def _json(self, raw: object) -> object:
        if type(raw) in _JSON_SCALARS:
            return raw
        if isinstance(raw, (list, dict)):
            return self._node(raw)
        raise TransferError(f"a {type(raw).__name__} is not a json value")

    def _fields(self, fields: dict[str, Value]) -> list[tuple[str, object]]:
        return [(name, self._value(field)) for name, field in fields.items()]

    def _entry(self, node: object) -> tuple[object, ...]:
        if isinstance(node, ArrayValue):
            return "A", [self._value(element) for element in node.elements]
        if isinstance(node, DictValue):
            return "D", [(self._value(key), self._value(value)) for key, value in node.items()]
        if isinstance(node, RecordValue):
            return "R", node.nominal.value, self._fields(node.fields)
        if isinstance(node, ExceptionValue):
            return "E", node.nominal.value, self._fields(node.fields)
        if isinstance(node, ConstructorValue):
            return "K", node.nominal.value
        if isinstance(node, IrClosureValue):
            captures = [(symbol.value, self._value(slot)) for symbol, slot in node.captures]
            return "F", node.function_id.value, captures
        if isinstance(node, Cell):
            return "C", self._value(node.value)
        if isinstance(node, JsonValue):
            return "J", self._json(node.raw)
        if isinstance(node, list):
            return "jl", [self._json(element) for element in node]
        if isinstance(node, dict):
            if not all(isinstance(key, str) for key in node):
                raise TransferError("a json object has a non-text key")
            return "jd", [(key, self._json(value)) for key, value in node.items()]
        raise TransferError(f"a {type(node).__name__} cannot be transferred")


#: Allocates the mutable shell of a node kind, to be filled once all nodes exist.
_SHELLS: dict[str, Callable[[tuple[object, ...]], object]] = {
    "A": lambda entry: ArrayValue([]),
    "D": lambda entry: DictValue(),
    "R": lambda entry: RecordValue(NominalId(cast("int", entry[1])), {}),
    "E": lambda entry: ExceptionValue(NominalId(cast("int", entry[1])), {}),
    "K": lambda entry: ConstructorValue(NominalId(cast("int", entry[1]))),
    "C": lambda entry: Cell(UNIT_VALUE),
    "jl": lambda entry: [],
    "jd": lambda entry: {},
}


class _Rebuilder:
    """Rebuilds a value graph from a node table without recursion; trusts the table."""

    def __init__(self, entries: list[tuple[object, ...]]) -> None:
        self._entries = entries
        self._kinds = [cast("str", entry[0]) for entry in entries]
        self._nodes: list[object] = [None] * len(entries)

    def rebuild(self, root: object) -> Value:
        """Build every node, then return the one *root* refers to."""
        entries, nodes = self._entries, self._nodes
        for index, entry in enumerate(entries):
            shell = _SHELLS.get(self._kinds[index])
            nodes[index] = None if shell is None else shell(entry)
        for index, entry in enumerate(entries):
            if self._kinds[index] == "J":
                nodes[index] = JsonValue(self._json(entry[1]))
        self._build_closures()
        for node, entry in zip(nodes, entries):
            self._fill(node, entry)
        for node, entry in zip(nodes, entries):
            if isinstance(node, DictValue):
                for key, value in cast("list[tuple[object, object]]", entry[1]):
                    node.insert(self._value(key), self._value(value))
        return self._value(root)

    def _node(self, ref: object) -> object:
        return self._nodes[cast("tuple[int]", ref)[0]]

    def _value(self, ref: object) -> Value:
        if ref is None:
            return UNIT_VALUE
        if isinstance(ref, bool):
            return BoolValue(ref)
        if isinstance(ref, int):
            return IntValue(ref)
        if isinstance(ref, str):
            return TextValue(ref)
        if isinstance(ref, Decimal):
            return DecimalValue(ref)
        return cast("Value", self._node(ref))

    def _slot(self, ref: object) -> Value | Cell:
        if type(ref) is tuple:
            node = self._node(ref)
            if isinstance(node, Cell):
                return node
        return self._value(ref)

    def _json(self, ref: object) -> object:
        return self._node(ref) if type(ref) is tuple else ref

    def _fill(self, node: object, entry: tuple[object, ...]) -> None:
        if isinstance(node, ArrayValue):
            node.elements.extend(self._value(ref) for ref in cast("list[object]", entry[1]))
        elif isinstance(node, (RecordValue, ExceptionValue)):
            for name, ref in cast("list[tuple[str, object]]", entry[2]):
                node.fields[name] = self._value(ref)
        elif isinstance(node, Cell):
            node.value = self._value(entry[1])
        elif isinstance(node, list):
            cast("list[object]", node).extend(
                self._json(ref) for ref in cast("list[object]", entry[1])
            )
        elif isinstance(node, dict):
            for key, ref in cast("list[tuple[str, object]]", entry[1]):
                cast("dict[str, object]", node)[key] = self._json(ref)

    def _captures(self, index: int) -> list[tuple[int, object]]:
        return cast("list[tuple[int, object]]", self._entries[index][2])

    def _closure_dependencies(self, index: int) -> list[int]:
        """The unbuilt closure nodes the closure at *index* captures directly."""
        return [
            cast("tuple[int]", ref)[0]
            for _symbol, ref in self._captures(index)
            if type(ref) is tuple and self._kinds[cast("tuple[int]", ref)[0]] == "F"
        ]

    def _build_closures(self) -> None:
        """Build closures after the closures they capture."""
        built: set[int] = set()
        for root, kind in enumerate(self._kinds):
            stack = [root] if kind == "F" else []
            while stack:
                index = stack[-1]
                if index in built:
                    stack.pop()
                    continue
                pending = [d for d in self._closure_dependencies(index) if d not in built]
                if pending:
                    stack.extend(pending)
                    continue
                captures = tuple(
                    (SymbolId(symbol), self._slot(ref)) for symbol, ref in self._captures(index)
                )
                function = cast("int", self._entries[index][1])
                self._nodes[index] = IrClosureValue(FunctionId(function), captures)
                built.add(index)
                stack.pop()


class ImageTransfer:
    """``agl.transfer``: :meth:`dumps` and :meth:`loads` for one program image."""

    __slots__ = ("_token",)

    def __init__(self) -> None:
        self._token = secrets.token_bytes(_TOKEN_SIZE)

    def dumps(self, obj: object) -> bytes:
        """Serialize a boundary value or an :class:`AglException` carrier.

        A carrier keeps its raw exception value and span.
        """
        span: Location | None = None
        if isinstance(obj, AglException):
            value: Value = obj.value
            span = obj.span
        else:
            try:
                value = decode_boundary_value(obj)
            except BoundaryViolation as exc:
                raise TransferError(str(exc)) from exc
        root, table = _Flattener().flatten(value)
        envelope = (isinstance(obj, AglException), root, _span_fields(span), table)
        return _MAGIC + self._token + pickle.dumps(envelope, protocol=pickle.HIGHEST_PROTOCOL)

    def loads(self, data: bytes) -> object:
        """Rebuild a payload of this image as a boundary value, or an ``AglException`` carrier.

        Callable only during an extern call, like any function crossing.
        """
        if not extern_call_active():
            raise TransferError("agl.transfer.loads may run only during an extern call")
        if data[: len(_MAGIC)] != _MAGIC or data[len(_MAGIC) : _HEADER_SIZE] != self._token:
            raise TransferError("payload is not a transfer of this program image")
        carrier, value, span = _read(data[_HEADER_SIZE:])
        if carrier:
            return AglException(value, span=span)
        return encode_boundary_value(value, current_descriptors())


def _span_fields(span: Location | None) -> tuple[int, ...] | None:
    if span is None:
        return None
    return (
        span.source_id.value,
        span.start_offset,
        span.end_offset,
        span.start_line,
        span.start_col,
    )


def _read(body: bytes) -> tuple[bool, Value, Location | None]:
    """Unpickle and rebuild one payload body: ``(is carrier, raw value, span)``."""
    stream = io.BytesIO(body)
    try:
        envelope = cast("object", _Unpickler(stream).load())
        trailing = stream.read(1)
    except Exception as exc:  # pickle fails in arbitrary ways on truncated or garbage input
        raise TransferError("transfer payload is corrupt") from exc
    if trailing:
        raise TransferError("transfer payload has trailing data")
    carrier, root, fields, table = cast(
        "tuple[bool, object, tuple[int, int, int, int, int] | None, list[tuple[object, ...]]]",
        envelope,
    )
    span = None
    if fields is not None:
        span = Location(SourceId(fields[0]), fields[1], fields[2], fields[3], fields[4])
    return carrier, _Rebuilder(table).rebuild(root), span
