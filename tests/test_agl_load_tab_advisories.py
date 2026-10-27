"""A loaded file's TAB advisories are those of the file as a whole, each reported by one entry."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType

import pytest

from agnette.agl.lexer import lex_source
from agnette.agl.repl.entry import EntryResult
from agnette.agl.repl.session import ReplSession
from agnette.agl.warning_policy import WarningPolicy

_Advisory = tuple[int, int | None, int | None, int | None, str]

_SOURCES = {
    "entries-on-one-line": "let x = 1\nlet y = 2; let z = 3\t\n",
    "indented-entry": "# c\n\tlet a = 1\n\tlet b = 2\n",
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


@pytest.mark.parametrize(
    "later",
    [
        '@!doc("m")\nlet b = 1\t',
        "  let b = 2\t",
        "def t() -> int =\n\t1",
        "# \tc\n  let b = 2",
    ],
    ids=("late-attribute", "indented", "tab-block", "indented-after-comment"),
)
def test_a_halted_transcript_reports_the_later_advisories_though_it_is_no_program_as_a_whole(
    tmp_path: Path, later: str
) -> None:
    entries = [('let z: int = "x"', ""), (later, "")]
    path, results = _load(tmp_path, _transcript(*entries))

    assert [r.ok for r in results] == [False]
    assert len(_tab_diagnostics(results, denied=False)) == 1
    assert {label for label, _ in _tab_diagnostics(results, denied=False)} == {str(path)}


_METHODS_WITH_POSITIONAL_PARAMETER = {
    "plain": "record P\n  x: int\n\ndef P::m(self, @arg-pos y: int) -> int = y\n",
    "in-scope": "record P\n  x: int\n\nscope P\n  def m(self, @arg-pos y: int) -> int = y\nend P\n",
    "arg-named-owner": (
        "record P\n  x: int\n\n@arg-named\ndef P::m(self, @arg-pos y: int) -> int = y\n"
    ),
}


@pytest.mark.parametrize("name", sorted(_METHODS_WITH_POSITIONAL_PARAMETER))
def test_a_valid_method_does_not_withhold_the_advisories_after_a_failing_entry(
    tmp_path: Path, name: str
) -> None:
    source = _METHODS_WITH_POSITIONAL_PARAMETER[name] + 'let b: int = "x"\nlet c = 1\t\n'
    path, results = _load(tmp_path, source)

    assert [r.ok for r in results][-1] is False
    (advisory,) = (w for r in results for w in r.warnings if w.warning_name == "tab-indentation")
    assert advisory.source_label == str(path)
    assert advisory.line == source.count("\n")


_CONSTANT_ALLOW = "@allow(W)\ndef g() -> int =\n\t1\n"


@pytest.mark.parametrize(
    ("constant", "reported"), [("tab-indentation", False), ("unused-module-doc", True)]
)
def test_an_allow_of_an_unrun_item_may_name_a_constant_of_an_entry_run(
    tmp_path: Path, constant: str, reported: bool
) -> None:
    source = f'let W = "{constant}"\nlet a: int = "x"\n' + _CONSTANT_ALLOW
    path, results = _load(tmp_path, source)

    assert [r.ok for r in results] == [True, False]
    assert _tab_diagnostics(results, denied=False) == ([(str(path), 5)] if reported else [])


def test_an_unrun_item_the_conversion_rejects_withholds_the_later_advisories(
    tmp_path: Path,
) -> None:
    _, results = _load(tmp_path, 'let b: int = "x"\nuse a::{}\nlet c = 1\t\n')

    assert [r.ok for r in results] == [False]
    assert _tab_diagnostics(results, denied=False) == []


def test_an_unrun_entry_of_no_items_adds_no_advisories(tmp_path: Path) -> None:
    source = '# agm:repl-transcript:v1\n# agm:entry:16\nlet a: int = "x"\n# agm:entry:6\n# note\n'
    _, results = _load(tmp_path, source)

    assert [r.ok for r in results] == [False]
    assert _tab_diagnostics(results, denied=False) == []


def test_an_inner_attribute_after_an_item_rejects_the_file_unrun(tmp_path: Path) -> None:
    path = tmp_path / "t.agl"
    path.write_text('let a = 1\n@!doc("m")\nlet b = 2\n')
    session = ReplSession()
    session.open()

    results = session.load_file(path)

    assert [r.ok for r in results] == [False]
    assert (results[0].diagnostics[0].line, results[0].diagnostics[0].source_label) == (
        2,
        str(path),
    )
    assert not session.eval_entry("a").ok


def _transcript(*entries: tuple[str, str]) -> str:
    """A framed transcript of (text, allow marks) entries."""
    return "# agm:repl-transcript:v1\n" + "".join(
        f"# agm:entry:{len(text)}{' allow=' + marks if marks else ''}\n{text}\n"
        for text, marks in entries
    )


_FAILING = ('let b: int = "x"', "")
_TAB_ENTRY = "let c = 1\t"
_ALLOW = '@!allow("tab-indentation")\n'


@pytest.mark.parametrize("deny", [False, True], ids=("warn", "deny"))
def test_a_halted_transcript_reports_an_entry_advisory_despite_the_first_entrys_allow(
    tmp_path: Path, deny: bool
) -> None:
    entries = [(_ALLOW + "let a = 1", ""), _FAILING, (_TAB_ENTRY, "")]
    _, results = _load(tmp_path, _transcript(*entries), deny=deny)

    assert [r.ok for r in results] == [True, False]
    assert len(_tab_diagnostics(results, denied=deny)) == 1


@pytest.mark.parametrize("deny", [False, True], ids=("warn", "deny"))
def test_a_halted_transcript_silences_an_entry_advisory_its_own_mark_allows(
    tmp_path: Path, deny: bool
) -> None:
    entries = [("let a = 1", ""), _FAILING, (_TAB_ENTRY, "tab-indentation")]

    _, results = _load(tmp_path, _transcript(*entries), deny=deny)

    assert [r.ok for r in results] == [True, False]
    assert _tab_diagnostics(results, denied=deny) == []


@pytest.mark.parametrize("deny", [False, True], ids=("warn", "deny"))
def test_a_halted_transcript_silences_an_entry_advisory_its_own_attribute_allows(
    tmp_path: Path, deny: bool
) -> None:
    entries = [("let a = 1", ""), _FAILING, (_ALLOW + _TAB_ENTRY, "")]

    _, results = _load(tmp_path, _transcript(*entries), deny=deny)

    assert [r.ok for r in results] == [True, False]
    assert _tab_diagnostics(results, denied=deny) == []


def test_a_halted_transcript_silences_an_entry_advisory_its_own_item_allows(
    tmp_path: Path,
) -> None:
    allowed = '@allow("tab-indentation")\ndef g() -> int =\n\t1'
    entries = [("let a = 1", ""), _FAILING, (allowed, ""), ("def h() -> int =\n\t2", "")]

    path, results = _load(tmp_path, _transcript(*entries))

    assert _tab_diagnostics(results, denied=False) == [(str(path), 12)]


def test_a_halted_transcript_reports_the_advisory_of_an_unrun_entry_of_no_items(
    tmp_path: Path,
) -> None:
    entries = [("let a = 1", ""), _FAILING, ("# \tnote", "")]

    path, results = _load(tmp_path, _transcript(*entries))

    assert [r.ok for r in results] == [True, False]
    assert _tab_diagnostics(results, denied=False) == [(str(path), 7)]


def test_a_halted_transcript_withholds_only_the_advisories_of_an_entry_that_does_not_convert(
    tmp_path: Path,
) -> None:
    bad = '@allow("no-such-warning")\nlet b = 1\t'
    entries = [("let a = 1", ""), _FAILING, (bad, ""), (_TAB_ENTRY, "")]

    path, results = _load(tmp_path, _transcript(*entries))

    assert [r.ok for r in results] == [True, False]
    assert _tab_diagnostics(results, denied=False) == [(str(path), 10)]


def test_the_first_entry_of_a_file_that_does_not_convert_withholds_the_later_advisories(
    tmp_path: Path,
) -> None:
    _, results = _load(tmp_path, "use a::{}\nlet c = 1\t\n")

    assert [r.ok for r in results] == [False]
    assert _tab_diagnostics(results, denied=False) == []


type _Tracer = Callable[[FrameType, str, object], _Tracer | None]
_REPORTING_SOURCES = ("lexer/positions.py", "parser/parser.py", "repl/session.py")


def _reporting_work(tmp_path: Path, unrun: int) -> int:
    """The lines of position, parser and session code a halted transcript's load runs."""
    entries = [('let z: int = "x"', "")] + [
        (f"def f{i}() -> int =\n\t{i}", "") for i in range(unrun)
    ]
    path = tmp_path / f"t{unrun}.agl"
    path.write_text(_transcript(*entries))
    session = ReplSession()
    session.open()
    lines = 0

    def count_lines(frame: FrameType, event: str, arg: object) -> _Tracer | None:
        nonlocal lines
        if event == "line":
            lines += 1
        return count_lines

    def trace(frame: FrameType, event: str, arg: object) -> _Tracer | None:
        return count_lines if frame.f_code.co_filename.endswith(_REPORTING_SOURCES) else None

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        results = session.load_file(path)
    finally:
        sys.settrace(previous)
    assert len(_tab_diagnostics(results, denied=False)) == unrun
    return lines


def test_a_halted_transcript_reports_the_later_advisories_in_linear_work(tmp_path: Path) -> None:
    small, medium, large = (_reporting_work(tmp_path, n) for n in (40, 80, 160))

    # Linear work doubles with the transcript, up to a constant; a quadratic share grows the excess.
    assert medium - 2 * small < small // 50
    assert large - 2 * medium < small // 50
