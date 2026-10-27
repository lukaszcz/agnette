"""A plain file indented throughout loads, saves and loads again as the same entries."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.repl.entry import EntryResult
from agnette.agl.repl.session import ReplSession
from agnette.agl.warning_policy import WarningPolicy

TAB = "tab-indentation"

_FILES = {
    "scope": "    scope S\n        def g() -> int = 1\n    end S\n    let x = S::g()\n",
    "loop": (
        "    def f() -> int =\n        var i = 0\n        while i < 3 do\n"
        "            i := i + 1\n        done\n        i\n    let y = f()\n"
    ),
    "branches": (
        "    def h(n: int) -> int =\n        if n > 0 =>\n            1\n"
        "        else =>\n            2\n    let y = h(1)\n"
    ),
    "record": "  record P\n    x: int\n  let p = P(1)\n",
    "same-line": "    let a = 1; let b = a + 1\n    let c = b\n",
    "same-line-block": "    let a = 1; def f() -> int =\n        a\n    let c = f()\n",
    "after-bracket-line": "    let a = (1 +\n  2); let b = a\n    let c = b\n",
    "tabs": "\t\tlet a = 1; let\tb = 2\n\t\tlet c = 3\n",
    "tab-block": "\tdef f() -> int =\n\t\t1\n\tlet b = f()\n",
    "comments": "    # note\n    let a = 1 # one\n\n    # alone\n    let b = a\n",
}


def _load(
    path: Path, *, policy: WarningPolicy | None = None
) -> tuple[ReplSession, list[EntryResult]]:
    session = ReplSession(warning_policy=policy)
    session.open()
    return session, session.load_file(path)


def _tab_points(path: Path, results: list[EntryResult]) -> list[str]:
    """The characters of *path* the TAB advisories of *results* point at."""
    lines = path.read_text().split("\n")
    return [
        lines[w.line - 1][(w.column or 0) - 1]
        for r in results
        for w in r.warnings
        if w.warning_name == TAB
    ]


@pytest.mark.parametrize("name", sorted(_FILES))
def test_a_loaded_file_saved_and_loaded_again_gives_the_same_entries(
    tmp_path: Path, name: str
) -> None:
    plain = tmp_path / "p.agl"
    plain.write_text(_FILES[name])
    session, results = _load(plain)
    assert all(r.ok for r in results)
    saved = tmp_path / "t.agl"
    saved.write_text(session.dump_source())

    _, again = _load(saved)

    assert [(r.ok, r.value) for r in again] == [(r.ok, r.value) for r in results]
    plain_tabs, again_tabs = _tab_points(plain, results), _tab_points(saved, again)
    assert again_tabs == plain_tabs
    assert set(plain_tabs) <= {"\t"}


def test_a_tab_before_an_allowing_item_is_governed_by_that_item(tmp_path: Path) -> None:
    path = tmp_path / "p.agl"
    path.write_text('\t\tlet pre = 1\n\t\t@allow("tab-indentation") let b = 1\n')

    _, results = _load(path)

    assert [(w.line, w.column) for r in results for w in r.warnings if w.warning_name == TAB] == [
        (1, 1),
        (1, 2),
    ]
