"""Match analysis borrows caller context only for the duration of each traversal."""

from __future__ import annotations

import gc
from dataclasses import replace
from typing import cast
from weakref import ref

import pytest

from agnette.agl.matchcompile import compiler
from agnette.agl.matchcompile.compiler import CompiledMatchSite, compile_match_site
from agnette.agl.matchcompile.model import (
    BoolConstructor,
    DecisionLeaf,
    DecisionSwitch,
)
from agnette.agl.matchcompile.normalize import MatchCompileInvariantError, normalize_case
from agnette.agl.semantics.types import RecordType
from agnette.agl.syntax.nodes import Case
from agnette.agl.syntax.visitor import walk
from tests.agl.module_graph import resolve_and_check_inline_entry

pytestmark = pytest.mark.usefixtures("no_cyclic_gc")


def _compiled(source: str = "case true of | true => 1 | false => 2") -> CompiledMatchSite:
    checked = resolve_and_check_inline_entry(source, default_stdlib=False)
    cases: list[Case] = []

    def collect(node: object) -> None:
        if isinstance(node, Case):
            cases.append(node)

    walk(checked.resolved.program, collect)
    compiled = compile_match_site(normalize_case(cases[0], checked))
    # Isolate the traversal under test from scratch left by artifact construction.
    gc.collect()
    return compiled


def _missing_nominal_switch(compiled: CompiledMatchSite) -> DecisionSwitch:
    root = compiled.root
    assert isinstance(root, DecisionSwitch)
    occurrence = replace(root.occurrence, type=RecordType("Missing", decl_id=-1))
    return replace(root, occurrence=occurrence)


@pytest.mark.parametrize("fail", [False, True])
def test_failure_analysis_releases_borrowed_type_table(fail: bool) -> None:
    compiled = _compiled()
    table = compiled.normalized.type_table
    owner = ref(table)
    if fail:
        compiled = replace(compiled, root=_missing_nominal_switch(compiled))
        with pytest.raises(KeyError):
            compiler._first_failure_constraints(compiled.root, table)
    else:
        assert compiler._first_failure_constraints(compiled.root, table) is None
    # The caller's table remains usable until the caller drops it.
    assert table is compiled.normalized.type_table
    del compiled, table
    assert owner() is None


class _BorrowedLeaf(DecisionLeaf):
    """Weak-referenceable input decision; no production ownership changes."""


@pytest.mark.parametrize("fail", [False, True])
def test_semantic_replay_releases_borrowed_decision_graph(fail: bool) -> None:
    compiled = _compiled("case true of | _ => 1")
    root = compiled.root
    assert isinstance(root, DecisionLeaf)
    borrowed = _BorrowedLeaf(root.action_id, root.binder_assignments)
    owner = ref(borrowed)
    compiled = replace(compiled, root=borrowed)
    if fail:
        # Replay completes before the ledger mismatch is rejected.
        compiled = replace(compiled, occurrences=())
        with pytest.raises(MatchCompileInvariantError):
            compiler._validate_semantic_replay(compiled)
    else:
        compiler._validate_semantic_replay(compiled)
    assert borrowed.action_id in compiled.reachable_action_ids
    del compiled, borrowed, root
    assert owner() is None


class _BorrowedSwitch(DecisionSwitch):
    """Weak-referenceable input decision with recursive successors."""


def test_recursive_replay_releases_graph_after_branch_error() -> None:
    compiled = _compiled()
    root = compiled.root
    assert isinstance(root, DecisionSwitch)
    first, second = root.keyed_children
    branches = (first, replace(second, decision=DecisionLeaf(-1, ())))
    borrowed = _BorrowedSwitch(root.occurrence, branches, root.default, root.free_occurrences)
    owner = ref(borrowed)
    compiled = replace(compiled, root=borrowed)

    # The first branch replays successfully before the foreign action is rejected.
    with pytest.raises(MatchCompileInvariantError):
        compiler._validate_semantic_replay(compiled)
    del compiled, borrowed, root
    assert owner() is None


