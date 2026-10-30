"""Scope failures release temporary readers without shortening source lifetimes."""

from __future__ import annotations

from pathlib import Path
from weakref import ref

import pytest

from agnette.agl.diagnostics import AglError, AglTypeError
from agnette.agl.modules.ids import ModuleId
from agnette.agl.modules.loader import ModuleGraph
from agnette.agl.modules.roots import RootSet
from agnette.agl.scope.lookup import LookupKind
from agnette.agl.scope.program import resolve_program
from agnette.agl.scope.symbols import AglScopeError, DuplicateDeclarationError
from tests.agl.module_graph import load_graph
from tests.agl.qualifier_support import span_text

pytestmark = pytest.mark.usefixtures("no_cyclic_gc")


class _Roots(RootSet):
    """An ordinary input root set with weak-reference support."""


def _graph(tmp_path: Path, source: str) -> tuple[ModuleGraph, ref[_Roots]]:
    roots = _Roots(frozenset({tmp_path}))
    graph = load_graph(source, entry_path=None, roots=roots, default_stdlib=False)
    return graph, ref(roots)


@pytest.mark.parametrize(
    ("library", "body", "error_type", "selected"),
    [
        (False, "def broken() -> int = unknown_name", AglScopeError, "unknown_name"),
        (
            True,
            "def same() -> int = 1\ndef same() -> int = 2",
            DuplicateDeclarationError,
            "def same() -> int = 2",
        ),
        (True, "type Broken = Missing", AglTypeError, "Missing"),
        (True, "def broken() -> int = unknown_name", AglScopeError, "unknown_name"),
    ],
    ids=("direct-body", "header", "collect", "body"),
)
def test_rejected_scope_releases_its_inputs_without_gc(
    tmp_path: Path, library: bool, body: str, error_type: type[AglError], selected: str
) -> None:
    # The library is prepared before the entry's headers, so even a header
    # rejection must release an already installed resolver.
    (tmp_path / "lib.agl").write_text("def f() -> int = 1")
    source = ("import lib\n" if library else "") + body
    graph, roots = _graph(tmp_path, source)
    failure: AglError | None = None
    try:
        resolve_program(graph)
    except AglError as error:
        # Isolate scope ownership from intentional Python traceback ownership;
        # production expected-error boundaries are tested separately.
        error.__traceback__ = None
        failure = error
    assert type(failure) is error_type
    assert span_text(source, failure.span) == selected
    del graph

    # Keep the diagnostic alive: its semantic payload must not own the compiler.
    assert roots() is None


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("read_sources", [False, True])
def test_successful_scope_keeps_only_current_sources(
    tmp_path: Path, cached: bool, read_sources: bool
) -> None:
    (tmp_path / "lib.agl").write_text("def f() -> int = 1")
    graph, roots = _graph(tmp_path, "import lib\ndef answer() -> int = lib::f()")
    resolved = resolve_program(graph)
    if cached:
        previous = resolved
        resolved = resolve_program(graph, cached_modules=previous.modules)
        assert resolved.modules[graph.entry_id] is previous.modules[graph.entry_id]
        assert resolved.sources is not previous.sources
        del previous
    reader = ref(resolved.sources)
    if read_sources:
        sources = ref(resolved.sources(graph.entry_id))
        assert sources() is not None
        assert resolved.sources(graph.entry_id) is sources()
    del graph
    assert roots() is not None
    del resolved

    assert reader() is None
    assert roots() is None
    if read_sources:
        assert sources() is None


@pytest.mark.parametrize("cached", [False, True])
def test_standalone_source_callback_keeps_fresh_import_reads_alive(
    tmp_path: Path, cached: bool
) -> None:
    (tmp_path / "lib.agl").write_text(
        "scope Box\n  def f() -> int = 1\n  def g() -> int = 2\nend Box"
    )
    graph, roots = _graph(tmp_path, "import lib\nuse lib::Box::*\ndef answer() -> int = f()")
    resolved = resolve_program(graph)
    if cached:
        resolved = resolve_program(graph, cached_modules=resolved.modules)
    source = resolved.sources(graph.entry_id)
    owner = ref(source)
    assert source.contributed_at((), ("f",), LookupKind.VALUE).candidates
    callback = source.contributed_at
    del resolved, source, graph

    assert owner() is not None
    assert roots() is not None
    found = callback((), ("g",), LookupKind.VALUE)
    assert {candidate.target.key for candidate in found.candidates} == {
        (ModuleId.from_path("lib"), ("Box",), "g")
    }
    del callback
    assert owner() is None
    assert roots() is None
