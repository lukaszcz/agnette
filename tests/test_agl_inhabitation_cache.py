"""Inhabitation reuse must follow immutable solver inputs, not table identity."""

from collections.abc import Iterator
from dataclasses import replace

import pytest

from agnette.agl.modules.ids import ENTRY_ID
from agnette.agl.semantics import analyses
from agnette.agl.semantics.type_table import TypeDef, TypeTable
from agnette.agl.semantics.types import ArrayType, EnumType, IntType, RecordType, TypeVarType


@pytest.fixture()
def solver_runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[None]]:
    """Count actual fixpoint executions without replacing their behavior."""
    analyses._compute_uninhabited_cached.cache_clear()
    runs: list[None] = []
    solve = analyses._InhabitationSolver._solve

    def counted_solve(solver: analyses._InhabitationSolver) -> None:
        runs.append(None)
        solve(solver)

    monkeypatch.setattr(analyses._InhabitationSolver, "_solve", counted_solve)
    yield runs
    analyses._compute_uninhabited_cached.cache_clear()


def _table(*definitions: TypeDef) -> TypeTable:
    table = TypeTable()
    for definition in definitions:
        table.register(definition)
    return table


def test_equivalent_fresh_tables_reuse_the_real_solver(solver_runs: list[None]) -> None:
    def definition() -> TypeDef:
        return TypeDef(kind="enum", name="Empty", module_id=ENTRY_ID, decl_node_id=910001)

    original = _table(definition())
    equivalent = _table(definition())
    first = analyses.compute_uninhabited(original)
    assert first == frozenset({910001})
    assert analyses.compute_uninhabited(original) == first
    assert analyses.compute_uninhabited(equivalent) == first
    assert len(solver_runs) == 1


def test_changed_definition_with_the_same_identity_is_not_reused(solver_runs: list[None]) -> None:
    definition = TypeDef(kind="record", name="Node", module_id=ENTRY_ID, decl_node_id=910101)
    original = _table(definition)
    recursive = _table(
        replace(definition, fields=(("next", definition.handle()),), field_has_default=None)
    )

    assert analyses.compute_uninhabited(original) == frozenset()
    assert analyses.compute_uninhabited(recursive) == frozenset({910101})
    assert analyses.compute_uninhabited(original) == frozenset()
    assert len(solver_runs) == 2


def test_equal_typedefs_with_different_declaration_ids_are_not_reused(
    solver_runs: list[None],
) -> None:
    first = TypeDef(kind="enum", name="Empty", module_id=ENTRY_ID, decl_node_id=910111)
    second = replace(first, decl_node_id=910112)
    assert first == second

    assert analyses.compute_uninhabited(_table(first)) == frozenset({910111})
    assert analyses.compute_uninhabited(_table(second)) == frozenset({910112})
    assert len(solver_runs) == 2


def _recursive_generics(*, base_case: bool) -> tuple[TypeDef, ...]:
    parameter = TypeVarType("T")
    bad = RecordType("Bad", module_id=ENTRY_ID, decl_id=910201)
    box = RecordType("Box", module_id=ENTRY_ID, decl_id=910202)
    leaf = RecordType("Leaf", module_id=ENTRY_ID, decl_id=910203, type_args=(parameter,))
    step = RecordType("Step", module_id=ENTRY_ID, decl_id=910204, type_args=(parameter,))
    tree = EnumType("Tree", module_id=ENTRY_ID, decl_id=910205)
    loop = RecordType("Loop", module_id=ENTRY_ID, decl_id=910207)
    return (
        TypeDef(
            kind="record",
            name="Bad",
            module_id=ENTRY_ID,
            decl_node_id=bad.decl_id,
            fields=(("next", bad),),
        ),
        TypeDef(
            kind="record",
            name="Box",
            module_id=ENTRY_ID,
            decl_node_id=box.decl_id,
            type_params=("T",),
            fields=(("value", parameter),),
        ),
        TypeDef(
            kind="record",
            name="Leaf",
            module_id=ENTRY_ID,
            decl_node_id=leaf.decl_id,
            type_params=("T",),
            fields=(("value", parameter),),
        ),
        TypeDef(
            kind="record",
            name="Step",
            module_id=ENTRY_ID,
            decl_node_id=step.decl_id,
            type_params=("T",),
            fields=(("next", replace(tree, type_args=(ArrayType(parameter),))),),
        ),
        TypeDef(
            kind="enum",
            name="Tree",
            module_id=ENTRY_ID,
            decl_node_id=tree.decl_id,
            type_params=("T",),
            members=(leaf, step) if base_case else (step,),
        ),
        TypeDef(
            kind="record",
            name="User",
            module_id=ENTRY_ID,
            decl_node_id=910206,
            fields=(("value", replace(box, type_args=(replace(tree, type_args=(bad,)),))),),
        ),
        TypeDef(
            kind="record",
            name="Loop",
            module_id=ENTRY_ID,
            decl_node_id=loop.decl_id,
            type_params=("T",),
            fields=(("next", replace(loop, type_args=(ArrayType(parameter),))),),
        ),
        TypeDef(
            kind="record",
            name="BadUser",
            module_id=ENTRY_ID,
            decl_node_id=910208,
            fields=(("value", replace(box, type_args=(replace(loop, type_args=(IntType(),)),))),),
        ),
    )


