"""A loaded file's TAB advisories are those of the file as a whole, each reported by one entry."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.lexer import lex_source
from agnette.agl.repl.entry import EntryResult
from agnette.agl.repl.session import ReplSession
from agnette.agl.warning_policy import WarningPolicy

_Advisory = tuple[int, int | None, int | None, int | None, str]

_SOURCES = {
    "entries-on-one-line": "let x = 1\nlet y = 2; let z = 3\t\n",
    "indented-entry": "# c\n\tlet a = 1\nlet b = 2\n",
    "between-entries": "let x = 1\n\t\nlet b = 2\t# x\t\n# \tc\nlet c = 3\n",
    "space-indented-entry": "# c\n  let a =\t1\n",
    "after-last-entry": "let a = 1\n# \tz\n\t\n",
    "before-first-entry": "# \tz\nlet a = 1\n\t# c\nlet b = 2\n",
    "attributes-only": '@!doc("m")\n# \tc\n',
    "comment-only": "# \tc\n",
    "indented-comment-and-blank-line": "\t# c\n\t\n",
    "inside-an-entry": "let a = 1\ndef f() -> int =\n\ta\n\n\t# \tc\nlet b = f()\t+ 1\n",
}


@pytest.mark.parametrize("name", sorted(_SOURCES))
def test_a_loaded_files_tab_advisories_are_its_whole_file_advisories(
    tmp_path: Path, name: str
) -> None:
    source = _SOURCES[name]
    path = tmp_path / "t.agl"
    path.write_text(source)
    whole = lex_source(source).tab_warnings
    session = ReplSession()
    session.open()

    results = session.load_file(path)

    assert all(r.ok for r in results)
    loaded: list[_Advisory] = sorted(
        (w.line, w.column, w.end_line, w.end_column, w.message)
        for r in results
        for w in r.warnings
        if w.warning_name == "tab-indentation"
    )
    assert whole
    assert loaded == sorted((w.line, w.column, w.end_line, w.end_column, w.message) for w in whole)


def _tab_advisories(results: list[EntryResult]) -> list[_Advisory]:
    return sorted(
        (w.line, w.column, w.end_line, w.end_column, w.message)
        for r in results
        for w in r.warnings
        if w.warning_name == "tab-indentation"
    )


def test_a_load_halting_at_a_failing_entry_still_reports_the_later_advisories(
    tmp_path: Path,
) -> None:
    source = 'let a = 1\nlet b: int = "x"\nlet c = 2\t\nlet d = 3\t\n'
    path = tmp_path / "t.agl"
    path.write_text(source)
    session = ReplSession()
    session.open()

    results = session.load_file(path)

    assert [r.ok for r in results] == [True, False]
    whole = lex_source(source).tab_warnings
    assert len(whole) == 2
    assert _tab_advisories(results) == sorted(
        (w.line, w.column, w.end_line, w.end_column, w.message) for w in whole
    )


@pytest.mark.parametrize("source", ["# \tc\n", 'let a = 1\nlet b: int = "x"\nlet c = 2\t\n'])
def test_a_denied_advisory_fails_the_load_whether_or_not_an_entry_ran(
    tmp_path: Path, source: str
) -> None:
    path = tmp_path / "t.agl"
    path.write_text(source)
    session = ReplSession(warning_policy=WarningPolicy({"tab-indentation": "deny"}))
    session.open()

    results = session.load_file(path)

    assert not results[-1].ok
    assert any(d.warning_name == "tab-indentation" for r in results for d in r.diagnostics)
