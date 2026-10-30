"""Imported type names exclude functions without changing hiding or alias views."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.diagnostics import HiddenMemberError
from agnette.agl.repl import ReplSession
from agnette.agl.scope.program import resolve_program
from agnette.agl.scope.symbols import UnknownMemberError
from agnette.agl.semantics.values import IntValue
from tests.agl.ir_harness import make_inline_graph_from_files
from tests.agl.qualifier_support import inline_verdict

_TYPES = (
    "record Thing\n  value: int\n"
    "type Alias = Thing\n"
    "\n"
    "scope Nested\n  type Number = int\nend Nested\n"
    "\n"
    "def helper() -> int = 1\n"
)
_FUNCTIONS = "def Thing() -> int = 7\ndef Alias() -> int = 8\n"


@pytest.mark.parametrize(
    ("imports", "expected"),
    [
        ("import functions::*", set()),
        ("import types::*", {"Thing", "Alias"}),
        ("import functions::*\nimport types::*", {"Thing", "Alias"}),
        ("import types::*\nimport functions::*", {"Thing", "Alias"}),
        ("import types::* hiding Thing", set()),
        ("import types::* hiding Alias", set()),
        ("import types::* hiding Thing\nimport types::*", {"Thing", "Alias"}),
        ("import types::{Thing as Renamed, helper as Function}", {"Thing", "Renamed"}),
        ("scope Local\n  import types::*\nend Local", set()),
        ("import facade::*", set()),
    ],
)
def test_root_type_names_follow_declarations_and_import_hiding(
    tmp_path: Path, imports: str, expected: set[str]
) -> None:
    graph = make_inline_graph_from_files(
        tmp_path,
        {
            "types": _TYPES,
            "functions": _FUNCTIONS,
            "facade": "import types\nexport types hiding Thing\n",
            "entry": imports + "\n1",
        },
        default_stdlib=False,
    )
    resolved = resolve_program(graph)
    source = resolved.sources(graph.entry_id)
    assert source.tail_type_names() == expected
    # Repeated reads of the completed source must keep the same view.
    assert source.tail_type_names() == expected


@pytest.mark.parametrize("function_first", [False, True])
def test_imported_functions_do_not_replace_a_same_named_type(
    tmp_path: Path, function_first: bool
) -> None:
    imports = ["import types::*", "import functions::*"]
    if function_first:
        imports.reverse()
    verdict = inline_verdict(
        tmp_path,
        {
            "types": _TYPES,
            "functions": _FUNCTIONS,
            "entry": "\n".join(imports) + "\nlet item: Alias = types::Thing(value = 3)\nitem.value",
        },
        stdlib=False,
    )
    phase, error, _span, identity = verdict
    assert (phase, error, identity) == ("accepted", type(None), "int")


@pytest.mark.parametrize("item", ["Alias::Missing", "Alias::Missing::member"])
def test_unused_imports_without_type_tails_still_validate_hiding_items(
    tmp_path: Path, item: str
) -> None:
    phase, error, span, _identity = inline_verdict(
        tmp_path,
        {
            "types": _TYPES,
            "entry": f"import types hiding {item}\nimport types::helper\n1",
        },
        stdlib=False,
    )
    assert (phase, error) == ("scope", UnknownMemberError)
    assert span is not None


def test_scoped_unhiding_does_not_change_the_root_type_view(tmp_path: Path) -> None:
    phase, error, _span, identity = inline_verdict(
        tmp_path,
        {
            "types": _TYPES,
            "entry": (
                "import types::* hiding Thing\n"
                "\n"
                "scope Local\n"
                "  import types::*\n"
                "  let item: Alias = Thing(value = 4)\n"
                "end Local\n\nLocal::item.value"
            ),
        },
        stdlib=False,
    )
    assert (phase, error, identity) == ("accepted", type(None), "int")


def test_changed_dependencies_get_new_type_names_without_changing_old_sources(
    tmp_path: Path,
) -> None:
    def sources(library: str) -> frozenset[str]:
        graph = make_inline_graph_from_files(
            tmp_path,
            {"library": library, "entry": "import library::*\n1"},
            default_stdlib=False,
        )
        return resolve_program(graph).sources(graph.entry_id).tail_type_names()

    graph = make_inline_graph_from_files(
        tmp_path,
        {"library": "type Selected = int\n", "entry": "import library::*\n1"},
        default_stdlib=False,
    )
    original = resolve_program(graph).sources(graph.entry_id)
    assert original.tail_type_names() == {"Selected"}
    assert sources("def Selected() -> int = 2\n") == set()
    assert sources("type Replacement = int\n") == {"Replacement"}
    assert original.tail_type_names() == {"Selected"}


def test_retained_aliases_track_current_import_hiding_and_keep_value_identity(
    tmp_path: Path,
) -> None:
    (tmp_path / "types.agl").write_text(
        _TYPES + "enum Thing::Color\n  | Red\n  | Green\n", encoding="utf-8"
    )
    session = ReplSession(cwd=tmp_path)
    for entry in (
        "import types::*",
        "type Saved = Alias",
        "let old: Saved = Saved(value = 5)",
    ):
        result = session.eval_entry(entry)
        assert result.ok, result.diagnostics
    assert session.eval_entry("import types::* hiding Thing::Color::Red").ok
    assert isinstance(session.eval_entry("Saved::Color::Red").failure, HiddenMemberError)
    assert session.eval_entry("old.value").value == IntValue(5)
    assert session.eval_entry("import types::*").ok
    assert session.eval_entry("Saved::Color::Red").ok
    assert session.eval_entry("Saved(value = 6).value").value == IntValue(6)
    assert session.eval_entry("old.value").value == IntValue(5)
