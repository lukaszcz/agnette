"""Shared ``NominalDescriptor``/``VariantDescriptor`` builders.

Both whole-program linking (``lower/program.py``, from a checked module's own
``TypeDef``) and reserved built-in registration (``lower/lowerer.py``, from a
prelude/enum/exception ``TypeDef`` looked up on the same ``TypeTable``) build
the same descriptor shapes from declaration data: :func:`exception_descriptor`
sets the ``base`` link (the runtime mirror of ``TypeTable.ancestor_defs``;
records and enums have none), :func:`enum_descriptor` builds its
``VariantDescriptor`` tuple via :func:`variant_descriptors`, and
:func:`record_descriptor`, :func:`exception_descriptor`, and
:func:`_variant_descriptor` all get their field/JSON-name pairing from
:func:`json_field_names`, computed in one place for all three.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from agnette.agl.ir.ids import NominalId
from agnette.agl.ir.nodes import IrExpr
from agnette.agl.ir.program import NominalDescriptor, NominalKind, VariantDescriptor
from agnette.agl.semantics.arguments import has_positional_only, positional_field_names
from agnette.agl.semantics.type_table import TypeDef, TypeTable
from agnette.agl.semantics.types import EnumType, ExceptionType, RecordType

__all__ = ["nominal_descriptor"]


def json_field_names(
    type_table: TypeTable, handle: RecordType | ExceptionType
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return *handle*'s field names and their effective JSON names, both in declaration order.

    The one place ``TypeTable.json_fields`` is split into the parallel
    ``fields``/``field_json_names`` pair every ``NominalDescriptor`` and
    ``VariantDescriptor`` carries; ``type_schema._emit_field_encodes`` reads
    ``TypeTable.json_fields`` directly for ``FieldEncode``.
    """
    json_fields = type_table.json_fields(handle)
    return (
        tuple(fname for fname, _json_name, _ftype in json_fields),
        tuple(json_name for _name, json_name, _ftype in json_fields),
    )


def _variant_descriptor(name: str, member: RecordType, type_table: TypeTable) -> VariantDescriptor:
    """Build one enum member's ``VariantDescriptor``, fields JSON-keyed like its own descriptor."""
    fields, field_json_names = json_field_names(type_table, member)
    return VariantDescriptor(
        name=name,
        fields=fields,
        member=NominalId(member.decl_id),
        json_name=type_table.member_json_tag(member, name),
        field_json_names=field_json_names,
    )


def variant_descriptors(handle: EnumType, type_table: TypeTable) -> tuple[VariantDescriptor, ...]:
    """Build every member's ``VariantDescriptor`` for enum *handle*, in declaration order."""
    return tuple(
        _variant_descriptor(name, member, type_table)
        for name, member in type_table.enum_member_names(handle).items()
    )


class _FlatFields(NamedTuple):
    """An exception's flattened field data, base chain first."""

    names: tuple[str, ...]
    json_names: tuple[str, ...]
    defaults: tuple[IrExpr | None, ...]
    positional: tuple[str, ...]


_NO_FIELDS = _FlatFields((), (), (), ())


def _flatten_from_scratch(
    typedef: TypeDef,
    type_table: TypeTable,
    field_defaults: Mapping[NominalId, tuple[IrExpr | None, ...]],
) -> _FlatFields:
    """Flatten *typedef*'s whole base chain, an own field overriding an inherited one in place."""
    handle = typedef.exception_handle()
    names, json_names = json_field_names(type_table, handle)
    defaults_by_field: dict[str, IrExpr | None] = {}
    for chain_typedef in type_table.exception_field_defs(typedef.decl_node_id):
        own_defaults = field_defaults.get(
            NominalId(chain_typedef.decl_node_id), (None,) * len(chain_typedef.fields)
        )
        defaults_by_field.update(
            zip((name for name, _ in chain_typedef.fields), own_defaults, strict=True)
        )
    return _FlatFields(
        names,
        json_names,
        tuple(defaults_by_field.values()),
        positional_field_names(type_table.field_kinds(handle)),
    )


def _extend_fields(
    base: _FlatFields,
    typedef: TypeDef,
    type_table: TypeTable,
    field_defaults: Mapping[NominalId, tuple[IrExpr | None, ...]],
) -> _FlatFields:
    """Extend a base exception's flattened fields by those *typedef* declares itself.

    The own fields simply follow the base's, unless one redeclares an
    inherited field (the implicit root's ``message``): then the chain is
    flattened afresh. The positional prefix only changes when an own field is
    positional-only.
    """
    handle = typedef.exception_handle()
    own = type_table.own_json_fields(handle)
    if len(base.names) + len(own) != len(type_table.exception_fields(handle)):
        return _flatten_from_scratch(typedef, type_table, field_defaults)
    own_defaults = field_defaults.get(NominalId(typedef.decl_node_id), (None,) * len(own))
    return _FlatFields(
        (*base.names, *(name for name, _json_name, _type in own)),
        (*base.json_names, *(json_name for _name, json_name, _type in own)),
        (*base.defaults, *own_defaults),
        positional_field_names(type_table.field_kinds(handle))
        if has_positional_only(typedef.field_kinds)
        else base.positional,
    )


