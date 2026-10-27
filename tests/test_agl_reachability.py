"""Static reference summaries and the reachability walk over them."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.modules.ids import ModuleId
from agnette.agl.scope.program import resolve_program
from agnette.agl.typecheck.program import CheckedProgram, check_program
from agnette.agl.typecheck.reachability import reachable_from
from agnette.agl.typecheck.references import References
from tests.agl.ir_harness import make_file_graph_from_files
from tests.agl.module_graph import build_inline_entry_graph

_LIB = ModuleId.from_path("lib")


def _checked(
    tmp_path: Path, entry: str, lib: str = "", *, default_stdlib: bool = False
) -> CheckedProgram:
    modules = {"entry": entry, "lib": lib}
    graph = make_file_graph_from_files(tmp_path, modules, default_stdlib=default_stdlib)
    return check_program(resolve_program(graph))


def _spell(checked: CheckedProgram, key: tuple[ModuleId, tuple[str, ...], str]) -> str:
    module, scope_path, name = key
    owner = "entry" if module == checked.entry_id else module.path_str()
    return "::".join((owner, *scope_path, name))


def _flat(checked: CheckedProgram, refs: References) -> set[str]:
    targets = refs.functions | refs.params | refs.bindings
    return {_spell(checked, target) for target in targets}


def _entry_edges(checked: CheckedProgram, name: str) -> set[str]:
    summary = checked.modules[checked.entry_id].declaration_references
    return _flat(checked, summary[(checked.entry_id, (), name)])


@pytest.mark.parametrize(
    ("entry", "lib", "expected"),
    [
        pytest.param(
            "import lib::*\nprogram def main() -> unit =\n  let _ = leaf()\n",
            "def leaf() -> int = 1\n",
            {"lib::leaf"},
            id="direct-call",
        ),
        pytest.param(
            "import lib\nprogram def main() -> unit =\n  let _ = lib::leaf()\n",
            "def leaf() -> int = 1\n",
            {"lib::leaf"},
            id="qualified-call",
        ),
        pytest.param(
            "import lib::{leaf as other}\nprogram def main() -> unit =\n  let _ = other()\n",
            "def leaf() -> int = 1\n",
            {"lib::leaf"},
            id="import-alias",
        ),
        pytest.param(
            "import lib::*\nprogram def main() -> unit =\n  let _ = Region::inner()\n",
            "scope Region\n  def inner() -> int = 1\nend Region\n",
            {"lib::Region::inner"},
            id="scoped-function",
        ),
        pytest.param(
            "import lib::*\nprogram def main() -> unit =\n  let _ = 1 <+> 2\n",
            "infixl <+> at 6\ndef <+>(x: int, y: int) -> int = x\n",
            {"lib::<+>"},
            id="user-infix-operator",
        ),
        pytest.param(
            "import lib::*\nprogram def main() -> unit =\n  let _ = Point(x = 1).sum()\n",
            "record Point\n  x: int\n\ndef Point::sum(self) -> int = self.x\n",
            {"lib::Point::sum"},
            id="method-call",
        ),
        pytest.param(
            "import lib::*\nprogram def main() -> unit =\n"
            "  let f = Point(x = 1).sum\n  let _ = f()\n",
            "record Point\n  x: int\n\ndef Point::sum(self) -> int = self.x\n",
            {"lib::Point::sum"},
            id="method-reference-without-call",
        ),
        pytest.param(
            "import lib::*\nprogram def main() -> unit =\n  let _ = apply(double, 3)\n",
            "def double(x: int) -> int = x * 2\ndef apply(f: (int) -> int, x: int) -> int = f(x)\n",
            {"lib::apply", "lib::double"},
            id="function-value-argument",
        ),
        pytest.param(
            "@param let limit: int = 3\nprogram def main() -> unit =\n  let _ = limit\n",
            "",
            {"entry::limit"},
            id="param-read-same-module",
        ),
        pytest.param(
            "import lib::*\nprogram def main() -> unit =\n  let _ = limit\n",
            "@param let limit: int = 3\n",
            {"lib::limit"},
            id="param-read-imported",
        ),
        pytest.param(
            "import lib::*\nprogram def main() -> unit =\n  let _ = plain\n",
            "let plain: int = 3\n",
            {"lib::plain"},
            id="plain-binding-read",
        ),
        pytest.param(
            "import lib::*\nprogram def main() -> unit =\n  let _ = 1\n",
            "def leaf() -> int = 1\n",
            set(),
            id="unreferenced-import",
        ),
        pytest.param(
            "program def main() -> unit =\n  let x = 1\n  let _ = x\n",
            "",
            set(),
            id="local-binding-read",
        ),
        pytest.param(
            "import lib::*\nprogram def main() -> unit =\n  let x = 1\n  let _ = x\n",
            "let x: int = 3\n",
            set(),
            id="local-binding-shadowing-an-imported-one",
        ),
        pytest.param(
            "program def main(x: int) -> unit =\n  let _ = x\n",
            "",
            set(),
            id="function-parameter-read",
        ),
        pytest.param(
            "def helper(@param n: int = 1) -> int = n\n"
            "program def main() -> unit =\n  let _ = helper()\n",
            "",
            {"entry::helper"},
            id="function-param-parameter-read-adds-no-param-edge",
        ),
        pytest.param(
            "program def main(@param n: int = 1) -> unit =\n  let _ = n\n",
            "",
            set(),
            id="program-param-parameter-read",
        ),
    ],
)
def test_main_summary_lists_the_references_of_its_body(
    tmp_path: Path, entry: str, lib: str, expected: set[str]
) -> None:
    assert _entry_edges(_checked(tmp_path, entry, lib), "main") == expected


@pytest.mark.parametrize(
    "default",
    ["leaf()", "(fn() => leaf())()"],
    ids=["plain-default", "lambda-in-default"],
)
def test_summary_includes_references_of_parameter_defaults(tmp_path: Path, default: str) -> None:
    checked = _checked(
        tmp_path,
        f"import lib::*\ndef pick(x: int = {default}) -> int = x\n"
        "program def main() -> unit =\n  let _ = 1\n",
        "def leaf() -> int = 1\n",
    )
    assert _entry_edges(checked, "pick") == {"lib::leaf"}
    assert _entry_edges(checked, "main") == set()


def test_summary_includes_references_made_inside_lambdas(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "import lib::*\nprogram def main() -> unit =\n  let _ = (fn() => leaf())()\n",
        "def leaf() -> int = 1\n",
    )
    assert _entry_edges(checked, "main") == {"lib::leaf"}


def test_summary_keeps_edges_to_builtins_and_summarizes_them(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "program def main() -> unit = print(1)\n",
        default_stdlib=True,
    )
    assert _entry_edges(checked, "main") == {"std/io::print"}
    io = ModuleId.from_path("std/io")
    assert checked.modules[io].declaration_references[(io, (), "print")] == References()


def _reached(checked: CheckedProgram, root: str) -> tuple[set[str], set[str]]:
    reachable = reachable_from(checked, (checked.entry_id, (), root))
    return (
        {_spell(checked, key) for key in reachable.functions},
        {_spell(checked, key) for key in reachable.params},
    )


def test_reachability_follows_transitive_chains_and_collects_params(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "import lib::*\nprogram def main() -> unit =\n  let _ = a() + d()\n",
        "def c() -> int = 1\n"
        "def b() -> int = c()\n"
        "def a() -> int = b()\n"
        "@param let p: int = 1\n"
        "def d() -> int = p\n"
        "@param let q: int = 2\n"
        "def lonely() -> int = q\n",
    )
    functions, params = _reached(checked, "main")
    assert functions == {"entry::main", "lib::a", "lib::b", "lib::c", "lib::d"}
    assert params == {"lib::p"}


def test_reachability_terminates_on_recursion(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "program def main() -> unit =\n  let _ = even(4)\n"
        "def even(n: int) -> bool = if n == 0 => true else => odd(n - 1)\n"
        "def odd(n: int) -> bool = if n == 0 => false else => even(n - 1)\n",
    )
    functions, params = _reached(checked, "main")
    assert functions == {"entry::main", "entry::even", "entry::odd"}
    assert params == set()


def test_an_imported_but_unreferenced_module_contributes_nothing(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "import lib::*\nprogram def main() -> unit =\n  let _ = 1\n",
        "@param let p: int = 1\ndef used() -> int = p\n",
    )
    assert _reached(checked, "main") == ({"entry::main"}, set())


def test_a_parameter_default_reference_makes_its_target_reachable(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "import lib::*\n"
        "def pick(x: int = leaf()) -> int = x\n"
        "program def main() -> unit =\n  let _ = pick()\n",
        "@param let p: int = 1\ndef leaf() -> int = p\n",
    )
    functions, params = _reached(checked, "main")
    assert functions == {"entry::main", "entry::pick", "lib::leaf"}
    assert params == {"lib::p"}


def test_a_pattern_binder_is_not_a_reference(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "enum Choice\n  | pick(n: int)\n\n"
        "def get(c: Choice) -> int =\n  case c of\n  | pick(n) => n\n"
        "program def main() -> unit =\n  let _ = get(pick(1))\n",
    )
    assert _entry_edges(checked, "get") == set()


def test_edges_are_tagged_by_kind(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "import lib::*\nprogram def main() -> unit =\n  let _ = leaf() + p + plain\n",
        "def leaf() -> int = 1\n@param let p: int = 1\nlet plain: int = 3\n",
    )
    refs = checked.modules[checked.entry_id].declaration_references[(checked.entry_id, (), "main")]
    assert refs == References(
        functions=frozenset({(_LIB, (), "leaf")}),
        params=frozenset({(_LIB, (), "p")}),
        bindings=frozenset({(_LIB, (), "plain")}),
    )


def _binding_edges(checked: CheckedProgram, module: ModuleId, name: str) -> set[str]:
    summary = checked.modules[module].binding_references
    return _flat(checked, summary[(module, (), name)])


def test_static_binding_summaries_cover_their_initializers(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "import lib::*\nprogram def main() -> unit = print(1)\n",
        "@param let a: int = 1\n@param var b: int = a\nlet c: int = b\nvar d: int = 5\n",
        default_stdlib=True,
    )
    assert _binding_edges(checked, _LIB, "a") == set()
    assert _binding_edges(checked, _LIB, "b") == {"lib::a"}
    assert _binding_edges(checked, _LIB, "c") == {"lib::b"}
    assert _binding_edges(checked, _LIB, "d") == set()


def test_scope_region_binding_summary_is_keyed_by_scope_path(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "import lib::*\nprogram def main() -> unit =\n  let _ = Region::v\n",
        "@param let p: int = 1\nscope Region\n  let v: int = p\nend Region\n",
    )
    assert _entry_edges(checked, "main") == {"lib::Region::v"}
    assert _flat(checked, checked.modules[_LIB].binding_references[(_LIB, ("Region",), "v")]) == {
        "lib::p"
    }
    functions, params = _reached(checked, "main")
    assert params == {"lib::p"}


def test_a_param_read_by_a_static_initializer_is_reachable(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "import lib::*\nprogram def main() -> unit =\n  print(q)\n",
        "@param let p: int = 1\nlet q: int = p\n",
        default_stdlib=True,
    )
    assert _reached(checked, "main") == ({"entry::main", "std/io::print"}, {"lib::p"})


def test_a_reached_param_initializer_contributes_its_own_reads(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "import lib::*\nprogram def main() -> unit =\n  print(b)\n",
        "@param let a: int = 1\n@param let b: int = a\n@param let unrelated: int = 2\n",
        default_stdlib=True,
    )
    assert _reached(checked, "main") == ({"entry::main", "std/io::print"}, {"lib::a", "lib::b"})


def test_static_binding_chains_are_followed(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        "import lib::*\nprogram def main() -> unit =\n  print(r)\n",
        "@param let p: int = 1\nlet q: int = p\nlet r: int = q\n",
        default_stdlib=True,
    )
    assert _reached(checked, "main") == ({"entry::main", "std/io::print"}, {"lib::p"})


def test_inline_source_root_bindings_feed_the_synthetic_entry(tmp_path: Path) -> None:
    graph, _ = build_inline_entry_graph(
        "@param let a: int = 1\nlet b: int = a\nprint(b)\n", default_stdlib=True
    )
    checked = check_program(resolve_program(graph))
    entry = checked.modules[checked.entry_id]
    (main,) = (key for key in entry.declaration_references if key[2] == "main")
    reachable = reachable_from(checked, main)
    assert reachable.params == {(checked.entry_id, (), "a")}


_SLOT_PROGRAM = (
    "enum Flag\n  | on\n\nenum Packet\n  | packet(flag: Flag)\n\n{outside}\n"
    "program def main() -> unit =\n  let item = packet(Flag::on)\n"
    "  case item of\n  | packet(on) =>\n    case item of\n    | packet(on) => print({read})\n"
)


def test_a_pattern_slot_reference_to_an_enclosing_param_is_an_edge(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        _SLOT_PROGRAM.format(outside="@param let on: int = 4\n", read="on"),
        default_stdlib=True,
    )
    assert _reached(checked, "main") == ({"entry::main", "std/io::print"}, {"entry::on"})


def test_a_pattern_slot_reference_to_an_enclosing_function_is_an_edge(tmp_path: Path) -> None:
    checked = _checked(
        tmp_path,
        _SLOT_PROGRAM.format(outside="def on() -> int = 4\n", read="on()"),
        default_stdlib=True,
    )
    assert _reached(checked, "main") == ({"entry::main", "entry::on", "std/io::print"}, set())
