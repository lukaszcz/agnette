"""Unrestricted import routes coexist with hiding without freezing later scope reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.diagnostics import HiddenMemberError
from agnette.agl.repl import ReplSession
from agnette.agl.scope.symbols import UnknownMemberError
from agnette.agl.semantics.values import IntValue
from tests.agl.qualifier_support import inline_verdict

_LIB = (
    "record Base\n  x: int\n"
    "def Base::m(self) -> int = self.x\n"
    "enum Base::Color\n  | Red\n  | Green\n"
    "\n"
    "scope Base::Sub\n  def f() -> int = 7\nend Base::Sub\n"
    "\n"
    "type Alias = Base\n"
)


@pytest.mark.parametrize("unhidden_first", [False, True])
@pytest.mark.parametrize(
    ("hidden", "probe", "identity"),
    [
        ("Alias", "Alias(x = 3).m()", "int"),
        ("Alias::m", "Alias::m(Base(x = 3))", "int"),
        ("Alias::Sub::f", "Alias::Sub::f()", "int"),
        (
            "Alias::Color::Red",
            "let c: Base::Color = Base::Color::Red\nc is Alias::Color::Red",
            "bool",
        ),
    ],
)
def test_one_unrestricted_route_keeps_the_same_declaration(
    tmp_path: Path, unhidden_first: bool, hidden: str, probe: str, identity: str
) -> None:
    imports = [f"import lib::* hiding {hidden}", "import lib::*"]
    if unhidden_first:
        imports.reverse()
    phase, error, _span, selected = inline_verdict(
        tmp_path, {"lib": _LIB, "entry": "\n".join((*imports, probe))}
    )
    assert (phase, error, selected) == ("accepted", type(None), identity)


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("spelling", ["Alias::m(Base(x = 3))", "Alias::Sub::f()"])
def test_export_withholding_still_filters_alias_tails(
    tmp_path: Path, restore: bool, spelling: str
) -> None:
    imports = "import facade::{Alias, Base}\n"
    if restore:
        imports += "import lib::*\n"
    phase, error, _span, selected = inline_verdict(
        tmp_path,
        {
            "lib": _LIB,
            "facade": "import lib\nexport lib hiding Alias::m, Alias::Sub::f\n",
            "entry": imports + spelling,
        },
    )
    expected = ("accepted", type(None), "int") if restore else ("scope", HiddenMemberError, None)
    assert (phase, error, selected) == expected


@pytest.mark.parametrize("item", ["Alias::Nope", "Alias::Sub::Nope"])
@pytest.mark.parametrize("unhidden_first", [False, True])
def test_an_unrestricted_route_does_not_excuse_an_invalid_hiding_item(
    tmp_path: Path, item: str, unhidden_first: bool
) -> None:
    imports = [f"import lib::* hiding {item}", "import lib::*"]
    if unhidden_first:
        imports.reverse()
    phase, error, span, _selected = inline_verdict(
        tmp_path, {"lib": _LIB, "entry": "\n".join((*imports, "1"))}
    )
    assert (phase, error) == ("scope", UnknownMemberError)
    assert span is not None


def test_unhidden_region_route_does_not_escape_its_region(tmp_path: Path) -> None:
    header = (
        "import lib::* hiding Alias::Sub::f\n"
        "\n"
        "scope Local\n  import lib::*\n  let a = Alias::Sub::f()\nend Local\n"
    )
    for suffix, expected in (
        ("Local::a", ("accepted", type(None), "int")),
        ("Alias::Sub::f()", ("scope", HiddenMemberError, None)),
    ):
        phase, error, _span, selected = inline_verdict(
            tmp_path, {"lib": _LIB, "entry": header + suffix}
        )
        assert (phase, error, selected) == expected


def test_repl_alias_visibility_follows_current_import_hiding(tmp_path: Path) -> None:
    (tmp_path / "lib.agl").write_text(_LIB, encoding="utf-8")
    session = ReplSession(cwd=tmp_path)
    for source in ("import lib::* hiding Base::Color::Red", "type C = Base::Color"):
        result = session.eval_entry(source)
        assert result.ok, result.diagnostics
    assert isinstance(session.eval_entry("C::Red").failure, HiddenMemberError)
    assert session.eval_entry("import lib::*").ok
    assert session.eval_entry(
        "let c: C = C::Red\ncase c of | C::Red => 1 | _ => 2"
    ).value == IntValue(1)
    assert session.eval_entry("import lib::* hiding Base::Color::Red").ok
    assert isinstance(session.eval_entry("C::Red").failure, HiddenMemberError)
    assert session.eval_entry(
        "let c: C = C::Green\ncase c of | C::Green => 2 | _ => 1"
    ).value == IntValue(2)


@pytest.mark.parametrize("mixed", [False, True])
def test_unhidden_scope_reads_notice_a_later_own_binding(tmp_path: Path, mixed: bool) -> None:
    (tmp_path / "lib.agl").write_text("def g() -> int = 1\n", encoding="utf-8")
    imports = "import lib\n"
    if mixed:
        imports += "import lib hiding g\n"
    session = ReplSession(cwd=tmp_path)
    result = session.eval_entry(
        imports + "use lib::*\n\nscope S\n  use T::*\n  let a = g()\n"
        "\n"
        "  scope T\n    let g = 7\n  end T\n\n  let b = g\nend S\n\nS::a + S::b"
    )
    assert result.ok, result.diagnostics
    assert result.value == IntValue(8)