def exception_descriptor(
    typedef: TypeDef,
    type_table: TypeTable,
    *,
    bears_name_path: bool,
    field_defaults: Mapping[NominalId, tuple[IrExpr | None, ...]],
    built: Mapping[NominalId, NominalDescriptor],
) -> NominalDescriptor:
    """Build one exception descriptor from its authoritative declaration.

    *field_defaults* maps a declaration's own identity to its lowered
    per-field defaults (see ``_LinkState.field_defaults``); flattened base
    first across the ``extends`` chain — like ``fields`` itself — so an
    inherited field keeps its base's lowered default. A chain link absent
    from *field_defaults* contributes an all-``None`` run.

    The flattened data extends the nearest base descriptor already in *built*
    (the chain is walked only up to it), so building a hierarchy base first
    costs each exception its own fields plus a flat copy.
    """
    pending: list[TypeDef] = [typedef]
    flat = _NO_FIELDS
    while (base_id := pending[-1].base) is not None:
        base = built.get(NominalId(base_id))
        if base is not None:
            flat = _FlatFields(
                base.fields, base.field_json_names, base.field_defaults, base.positional_fields
            )
            break
        pending.append(type_table.typedef_of(base_id))
    for link in reversed(pending):
        flat = _extend_fields(flat, link, type_table, field_defaults)
    return NominalDescriptor(
        nominal=NominalId(typedef.decl_node_id),
        module_id=typedef.module_id,
        scope_path=typedef.scope_path,
        declared_name=typedef.name,
        kind=NominalKind.EXCEPTION,
        base=NominalId(typedef.base) if typedef.base is not None else None,
        fields=flat.names,
        field_json_names=flat.json_names,
        field_defaults=flat.defaults,
        variants=(),
        positional_fields=flat.positional,
        bears_name_path=bears_name_path,
    )


def enum_descriptor(
    typedef: TypeDef,
    handle: EnumType,
    type_table: TypeTable,
    *,
    bears_name_path: bool,
) -> NominalDescriptor:
    """Build one enum descriptor from its authoritative declaration."""
    return NominalDescriptor(
        nominal=NominalId(typedef.decl_node_id),
        module_id=typedef.module_id,
        scope_path=typedef.scope_path,
        declared_name=typedef.name,
        kind=NominalKind.ENUM,
        variants=variant_descriptors(handle, type_table),
        bears_name_path=bears_name_path,
    )


def record_descriptor(
    typedef: TypeDef,
    handle: RecordType,
    type_table: TypeTable,
    *,
    bears_name_path: bool,
    field_defaults: Mapping[NominalId, tuple[IrExpr | None, ...]],
) -> NominalDescriptor:
    """Build one record descriptor from its authoritative declaration."""
    nominal = NominalId(typedef.decl_node_id)
    fields, field_json_names = json_field_names(type_table, handle)
    return NominalDescriptor(
        nominal=nominal,
        module_id=typedef.module_id,
        scope_path=typedef.scope_path,
        declared_name=typedef.name,
        kind=NominalKind.RECORD,
        fields=fields,
        field_json_names=field_json_names,
        mutable_fields=typedef.mutable_fields,
        variants=(),
        positional_fields=positional_field_names(type_table.field_kinds(handle)),
        field_defaults=field_defaults.get(nominal, ()),
        bears_name_path=bears_name_path,
    )


def nominal_descriptor(
    typedef: TypeDef,
    handle: RecordType | EnumType | ExceptionType,
    type_table: TypeTable,
    *,
    bears_name_path: bool,
    field_defaults: Mapping[NominalId, tuple[IrExpr | None, ...]],
    built: Mapping[NominalId, NominalDescriptor],
) -> NominalDescriptor:
    """Build the descriptor matching *handle*'s kind (record, enum, or exception).

    *built* holds the descriptors already built; an exception extends its base's.
    """
    match handle:
        case RecordType():
            return record_descriptor(
                typedef,
                handle,
                type_table,
                bears_name_path=bears_name_path,
                field_defaults=field_defaults,
            )
        case EnumType():
            return enum_descriptor(typedef, handle, type_table, bears_name_path=bears_name_path)
        case _:
            return exception_descriptor(
                typedef,
                type_table,
                bears_name_path=bears_name_path,
                field_defaults=field_defaults,
                built=built,
            )
