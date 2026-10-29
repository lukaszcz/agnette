"""Independent declaration snapshots retain valid analysis work across REPL entries."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agnette.agl.constraints import ConstraintKind
from agnette.agl.modules.ids import ENTRY_ID, ModuleId
from agnette.agl.repl import ReplSession
from agnette.agl.scope.program import resolve_program
from agnette.agl.semantics import analyses
from agnette.agl.semantics.external_names import ExternalName
from agnette.agl.semantics.type_table import MethodDef, TypeDef, TypeTable
from agnette.agl.semantics.types import (
    EXCEPTION_BASE,
    FunctionType,
    IntType,
    TextType,
    TypeVarType,
    UnitType,
)
from agnette.agl.semantics.values import IntValue
from agnette.agl.typecheck.env import TypeEnvironment
from agnette.agl.typecheck.program import _build_program_type_table
from agnette.agl.zones import ParamZone
from tests._agl_helpers import agl_roots
from tests.agl.module_graph import load_graph


def _record(name: str, identity: int) -> TypeDef:
    return TypeDef("record", name, ENTRY_ID, decl_node_id=identity)


def _exception(name: str, identity: int, base: int | None = None) -> TypeDef:
    return TypeDef("exception", name, ENTRY_ID, decl_node_id=identity, base=base)


@pytest.mark.parametrize("mutate_source", [False, True])
def test_declarations_names_orphans_and_proofs_are_independent(mutate_source: bool) -> None:
    source = TypeTable()
    original = _record("Box", 1)
    orphan = _record("Lost", 2)
    for definition in (original, orphan):
        source.register(definition)
    source.orphan(2)
    assert source.nominal_satisfies(original.record_handle(), ConstraintKind.HASHABLE, {})
    fork = source.fork()
    changed, untouched = (source, fork) if mutate_source else (fork, source)
    replacement = _record("Box", 3)
    changed.register(replacement)
    changed.orphan(1)
    assert changed.nominal_satisfies(replacement.record_handle(), ConstraintKind.HASHABLE, {})
    assert untouched.named(ENTRY_ID, "Box") is original
    assert untouched.get_by_id(3) is None
    assert untouched.get_by_id(2) is orphan
    assert untouched.get(ENTRY_ID, "Lost") is None
    assert untouched.hashable_proofs == {original.record_handle()}
    assert changed.hashable_proofs == {original.record_handle(), replacement.record_handle()}
    # Orphan state survives another generation, not just the initial fork.
    assert changed.fork().get(ENTRY_ID, "Box") is replacement
    assert not untouched.fork().is_current(orphan)


@pytest.mark.parametrize("mutate_source", [False, True])
def test_cached_shapes_and_inline_member_indexes_are_independent(mutate_source: bool) -> None:
    source = TypeTable()
    member = replace(
        _record("Item", 1),
        scope_path=("Choice",),
        type_params=("T",),
        fields=(("value", TypeVarType("T")),),
        field_has_default=None,
        is_inline_enum_member=True,
    )
    owner = TypeDef(
        "enum",
        "Choice",
        ENTRY_ID,
        decl_node_id=2,
        type_params=("T",),
        members=(member.record_handle((TypeVarType("T"),)),),
    )
    base = replace(
        _exception("Root", 3),
        fields=(("code", IntType()),),
        field_kinds=(ParamZone.STANDARD,),
        field_has_default=(True,),
        field_external_names=(("code", ExternalName("status", "status")),),
    )
    child = replace(
        _exception("Child", 4, 3),
        fields=(("detail", TextType()),),
        field_has_default=None,
        field_kinds=(ParamZone.STANDARD,),
    )
    for definition in (member, owner, base, child):
        source.register(definition)
    record = member.record_handle((IntType(),))
    enum = owner.enum_handle((IntType(),))
    exception = child.exception_handle()
    source.record_fields(record)
    source.enum_members(enum)
    source.enum_member_names(enum)
    source.enum_member_ids(enum)
    source.exception_fields(exception)
    source.exception_field_defs(4)
    source.field_external_names(exception)
    source.field_kinds(exception)
    source.field_has_default(exception)
    source.owning_enums_for_selection(record)
    fork = source.fork()
    changed, untouched = (source, fork) if mutate_source else (fork, source)
    # Returned mappings are mutable implementations of read-only query APIs;
    # neither their storage nor their memo buckets may cross the fork boundary.
    assert changed.record_fields(record) == untouched.record_fields(record) == {"value": IntType()}
    assert changed.record_fields(record) is not untouched.record_fields(record)
    assert changed.enum_member_names(enum) is not untouched.enum_member_names(enum)
    assert changed.enum_member_ids(enum) is not untouched.enum_member_ids(enum)
    assert changed.exception_fields(exception) is not untouched.exception_fields(exception)
    assert changed.field_external_names(exception) is not untouched.field_external_names(exception)
    changed.record_fields(member.record_handle((TextType(),)))
    changed.enum_members(owner.enum_handle((TextType(),)))
    extra = replace(member, name="Extra", decl_node_id=5)
    changed.register(extra)
    changed.declare_inline_member(2, ("T",), extra.record_handle((TypeVarType("T"),)))
    assert changed.inline_member(enum, "Extra") == extra.record_handle((IntType(),))
    assert untouched.inline_member_record_names(ENTRY_ID, ("Choice",)) == ("Item",)
    assert untouched.inline_member(enum, "Item") == record
    with pytest.raises(KeyError):
        untouched.inline_member(enum, "Extra")
    overwrite = TypeTable()
    overwrite.register(replace(base, fields=(("new", TextType()),), field_has_default=(False,)))
    overwrite.register(replace(member, fields=(("changed", TextType()),)))
    overwrite.register(replace(owner, members=()))
    changed.merge_from(overwrite)
    assert changed.record_fields(record) == {"changed": TextType()}
    assert changed.enum_members(enum) == ()
    assert changed.enum_member_names(enum) == {}
    assert changed.enum_member_ids(enum) == {}
    assert changed.exception_fields(exception) == {"new": TextType(), "detail": TextType()}
    assert untouched.record_fields(record) == {"value": IntType()}
    assert untouched.enum_members(enum) == (record,)
    assert untouched.enum_member_names(enum) == {"Item": record}
    assert untouched.enum_member_ids(enum) == {1: record}
    assert untouched.exception_fields(exception) == {"code": IntType(), "detail": TextType()}
    assert untouched.exception_field_defs(4) == (base, child)
    assert dict(untouched.field_external_names(exception)) == {
        "code": ExternalName("status", "status")
    }
    assert untouched.field_kinds(exception)[0] == ("code", ParamZone.STANDARD)
    assert untouched.field_has_default(exception) == (("code", True), ("detail", False))


def test_analysis_results_are_reused_then_invalidated_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = TypeTable()
    root = _exception("Root", 1)
    box = replace(_record("Box", 2), fields=(("value", IntType()),), field_has_default=None)
    source.register(root)
    source.register(box)
    assert source.nominal_satisfies(root.exception_handle(), ConstraintKind.HASHABLE, {})
    assert source.nominal_is_json_convertible(box.record_handle())
    assert source.nominal_is_extern_keyable(box.record_handle())
    assert not source.nominal_reaches_non_data(box.record_handle())
    assert source.has_finite_schema(box.record_handle())
    source.exception_children(1)
    source.exception_nesting()
    source.opaque_declaration_ids()
    source.standard_builtin_declarations()
    fork = source.fork()
    with monkeypatch.context() as patch:

        def unexpected(*args: object, **kwargs: object) -> object:
            pytest.fail("fork discarded valid whole-table analysis")

        patch.setattr(analyses, "compute_declaration_flags", unexpected)
        patch.setattr(analyses, "compute_finite_closure", unexpected)
        patch.setattr(analyses, "compute_relevant_params", unexpected)
        assert fork.nominal_satisfies(root.exception_handle(), ConstraintKind.HASHABLE, {})
        assert fork.nominal_is_json_convertible(box.record_handle())
        assert fork.nominal_is_extern_keyable(box.record_handle())
        assert not fork.nominal_reaches_non_data(box.record_handle())
        assert fork.has_finite_schema(box.record_handle())
    bad_child = replace(
        _exception("Bad", 3, 1), fields=(("bad", UnitType()),), field_has_default=None
    )
    fork.register(bad_child)
    assert fork.exception_children(1) == (3,)
    assert fork.is_exception_ancestor(1, 3)
    assert not fork.nominal_satisfies(root.exception_handle(), ConstraintKind.HASHABLE, {})
    assert source.exception_children(1) == ()
    assert source.nominal_satisfies(root.exception_handle(), ConstraintKind.HASHABLE, {})
    opaque = replace(_record("Hidden", 4), opaque=True)
    source.register(opaque)
    assert source.opaque_declaration_ids() == {4}
    assert fork.opaque_declaration_ids() == frozenset()
    fork.orphan(3)
    assert fork.exception_children(1) == ()
    assert fork.nominal_satisfies(root.exception_handle(), ConstraintKind.HASHABLE, {})


@pytest.mark.parametrize("mutate_source", [False, True])
def test_method_retirement_and_updates_use_copied_candidate_buckets(mutate_source: bool) -> None:
    source = TypeTable()
    owner = _record("Box", 1).record_handle()
    method = MethodDef(ENTRY_ID, ("Box",), "show", 10, FunctionType((owner,), TextType()), 0)
    builtin = replace(
        method,
        scope_path=("text",),
        decl_node_id=11,
        signature=FunctionType((TextType(),), TextType()),
        is_builtin=True,
    )
    source.register_method(owner, method)
    source.register_builtin_method("text", builtin)
    fork = source.fork()
    changed, untouched = (source, fork) if mutate_source else (fork, source)
    moved = replace(method, decl_node_id=12, signature=builtin.signature, is_builtin=True)
    changed.register_builtin_method("text", moved)
    assert changed.method_candidates(owner, "show") == ()
    assert untouched.method_candidates(owner, "show") == (method,)
    updated = replace(builtin, decl_node_id=13, signature=FunctionType((owner,), IntType()))
    changed.register_method(owner, updated)
    assert changed.method_candidates(TextType(), "show") == (moved,)
    assert untouched.method_candidates(TextType(), "show") == (builtin,)
    changed.rewind_methods_from(untouched, {12, 13})
    assert changed.method_candidates(owner, "show") == (method,)
    assert changed.method_candidates(TextType(), "show") == (builtin,)
    changed.register_method(owner, replace(method, decl_node_id=14))
    assert untouched.method_candidates(owner, "show") == (method,)


def test_shell_publications_are_compile_local_but_registered_builtins_survive() -> None:
    source = TypeTable()
    root = replace(
        _exception("Exception", 1), module_id=ModuleId.from_path("std/errors"), is_builtin=True
    )
    source.register(root)
    source.standard_builtin_declarations()
    provisional = replace(root, decl_node_id=2).exception_handle()
    source.declare_standard_builtin_exception(provisional)
    fork = source.fork()
    assert source.exception_root() == provisional
    assert fork.exception_root() == root.exception_handle()
    assert fork.standard_builtin_declaration("Exception") is root
    fork.orphan(1)
    assert fork.exception_root() == EXCEPTION_BASE
    assert source.builtin_declaration("Exception") is root
    assert source.standard_builtin_declaration("Exception") is root
    fork.declare_standard_builtin_exception(replace(root, decl_node_id=3).exception_handle())
    assert source.exception_root() == provisional
    assert fork.fork().exception_root() == EXCEPTION_BASE


def test_direct_type_prepass_call_still_merges_session_declarations(tmp_path: Path) -> None:
    seed = TypeEnvironment()
    declaration = _record("Retained", 12345)
    seed.type_table.register(declaration)
    graph = load_graph(
        "let value = 0", entry_path=None, roots=agl_roots(tmp_path), default_stdlib=False
    )
    resolved = resolve_program(graph)
    target = TypeTable()
    _build_program_type_table(resolved, type_table=target, entry_seed_env=seed)
    assert target.named(ENTRY_ID, "Retained") is declaration
    assert target is not seed.type_table


def test_repeated_repl_checking_forks_without_stale_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = ReplSession(cwd=tmp_path)
    assert session.open() == ()
    try:
        assert session.eval_entry("record Box\n  value: int\nlet old = Box(1)").error is None

        def unexpected_merge(self: TypeTable, other: TypeTable) -> None:
            pytest.fail("entry reseeded an already-forked type table")

        monkeypatch.setattr(TypeTable, "merge_from", unexpected_merge)
        assert session.eval_entry("40 + 2").value == IntValue(42)
        assert session.eval_entry("record Box\n  value: text").error is None
        assert session.eval_entry('Box("new").value').error is None
        assert session.eval_entry("old.value").value == IntValue(1)
    finally:
        session.close()
