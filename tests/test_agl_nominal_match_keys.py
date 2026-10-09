"""Nominal constructor keys unify record and enum-member match compilation."""

from __future__ import annotations

import pytest

import agnette.agl.matchcompile.compiler as compiler_module
from agnette.agl.matchcompile.model import (
    ClosedSignature,
    NominalConstructor,
    Occurrence,
    OccurrenceId,
    RootOccurrenceProvenance,
)
from agnette.agl.matchcompile.normalize import MatchCompileInvariantError, signature_for_type
from agnette.agl.semantics.types import EnumType, RecordType
from agnette.agl.syntax.nodes import Case
from agnette.agl.syntax.spans import SourceSpan
from agnette.agl.syntax.visitor import walk
from tests.agl.module_graph import resolve_and_check_inline_entry


def test_record_and_enum_member_signatures_use_nominal_record_constructors() -> None:
    checked = resolve_and_check_inline_entry(
        "record R\n  x: int\nenum E\n  | A(x: int)\nlet e: E = A(x = 1)\ncase e of | A(x = _) => 0",
    )
    cases: list[Case] = []
    walk(
        checked.resolved.program,
        lambda node: cases.append(node) if isinstance(node, Case) else None,
    )
    (case,) = cases
    enum_type = checked.node_types[case.subject.node_id]
    assert isinstance(enum_type, EnumType)
    enum_signature = signature_for_type(enum_type, checked.type_env.type_table)
    assert isinstance(enum_signature, ClosedSignature)
    (enum_constructor,) = enum_signature.constructors
    assert isinstance(enum_constructor, NominalConstructor)
    assert isinstance(enum_constructor.record_type, RecordType)

    record_type = checked.type_env.type_table.enum_members(enum_type)[0]
    record_signature = signature_for_type(record_type, checked.type_env.type_table)
    assert record_signature == enum_signature

    with pytest.raises(MatchCompileInvariantError, match="absent from its checked signature"):
        compiler_module._canonical_switch_constructor(
            NominalConstructor(record_type, ()),
            Occurrence(
                OccurrenceId(0),
                0,
                record_type,
                RootOccurrenceProvenance(0, SourceSpan(1, 1, 1, 1, 0, 0)),
            ),
            checked.type_env.type_table,
        )