def test_recursive_generic_results_are_reused_without_changing_fixpoint_semantics(
    solver_runs: list[None],
) -> None:
    inhabited = _table(*_recursive_generics(base_case=True))
    no_base_case = _table(*_recursive_generics(base_case=False))
    equivalent = _table(*reversed(_recursive_generics(base_case=True)))

    expected = frozenset({910201, 910207, 910208})
    first = analyses.compute_uninhabited(inhabited)
    assert first == expected
    assert analyses.compute_uninhabited(no_base_case) == expected | {910204, 910205, 910206}
    assert analyses.compute_uninhabited(equivalent) == expected
    assert len(solver_runs) == 2


def _exceptions() -> tuple[TypeDef, TypeDef]:
    root = TypeDef(
        kind="exception", name="Root", module_id=ENTRY_ID, decl_node_id=910301, abstract=True
    )
    child = TypeDef(
        kind="exception",
        name="Child",
        module_id=ENTRY_ID,
        decl_node_id=910302,
        base=root.decl_node_id,
    )
    return root, child


@pytest.mark.parametrize("orphan_id", [910301, 910302], ids=["root", "child"])
def test_orphaning_changes_live_topology_without_changing_definitions(
    orphan_id: int, solver_runs: list[None]
) -> None:
    root, child = _exceptions()
    table = _table(root, child)
    equivalent = _table(*_exceptions())
    assert analyses.compute_uninhabited(table) == frozenset()

    table.orphan(orphan_id)
    assert table.defs == equivalent.defs
    assert analyses.compute_uninhabited(table) == frozenset({root.decl_node_id})
    assert analyses.compute_uninhabited(equivalent) == frozenset()
    assert len(solver_runs) == 2


def test_adding_an_exception_child_does_not_mutate_the_cached_result(
    solver_runs: list[None],
) -> None:
    root, child = _exceptions()
    table = _table(root)
    first = analyses.compute_uninhabited(table)
    assert first == frozenset({root.decl_node_id})

    table.register(child)
    assert analyses.compute_uninhabited(table) == frozenset()
    assert analyses.compute_uninhabited(_table(*_exceptions())) == frozenset()
    assert first == frozenset({root.decl_node_id})
    assert len(solver_runs) == 2


def test_cache_evicts_old_inputs_after_at_most_128_entries(solver_runs: list[None]) -> None:
    definition = TypeDef(kind="enum", name="Empty", module_id=ENTRY_ID, decl_node_id=910401)
    for index in range(129):
        table = _table(replace(definition, name=f"Empty{index}"))
        assert analyses.compute_uninhabited(table) == frozenset({definition.decl_node_id})
    assert len(solver_runs) == 129
    assert analyses.compute_uninhabited(table) == frozenset({definition.decl_node_id})
    assert len(solver_runs) == 129

    assert analyses.compute_uninhabited(_table(replace(definition, name="Empty0"))) == frozenset(
        {definition.decl_node_id}
    )
    assert len(solver_runs) == 130
