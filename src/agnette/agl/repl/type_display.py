"""REPL-oriented static type display helpers.

The semantic ``Type.__repr__`` implementations are intentionally compact and
canonical because they are used throughout diagnostics, schemas, and ordinary
binding echoes.  Type-focused REPL surfaces (``:type`` and bare type entries)
can afford a richer view: for nominal records, enums and exceptions they print the relevant
field / constructor declarations rather than only the nominal name.

Record and enum handles carry no shape data; their field / variant declarations
are resolved on demand through the shared :class:`TypeTable`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from agnette.agl.repl.info_docs import format_doc_attribute
from agnette.agl.semantics.type_table import TypeTable
from agnette.agl.semantics.types import EnumType, ExceptionType, RecordType, Type
from agnette.agl.typecheck.env import GenericTypeDef


def format_type_for_repl(typ: Type, table: TypeTable | None) -> str:
    """Return the type-focused REPL display for ``typ``.

    Primitive, container, function, and unit types keep their canonical compact
    representation.  Records, enums, and exceptions expand to AgL-like
    declarations so a user can see the available fields or constructors at the
    prompt without finding the original declaration.  ``table`` resolves the
    field / variant shapes for the nominal handles; when it is ``None`` (no
    shared type table is available for the entry) nominal handles fall back to
    their compact ``repr`` form.
    """
    if table is not None:
        if isinstance(typ, RecordType):
            return _format_record_type(typ, table)
        if isinstance(typ, EnumType):
            return _format_enum_type(typ, table)
        if isinstance(typ, ExceptionType):
            return _format_exception_type_with_name(typ, repr(typ), table)
    return repr(typ)


def format_type_echo_for_repl(typ: Type, table: TypeTable | None) -> str:
    """Format a bare type-entry echo for the REPL.

    Single-line type displays use the ``<type: T>`` form. Expanded
    record / enum declarations are wrapped on their own lines so the declaration
    indentation stays readable.
    """
    return format_type_text_echo_for_repl(format_type_for_repl(typ, table))


def format_type_text_echo_for_repl(rendered: str) -> str:
    """Wrap pre-rendered type display text in the REPL ``<type…>`` echo form."""
    if "\n" in rendered:
        return f"<type:\n{rendered}\n>"
    return f"<type: {rendered}>"


def format_generic_type_def_for_repl(name: str, gdef: GenericTypeDef, table: TypeTable) -> str:
    """Return a declaration-like display for an unapplied generic type definition."""
    return format_type_def_for_repl(f"{name}[{', '.join(gdef.type_params)}]", gdef.template, table)


def format_type_def_for_repl(
    name: str, typ: RecordType | EnumType | ExceptionType, table: TypeTable
) -> str:
    """Return a declaration-like display of nominal type *typ* declared as *name*."""
    if isinstance(typ, RecordType):
        return _format_record_type_with_name(typ, name, table)
    if isinstance(typ, EnumType):
        return _format_enum_type_with_name(typ, name, table)
    return _format_exception_type_with_name(typ, name, table)


def _format_exception_type_with_name(typ: ExceptionType, name: str, table: TypeTable) -> str:
    """Render an exception as its declaration: base, then its own fields."""
    typedef = table.exception_def(typ)
    header = f"exception {name}"
    if typedef.base is not None:
        # ``extends`` takes a bare name, as the declaration spells it.
        header += f" extends {table.defs[typedef.base].name}"
    lines = [header]
    lines.extend(_field_lines(typedef.fields, dict(typedef.field_docs)))
    return "\n".join(lines)


def _field_lines(fields: Iterable[tuple[str, Type]], docs: Mapping[str, str]) -> list[str]:
    """Render *fields* one per line, each below its ``@doc`` attribute when it has one."""
    lines: list[str] = []
    for field_name, field_type in fields:
        if field_name in docs:
            lines.extend(
                f"  {line}" if line else ""
                for line in format_doc_attribute(docs[field_name]).split("\n")
            )
        lines.append(f"  {field_name}: {field_type!r}")
    return lines


def _format_record_type(typ: RecordType, table: TypeTable) -> str:
    return _format_record_type_with_name(typ, repr(typ), table)


def _format_record_type_with_name(typ: RecordType, name: str, table: TypeTable) -> str:
    fields = table.record_fields(typ)
    if not fields:
        return f"record {name}"
    lines = [f"record {name}"]
    lines.extend(_field_lines(fields.items(), table.field_docs(typ)))
    return "\n".join(lines)


def _format_enum_type(typ: EnumType, table: TypeTable) -> str:
    return _format_enum_type_with_name(typ, repr(typ), table)


def _format_enum_type_with_name(typ: EnumType, name: str, table: TypeTable) -> str:
    lines = [f"enum {name}"]
    for variant_name, member in table.enum_member_names(typ).items():
        fields = table.record_fields(member)
        docs = table.field_docs(member)
        member_doc = table.declaration_doc(member)
        head = "" if member_doc is None else f"{format_doc_attribute(member_doc)} "
        if fields:
            field_list = ", ".join(
                (f"{format_doc_attribute(docs[field_name])} " if field_name in docs else "")
                + f"{field_name}: {field_type!r}"
                for field_name, field_type in fields.items()
            )
            lines.append(f"  | {head}{variant_name}({field_list})")
        else:
            lines.append(f"  | {head}{variant_name}")
    return "\n".join(lines)
