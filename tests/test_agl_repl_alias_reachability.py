"""Retained aliases expose the same constructors to entries and match witnesses."""

from __future__ import annotations

import gc
import re
from pathlib import Path
from weakref import ref

import pytest

from agnette.agl.diagnostics import HiddenMemberError
from agnette.agl.matchcompile.diagnostics import NonExhaustiveMatchError
from agnette.agl.repl.session import ReplSession
from agnette.agl.scope.lookup import LookupKind
from agnette.agl.scope.program import resolve_program
from agnette.agl.scope.symbols import ScopeNode
from agnette.agl.semantics.values import IntValue
from agnette.agl.syntax.nodes import EnumDef, TypeAlias, VariantRef
from agnette.agl.syntax.types import AppliedT, NameT
from tests.agl.ir_harness import make_inline_graph_from_files, make_repl_graph_from_files


@pytest.mark.parametrize("alias", ["type C = E", "type K = E\ntype C = K"])
@pytest.mark.parametrize(
    ("opening", "changes", "reachable"),
    [
        ("use s::*", ("use s::* hiding E::A",), False),
        ("use s::* hiding E::A", ("use s::*",), True),
        ("use s::*", ("enum E = A | B", "use s::* hiding E::A"), True),
        (
            "use s::* hiding E::A",
            ("scope t\n  enum E = A | B\nend t", "use t::*", "use s::*"),
            False,
        ),
        ("use s::*", ("use C::*\nuse s::* hiding E::A",), False),
    ],
    ids=["narrowing", "widening", "shadow-freeze", "ambiguity-freeze", "use-horizon"],
)
def test_retained_alias_witness_is_writable(
    alias: str, opening: str, changes: tuple[str, ...], reachable: bool
) -> None:
    session = ReplSession(default_stdlib=False)
    for entry in (
        "scope s\n  enum E = A | B\nend s",
        opening,
        alias,
        *changes,
    ):
        result = session.eval_entry(entry)
        assert result.ok, result.diagnostics

    if reachable:
        assert session.eval_entry("C::A").ok
    else:
        with pytest.raises(HiddenMemberError):
            session.type_of("C::A")

    incomplete = "def f(v: C) -> int = case v of | C::B => 0"
    result = session.eval_entry(incomplete)
    assert isinstance(result.failure, NonExhaustiveMatchError)
    suggested = re.search(r"missing pattern: (.+)\.$", str(result.failure))
    assert suggested is not None

    completed = session.eval_entry(f"{incomplete} | {suggested[1]} => 1")
    assert completed.ok, completed.diagnostics
    assert session.eval_entry("f(s::E::A)").value == IntValue(1)
    assert session.eval_entry("f(s::E::B)").value == IntValue(0)


@pytest.mark.parametrize("cached", [False, True])
def test_standalone_alias_source_releases_its_factory(tmp_path: Path, cached: bool) -> None:
    graph = make_inline_graph_from_files(
        tmp_path,
        {
            "lib": "use s::* hiding E::A\n\nscope s\n  enum E = A | B\nend s\n\ntype C = E",
            "entry": "import lib\nuse lib::C::*",
        },
        default_stdlib=False,
    )
    enabled = gc.isenabled()
    gc.disable()
    try:
        resolved = resolve_program(graph)
        if cached:
            resolved = resolve_program(graph, cached_modules=resolved.modules)
        sources = resolved.sources(graph.entry_id)
        source = ref(sources)
        factory = ref(sources._site_sources)
        callback = sources.contributed_at
        del sources, resolved

        # A retained callback owns the factory even without its program.
        assert factory() is not None
        assert callback((), ("B",), LookupKind.CONSTRUCTOR).candidates
        assert not callback((), ("A",), LookupKind.CONSTRUCTOR).candidates
        del callback

        assert source() is None
        assert factory() is None
    finally:
        if enabled:
            gc.enable()
        gc.collect()


@pytest.mark.parametrize(
    ("target", "owner", "constructor"), [("s::E", "E", "A"), ("s::G[int]", "G", "V")]
)
def test_standalone_retained_source_selects_qualified_types_and_members(
    tmp_path: Path, target: str, owner: str, constructor: str
) -> None:
    graph = make_repl_graph_from_files(
        tmp_path,
        {
            "entry": (
                "scope s\n  enum E = A | B\n  enum G[T] = V(value: T)\nend s\n"
                f"type C = {target}\nenum F = s::E::A"
            )
        },
        default_stdlib=False,
    )
    resolved = resolve_program(
        graph, entry_repl_session_scope=ScopeNode(node_id=-1, parent=None, scope_path=())
    )
    previous = resolved.modules[graph.entry_id].resolved
    alias = next(decl for decl, _ in previous.type_declarations if isinstance(decl, TypeAlias))
    spelling = alias.type_expr
    assert isinstance(spelling, (NameT, AppliedT))
    enum = next(
        decl
        for decl, _ in previous.type_declarations
        if isinstance(decl, EnumDef) and decl.name == "F"
    )
    member = enum.members[0]
    assert isinstance(member, VariantRef)

    graph = make_repl_graph_from_files(tmp_path, {"entry": "use s::*"}, default_stdlib=False)
    resolved = resolve_program(
        graph,
        entry_repl_session_scope=previous.root_scope,
        entry_repl_session_scope_nodes=previous.scope_nodes,
        entry_repl_session_type_paths=previous.type_owners,
    )
    sources = resolved.sources(graph.entry_id)
    select = sources.type_name_selection_at
    selects_constructor = sources.selects_constructor
    del sources, resolved

    assert select((), spelling, every_use=False) is not None
    assert select((), member, every_use=True) is not None
    assert selects_constructor(
        ("C",),
        constructor,
        (graph.entry_id, ("s", owner), constructor),
        alias.span,
        (),
    )
