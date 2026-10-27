"""Saved and reloaded REPL entries behave as typed, whatever their layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.repl.entry import EntryResult
from agnette.agl.repl.session import ReplSession
from agnette.agl.warning_policy import WarningPolicy

_Outcome = tuple[bool, list[tuple[str | None, int, int | None, int | None]]]


def _session(deny_tabs: bool) -> ReplSession:
    session = ReplSession(
        warning_policy=WarningPolicy({"tab-indentation": "deny"}) if deny_tabs else None
    )
    session.open()
    return session


def _outcome(result: EntryResult, first_line: int) -> _Outcome:
    """Whether *result* succeeded, and its advisories as (name, rows spanned, columns)."""
    return (
        result.ok,
        sorted(
            (
                (d.warning_name, (d.end_line or d.line) - d.line, d.column, d.end_column)
                for d in result.diagnostics + result.warnings
                if d.line >= first_line
            ),
            key=repr,
        ),
    )


def _typed_and_reloaded(
    tmp_path: Path, entries: list[str], deny_tabs: bool = False
) -> tuple[list[_Outcome], list[_Outcome]]:
    """Each entry's outcome typed at the prompt, and after a save and a load."""
    typed_session = _session(deny_tabs)
    typed = [_outcome(typed_session.eval_entry(entry), 0) for entry in entries]
    path = tmp_path / "t.agl"
    path.write_text(typed_session.dump_source())
    loaded = [_outcome(r, 1) for r in _session(deny_tabs).load_file(path)]
    return typed, loaded


@pytest.mark.parametrize("indent", ["  ", "\t"], ids=("spaces", "tab"))
def test_an_indented_entry_reloads_as_typed(tmp_path: Path, indent: str) -> None:
    entries = ["let a = 1", f"{indent}let b = 2", "b"]

    typed, loaded = _typed_and_reloaded(tmp_path, entries)

    assert all(ok for ok, _ in typed)
    assert [ok for ok, _ in loaded] == [True] * 3


def test_an_entry_allowed_a_tab_is_still_allowed_when_reloaded(tmp_path: Path) -> None:
    entry = '@allow("tab-indentation")\ndef w() -> int =\n\t1  # c'

    typed, loaded = _typed_and_reloaded(tmp_path, [entry], deny_tabs=True)

    assert typed == [(True, [])]
    assert loaded == typed


def test_a_mismatch_in_a_tab_indented_entry_spans_the_same_columns_when_loaded(
    tmp_path: Path,
) -> None:
    entry = 'def t() -> int =\n\t"x"'
    typed = _outcome(_session(False).eval_entry(entry), 0)
    path = tmp_path / "t.agl"
    path.write_text(f"# agm:repl-transcript:v1\n# agm:entry:{len(entry)}\n{entry}\n")

    (loaded,) = [_outcome(r, 1) for r in _session(False).load_file(path)]

    assert not typed[0]
    assert loaded == typed


@pytest.mark.parametrize(
    "entry",
    [
        "def v() -> int =\n  $\n    line one\n    line two\n",
        'def v(n: int) -> int =\n  "a{n}"\n',
        'let v: int = """\nabc\n"""\n',
        "scope S\n  def q() -> int =\n    1\nend S # tail\n\nlet v: int = true\n",
        'def v() -> int =\n  "x" # c\n',
    ],
    ids=("verbatim", "interpolation", "triple-quoted", "scope-end", "comment"),
)
def test_a_mismatch_in_an_entry_ending_in_a_newline_spans_the_same_when_loaded(
    tmp_path: Path, entry: str
) -> None:
    typed = _outcome(_session(False).eval_entry(entry), 0)
    path = tmp_path / "t.agl"
    path.write_text(f"# agm:repl-transcript:v1\n# agm:entry:{len(entry)}\n{entry}\n")

    (loaded,) = [_outcome(r, 1) for r in _session(False).load_file(path)]

    assert not typed[0]
    assert loaded == typed


@pytest.mark.parametrize(
    "entries",
    [
        ["def f() -> int =\n  1", "    f()"],
        ["def g() -> int =\n  if true =>\n    1\n  else =>\n    2", "      g()"],
        ["let a = 1\n", "  let b = 2", "b"],
        ["let v0 = 1 # t\n", "\t1\n  # deep\n"],
        ["let a = 1", "if true =>\n  let b = 2\n  ()\n", "    a"],
        ["# c\n", "\t\t1"],
    ],
    ids=("deeper-than-block", "deeper-than-nested-block", "newline", "comment", "block", "tabs"),
)
def test_an_entry_indented_unlike_the_previous_one_reloads_as_typed(
    tmp_path: Path, entries: list[str]
) -> None:
    typed, loaded = _typed_and_reloaded(tmp_path, entries)

    assert all(ok for ok, _ in typed)
    assert [ok for ok, _ in loaded] == [True] * len(entries)
