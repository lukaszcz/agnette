"""A bare name removed only by ``hiding`` is reported as hidden in entries and ``:info``."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.diagnostics import AglError, HiddenMemberError
from agnette.agl.repl.session import ReplSession
from agnette.agl.scope.symbols import NoVisibleConstructorError

from ._agl_helpers import repl_session_with_root

_LIB = """\
record Rec
  a: int

scope Sc
  def f() -> int = 1
end Sc

enum Col =
  | Red
  | Blue

def g() -> int = 2

let gv = 3
"""


@pytest.fixture
def session(tmp_path: Path) -> ReplSession:
    (tmp_path / "hid.agl").write_text(_LIB, encoding="utf-8")
    opened = repl_session_with_root(tmp_path)
    assert opened.eval_entry("import hid::* hiding Rec, Sc, Col::Red, g, gv").ok
    return opened


@pytest.mark.parametrize("name", ["Rec", "Sc", "Red", "g"])
def test_a_bare_hidden_name_is_hidden_not_undefined(session: ReplSession, name: str) -> None:
    result = session.eval_entry(name)

    assert not result.ok
    assert "hidden" in result.diagnostics[0].message
    with pytest.raises(AglError) as info:
        session.info_of(name)
    assert isinstance(info.value, HiddenMemberError)
    assert str(info.value) == result.diagnostics[0].message


@pytest.mark.parametrize("entry", ["def h(x: Rec) -> int = 1", "let x: Rec = 1"])
def test_a_bare_hidden_type_annotation_is_hidden(session: ReplSession, entry: str) -> None:
    result = session.eval_entry(entry)

    assert "hidden" in result.diagnostics[0].message


def test_a_bare_unknown_name_stays_unknown(session: ReplSession) -> None:
    result = session.eval_entry("nowhere")

    assert not result.ok
    assert "hidden" not in result.diagnostics[0].message


@pytest.mark.parametrize(
    "entry",
    [
        "def h(c: Col) -> int =\n  case c of\n    | Red => 1\n    | _ => 2",
        "def h(r: Rec) -> int =\n  case r of\n    | Rec(a = 1) => 1\n    | _ => 2",
    ],
)
def test_a_bare_hidden_name_in_a_pattern_is_hidden(session: ReplSession, entry: str) -> None:
    result = session.eval_entry(entry)

    assert not result.ok
    assert isinstance(result.failure, HiddenMemberError)


def test_a_hidden_name_another_import_supplies_is_a_pattern(
    session: ReplSession, tmp_path: Path
) -> None:
    (tmp_path / "other.agl").write_text("enum Hue =\n  | Red\n  | Green\n", encoding="utf-8")
    assert session.eval_entry("import other::*").ok

    result = session.eval_entry("def h(c: Hue) -> int =\n  case c of\n    | Red => 1\n    | _ => 2")

    assert result.ok


def test_an_unknown_bare_pattern_name_stays_unknown(session: ReplSession) -> None:
    result = session.eval_entry(
        "def h(c: Col) -> int =\n  case c of\n    | Nowhere => 1\n    | _ => 2"
    )

    assert not result.ok
    assert not isinstance(result.failure, HiddenMemberError)


@pytest.mark.parametrize("name", ["g", "gv", "Sc"])
@pytest.mark.parametrize("form", ["{0}", "{0}(x)"])
def test_a_hidden_non_constructor_in_a_pattern_is_no_constructor(
    session: ReplSession, name: str, form: str
) -> None:
    pattern = form.format(name)

    result = session.eval_entry(
        f"def h(c: Col) -> int =\n  case c of\n    | {pattern} => 1\n    | _ => 2"
    )

    assert isinstance(result.failure, NoVisibleConstructorError)


def test_a_hidden_non_constructor_after_is_no_constructor(session: ReplSession) -> None:
    result = session.eval_entry("def h(c: Col) -> bool = c is g")

    assert isinstance(result.failure, NoVisibleConstructorError)


@pytest.mark.parametrize("name", ["g", "gv"])
@pytest.mark.parametrize("form", ["{0}", "{0}(x)"])
def test_a_hidden_qualified_non_constructor_in_a_pattern_is_rejected_like_a_visible_one(
    tmp_path: Path, name: str, form: str
) -> None:
    (tmp_path / "hid.agl").write_text(_LIB, encoding="utf-8")
    entry = (
        "def h(c: int) -> int =\n  case c of\n    | hid::"
        + form.format(name)
        + " => 1\n    | _ => 2"
    )
    failures = []
    for hiding in ("", f" hiding {name}"):
        session = repl_session_with_root(tmp_path)
        assert session.eval_entry(f"import hid{hiding}").ok
        failures.append(session.eval_entry(entry).failure)

    visible, hidden = failures
    assert visible is not None and hidden is not None
    assert not isinstance(hidden, HiddenMemberError)
    assert type(hidden) is type(visible)


@pytest.mark.parametrize(
    ("hiding", "pattern"),
    [("Rec", "hid::Rec(a)"), ("Col::Red", "hid::Col::Red")],
)
def test_a_hidden_qualified_constructor_in_a_pattern_is_hidden(
    tmp_path: Path, hiding: str, pattern: str
) -> None:
    (tmp_path / "hid.agl").write_text(_LIB, encoding="utf-8")
    session = repl_session_with_root(tmp_path)
    assert session.eval_entry(f"import hid hiding {hiding}").ok

    result = session.eval_entry(
        f"def h(c: int) -> int =\n  case c of\n    | {pattern} => 1\n    | _ => 2"
    )

    assert isinstance(result.failure, HiddenMemberError)
