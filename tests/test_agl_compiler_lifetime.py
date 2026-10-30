"""Compiler scratch ownership ends without waiting for cyclic collection."""

from __future__ import annotations

import gc
from collections.abc import Callable, Iterable, Iterator
from typing import Concatenate
from weakref import ref

import pytest

from agnette.agl.ir.ids import SourceId
from agnette.agl.lower.lowerer import _LinkState, _Lowerer
from agnette.agl.lower.program import lower_program
from agnette.agl.matchcompile import compile_program_matches
from agnette.agl.modules.ids import ENTRY_ID
from agnette.agl.scope.program import ResolvedProgram, resolve_program
from agnette.agl.semantics.types import FunctionType, IntType, RecordType, TextType
from agnette.agl.syntax.nodes import FuncDef, LetDecl
from agnette.agl.typecheck.checker import _Checker
from agnette.agl.typecheck.constant_bindings import ModuleConstantBindings
from agnette.agl.typecheck.env import AglTypeError, TypeEnvironment
from agnette.agl.typecheck.function_inference import (
    CandidateModule,
    ModuleCandidateComponent,
    _candidate_functions,
    _function_dependencies,
)
from agnette.agl.typecheck.program import check_program
from agnette.util.graph import sccs
from tests.agl.module_graph import build_module_graph

pytestmark = pytest.mark.usefixtures("no_cyclic_gc")


def _observe_construction[T, **P](
    monkeypatch: pytest.MonkeyPatch,
    cls: type[T],
    initialize: Callable[Concatenate[T, P], None],
) -> list[ref[T]]:
    instances: list[ref[T]] = []

    def observe(instance: T, *args: P.args, **kwargs: P.kwargs) -> None:
        initialize(instance, *args, **kwargs)
        instances.append(ref(instance))

    monkeypatch.setattr(cls, "__init__", observe)
    return instances


def _resolve(source: str) -> ResolvedProgram:
    graph, _ = build_module_graph(source, origin_path=None, default_stdlib=False)
    return resolve_program(graph)


