"""A bare name removed only by ``hiding`` is reported as hidden in entries and ``:info``."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.diagnostics import AglError, HiddenMemberError
from agnette.agl.repl.session import ReplSession

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
"""


@pytest.fixture
def session(tmp_path: Path) -> ReplSession:
    (tmp_path / "hid.agl").write_text(_LIB, encoding="utf-8")
    opened = repl_session_with_root(tmp_path)
    assert opened.eval_entry("import hid::* hiding Rec, Sc, Col::Red, g").ok
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
