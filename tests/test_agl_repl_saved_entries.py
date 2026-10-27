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
    entry = '@allow("tab-indentation")\ndef w() -> int =\n\t1\t# c'

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