@pytest.mark.parametrize(
    "source",
    ["let value = 1", "record Box[T]\n  value: T\nlet item = Box(1)"],
)
def test_completed_checker_releases_scratch_but_preserves_module(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    resolved = _resolve(source)
    checkers = _observe_construction(monkeypatch, _Checker, _Checker.__init__)
    program = check_program(resolved)
    module = program.modules[ENTRY_ID]
    facts = module.type_env.own_facts()
    assert facts.entries
    del program, resolved

    assert checkers and all(owner() is None for owner in checkers)
    gc.collect()
    assert module.type_env.own_facts() == facts
    assert module.node_types
    binding = module.resolved.program.body.items[-1]
    assert isinstance(binding, LetDecl)
    binding_type = module.type_env.get_binding_type(binding.node_id)
    assert binding_type == module.node_types[binding.value.node_id]
    if isinstance(binding_type, IntType):
        assert binding_type == IntType()
    else:
        assert isinstance(binding_type, RecordType)
        assert module.type_env.type_table.record_fields(binding_type) == {"value": IntType()}


@pytest.mark.parametrize(
    "source",
    [
        "def identity(value: int) = value",
        "def first(value: int) = second(value)\ndef second(value: int) = value",
        "def odd(n: int) = if n == 0 => false else => even(n - 1)\n"
        "def even(n: int) = if n == 0 => true else => odd(n - 1)",
        'let outer = 1\ndef choose() =\n  let outer = "local"\n  outer',
        "record Box\n  value: int\ndef Box::read(self) = self.value\n"
        "def read(box: Box) = box.read()",
        "def first(value: int = second()) = value\ndef second() = 1",
    ],
)
def test_candidate_workflows_release_environments_after_outputs_drop(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    resolved = _resolve(source)
    environments = _observe_construction(monkeypatch, TypeEnvironment, TypeEnvironment.__init__)
    checked = check_program(resolved)
    module = checked.modules[ENTRY_ID]
    for item in module.resolved.program.body.items:
        if isinstance(item, FuncDef):
            signature = module.type_env.get_binding_type(item.node_id)
            assert isinstance(signature, FunctionType)
            if item.name == "choose":
                assert signature.result == TextType()
    del checked, module, resolved

    assert environments and all(owner() is None for owner in environments)


@pytest.mark.parametrize(
    "source",
    [
        'let value = 1\nlet invalid: int = "text"',
        'def first(value: int = "text") = value',
        "def first() = second()\ndef second() = first()",
    ],
)
def test_failed_checks_release_scratch_after_exception_drop(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    resolved = _resolve(source)
    checkers = _observe_construction(monkeypatch, _Checker, _Checker.__init__)
    environments = _observe_construction(monkeypatch, TypeEnvironment, TypeEnvironment.__init__)
    with pytest.raises(AglTypeError):
        check_program(resolved)
    del resolved

    assert checkers and all(owner() is None for owner in checkers)
    assert environments and all(owner() is None for owner in environments)


@pytest.mark.parametrize("binding", ["let", "var"])
def test_candidate_dependency_scan_releases_borrowed_environment(binding: str) -> None:
    resolved = _resolve(
        "def first(value: int = default()) = alias() + alias()\n"
        f"{binding} alias = second\ndef second() = 1\ndef default() = 2"
    )
    env = TypeEnvironment()
    owner = ref(env)
    module = CandidateModule(resolved.modules[ENTRY_ID].resolved, env, ENTRY_ID)
    functions = _candidate_functions(ModuleCandidateComponent((module,)))
    first, second, default = functions
    # Initializers are followed once; parameter defaults stay authoritative-only.
    assert _function_dependencies(functions) == {first: (second,), second: (), default: ()}
    assert tuple(functions) == (first, second, default)
    del functions, module, env, resolved

    assert owner() is None


class _Graph(dict[int, Iterable[int]]):
    """Weak-referenceable caller-owned graph."""


class _Key:
    def __init__(self, fail_at: int = 0) -> None:
        self.calls = 0
        self.fail_at = fail_at

    def key(self, node: int) -> int:
        self.calls += 1
        if self.calls == self.fail_at:
            raise ValueError
        return node


@pytest.mark.parametrize("fail_at", [0, 1, 3, 4])
def test_scc_releases_borrowed_graph_and_key_on_success_or_sort_error(fail_at: int) -> None:
    graph = _Graph({1: (2,), 2: ()})
    key = _Key(fail_at)
    graph_owner, key_owner = ref(graph), ref(key)
    if fail_at:
        with pytest.raises(ValueError):
            sccs(graph, key=key.key)
    else:
        assert sccs(graph, key=key.key) == ((2,), (1,))
    assert graph == {1: (2,), 2: ()}
    del graph, key

    assert graph_owner() is None
    assert key_owner() is None


def test_scc_releases_inputs_after_successor_iteration_error() -> None:
    def successors() -> Iterator[int]:
        yield 2
        raise ValueError

    graph = _Graph({1: successors(), 2: ()})
    key = _Key()
    graph_owner, key_owner = ref(graph), ref(key)
    with pytest.raises(ValueError):
        sccs(graph, key=key.key)
    assert set(graph) == {1, 2}
    del graph, key

    assert graph_owner() is None
    assert key_owner() is None


def test_scc_observes_graph_mutation_during_component_sort() -> None:
    graph = _Graph({1: (2, 3), 2: (), 3: ()})
    calls = 0

    def key(node: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 4:
            # The first component closes after the outer keys have been sorted.
            graph[3] = (4,)
        return node

    assert sccs(graph, key=key) == ((2,), (4,), (3,), (1,))
    assert graph == {1: (2, 3), 2: (), 3: (4,)}


def test_standalone_constant_classifier_owns_its_callbacks() -> None:
    class Classifier:
        def classify(self, node_id: int) -> bool:
            return node_id >= 0

    resolved = _resolve("record Box\nlet item = Box()")
    classifier = Classifier()
    owner = ref(classifier)
    bindings = ModuleConstantBindings(
        resolved.modules[ENTRY_ID].resolved,
        ENTRY_ID,
        is_constructor=classifier.classify,
    )
    binding = resolved.modules[ENTRY_ID].resolved.program.body.items[-1]
    assert isinstance(binding, LetDecl)
    del classifier, resolved
    assert owner() is not None
    assert bindings.is_constant(binding.value)
    del bindings
    assert owner() is None


def test_completed_lowering_releases_scratch(monkeypatch: pytest.MonkeyPatch) -> None:
    compiled = compile_program_matches(check_program(_resolve("let value = 1"))).compiled
    assert compiled is not None
    lowerers = _observe_construction(monkeypatch, _Lowerer, _Lowerer.__init__)
    links = _observe_construction(monkeypatch, _LinkState, _LinkState.__init__)
    executable = lower_program(compiled)
    assert executable.modules
    assert lowerers and all(owner() is None for owner in lowerers)
    assert links and all(owner() is None for owner in links)


def test_retained_lower_callback_owns_usable_lowerer() -> None:
    checked = check_program(_resolve("let value = 1"))
    module = checked.modules[ENTRY_ID]
    binding = module.resolved.program.body.items[0]
    assert isinstance(binding, LetDecl)
    assert module.type_env.get_binding_type(binding.node_id) == IntType()
    lowerer = _Lowerer(module, _LinkState(), ENTRY_ID, SourceId(0), "let value = 1", {})
    owner = ref(lowerer)
    callback = lowerer.lower_expr
    del lowerer, module, checked

    assert owner() is not None
    assert callback(binding.value) is not None
    del callback
    assert owner() is None
