"""Deeply nested interpolation is a diagnostic, never a host crash, on every REPL path."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.lexer import LexError, scan
from agnette.agl.repl.loop import has_runnable_statements, is_incomplete, run_repl_loop
from agnette.agl.repl.session import ReplSession

DEEP = 'let s = "' + '%{"' * 200 + "x" + '"}' * 200 + '"'


def test_strict_scan_reports_exhaustion_as_a_lex_error() -> None:
    with pytest.raises(LexError):
        list(scan(DEEP))


def test_incomplete_detection_and_runnable_check_survive() -> None:
    assert not is_incomplete(DEEP)
    assert has_runnable_statements(DEEP)


def test_entry_through_the_loop_reports_an_error() -> None:
    entries = iter([DEEP])
    written: list[str] = []

    def reader() -> str:
        try:
            return next(entries)
        except StopIteration:
            raise EOFError from None

    run_repl_loop(ReplSession(), reader=reader, writer=written.append)

    assert len(written) >= 2


def test_load_of_a_deep_file_fails_without_crashing(tmp_path: Path) -> None:
    path = tmp_path / "deep.agl"
    path.write_text(DEEP + "\n")
    session = ReplSession()
    session.open()

    results = session.load_file(path)

    assert results and not results[-1].ok