@pytest.mark.parametrize("fail", [False, True])
def test_decision_validation_releases_borrowed_checked_context(fail: bool) -> None:
    compiled = _compiled("case 1 of | 1 => 1 | _ => 2")
    owner = ref(compiled.normalized.type_table)
    ledger, groups = compiler._validate_occurrence_ledger(compiled.normalized, compiled.occurrences)
    if fail:
        root = compiled.root
        assert isinstance(root, DecisionSwitch)
        branch = replace(root.keyed_children[0], constructor=BoolConstructor(False))
        compiled = replace(compiled, root=replace(root, keyed_children=(branch,)))
        del root, branch
        with pytest.raises(MatchCompileInvariantError):
            compiler._validate_compiled_decisions(compiled, ledger, groups)
    else:
        compiler._validate_compiled_decisions(compiled, ledger, groups)
    assert compiled.root.free_occurrences
    del compiled, ledger, groups
    assert owner() is None


@pytest.mark.parametrize(
    "source",
    [
        "record Marker\ncase Marker() of | Marker() => 1",
        "record Box\n  value: bool\ncase Box(true) of | Box(value) => value",
        "case 1 of | 1 => 1 | _ => 2",
        "case true of | true => 1",
    ],
)
def test_completed_validation_releases_context_after_artifact_drop(source: str) -> None:
    compiled = _compiled(source)
    owner = ref(compiled.normalized.type_table)
    compiler.validate_compiled_case(compiled)
    # Retaining the artifact intentionally owns its checked context.
    gc.collect()
    assert owner() is compiled.normalized.type_table
    assert compiled.reachable_action_ids
    del compiled
    assert owner() is None


@pytest.mark.parametrize("traversal", ["failure", "replay", "decisions"])
def test_retained_exception_owns_context_only_until_traceback_drop(traversal: str) -> None:
    compiled = _compiled()
    owner = ref(compiled.normalized.type_table)

    retained: Exception | None = None
    try:
        if traversal == "failure":
            compiled = replace(compiled, root=_missing_nominal_switch(compiled))
            compiler._first_failure_constraints(compiled.root, compiled.normalized.type_table)
        elif traversal == "replay":
            compiled = replace(compiled, root=DecisionLeaf(-1, ()))
            compiler._validate_semantic_replay(compiled)
        else:
            compiled = replace(compiled, root=DecisionLeaf(-1, ()))
            ledger, groups = compiler._validate_occurrence_ledger(
                compiled.normalized, compiled.occurrences
            )
            compiler._validate_compiled_decisions(compiled, ledger, groups)
    except (KeyError, MatchCompileInvariantError) as error:
        retained = error
    assert retained is not None
    assert retained.__traceback__ is not None
    details = cast(tuple[object, ...], retained.args)
    if traversal == "failure":
        assert isinstance(retained, KeyError)
        assert details == (-1,)
    else:
        assert isinstance(retained, MatchCompileInvariantError)
        assert details
    del compiled
    assert owner() is not None
    retained.__traceback__ = None
    assert owner() is None
    assert cast(tuple[object, ...], retained.args) == details


def test_binder_scan_does_not_retain_checked_context() -> None:
    compiled = _compiled("case true of | _ as value => value")
    owner = ref(compiled.normalized.type_table)
    paths = compiler._source_binder_paths(compiled.normalized)
    assert paths[compiled.reachable_action_ids[0]]
    del compiled
    assert owner() is None
    assert paths


def test_failed_shape_validation_does_not_retain_borrowed_graph() -> None:
    compiled = _compiled()
    root = compiled.root
    assert isinstance(root, DecisionSwitch)
    borrowed = _BorrowedSwitch(root.occurrence, (), None, root.free_occurrences)
    owner = ref(borrowed)
    with pytest.raises(MatchCompileInvariantError):
        compiler.validate_decision_dag(borrowed)
    del compiled, root, borrowed
    assert owner() is None


@pytest.mark.parametrize("traversal", ["reachability", "shape"])
def test_integer_only_traversals_do_not_retain_borrowed_graph(traversal: str) -> None:
    root = _BorrowedLeaf(7, ())
    owner = ref(root)
    if traversal == "reachability":
        assert compiler._reachable_actions(root) == {7}
    else:
        compiler.validate_decision_dag(root)
    assert root.action_id == 7
    del root
    assert owner() is None
