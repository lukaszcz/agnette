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


def _load(
    tmp_path: Path, source: str, *, deny: bool = False, name: str = "t.agl"
) -> tuple[Path, list[EntryResult]]:
    path = tmp_path / name
    path.write_text(source)
    policy = WarningPolicy({"tab-indentation": "deny"}) if deny else WarningPolicy()
    session = ReplSession(warning_policy=policy)
    session.open()
    return path, session.load_file(path)


def _tab_diagnostics(results: list[EntryResult], *, denied: bool) -> list[tuple[str | None, int]]:
    return [
        (d.source_label, d.line)
        for r in results
        for d in (r.diagnostics if denied else r.warnings)
        if d.warning_name == "tab-indentation"
    ]


_LATER_TAB = 'let a: int = "x"\ndef g() -> int =\n\t1\n'
_COMMENT_ONLY = "# only comment\n\t# tabbed comment\n"


@pytest.mark.parametrize("deny", [False, True], ids=("warn", "deny"))
@pytest.mark.parametrize("source", [_LATER_TAB, _COMMENT_ONLY], ids=("after-failure", "no-items"))
def test_advisories_of_unrun_entries_carry_the_files_label(
    tmp_path: Path, source: str, deny: bool
) -> None:
    path, results = _load(tmp_path, source, deny=deny)

    assert _tab_diagnostics(results, denied=deny) == [(str(path), 3 if source == _LATER_TAB else 2)]


def test_a_leading_file_allow_silences_the_advisories_after_a_failing_entry(
    tmp_path: Path,
) -> None:
    _, results = _load(tmp_path, '@!allow("tab-indentation")\n' + _LATER_TAB, deny=True)

    assert [r.ok for r in results] == [False]
    assert _tab_diagnostics(results, denied=True) == []
    assert _tab_diagnostics(results, denied=False) == []


_ALLOW_AFTER_FAILURE = {
    "declaration": 'let a: int = "x"\n@allow("tab-indentation")\ndef g() -> int =\n\t1\n',
    "scope-region": (
        'let a: int = "x"\n\nscope S\n  @!allow("tab-indentation")\n'
        "  def g() -> int =\n\t\t1\nend S\n"
    ),
}


@pytest.mark.parametrize("deny", [False, True], ids=("warn", "deny"))
@pytest.mark.parametrize("name", sorted(_ALLOW_AFTER_FAILURE))
def test_a_failing_entry_does_not_unsilence_the_allow_regions_of_later_entries(
    tmp_path: Path, name: str, deny: bool
) -> None:
    _, results = _load(tmp_path, _ALLOW_AFTER_FAILURE[name], deny=deny)

    assert [r.ok for r in results] == [False]
    assert _tab_diagnostics(results, denied=deny) == []


def test_a_later_advisory_outside_an_allow_region_is_still_reported(tmp_path: Path) -> None:
    source = (
        'let a: int = "x"\n@allow("tab-indentation")\ndef g() -> int =\n\t1\n'
        "def h() -> int =\n\t2\n"
    )
    path, results = _load(tmp_path, source)

    assert _tab_diagnostics(results, denied=False) == [(str(path), 6)]


def test_a_scope_failure_withholds_the_advisories_of_later_entries(tmp_path: Path) -> None:
    _, results = _load(tmp_path, "let a = nope\nlet b = 1\t\n")

    assert [r.ok for r in results] == [False]
    assert _tab_diagnostics(results, denied=False) == []


def test_an_invalid_attribute_in_a_later_entry_withholds_the_later_advisories(
    tmp_path: Path,
) -> None:
    source = 'let a: int = "x"\n@allow("no-such-warning")\nlet b = 1\nlet c = 2\t\n'
    _, results = _load(tmp_path, source)

    assert [r.ok for r in results] == [False]
    assert _tab_diagnostics(results, denied=False) == []


def test_a_transcript_halting_at_a_failing_entry_reports_the_later_advisories(
    tmp_path: Path,
) -> None:
    source = (
        '# agm:repl-transcript:v1\n# agm:entry:16\nlet a: int = "x"\n# agm:entry:10\nlet b = 1\t\n'
    )
    path, results = _load(tmp_path, source)

    assert [r.ok for r in results] == [False]
    (advisory,) = (w for r in results for w in r.warnings if w.warning_name == "tab-indentation")
    assert (advisory.source_label, advisory.line, advisory.column) == (str(path), 5, 10)


def test_a_transcript_that_is_no_program_as_a_whole_withholds_the_later_advisories(
    tmp_path: Path,
) -> None:
    source = (
        '# agm:repl-transcript:v1\n# agm:entry:16\nlet a: int = "x"\n'
        '# agm:entry:21\n@!doc("m")\nlet b = 1\t\n'
    )
    _, results = _load(tmp_path, source)

    assert [r.ok for r in results] == [False]
    assert _tab_diagnostics(results, denied=False) == []
