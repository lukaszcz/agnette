"""A REPL entry's source is lexed once, whichever readings (program, bare type) it is given."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from lark.lexer import Token

from agnette.agl.lexer import lex_source
from agnette.agl.lexer.scanner import _Scanner
from agnette.agl.repl.loop import has_runnable_statements, is_incomplete
from agnette.agl.repl.plain_console import run_plain_console
from agnette.agl.repl.session import ReplSession
from tests.test_agl_repl_console import drive


def _session() -> ReplSession:
    session = ReplSession()
    session.open()
    return session


class _Scans:
    """Counts the strict scanner passes made while patched in, wherever they start."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real = _Scanner.scan

        def counting(scanner: _Scanner) -> Iterator[Token]:
            # Editor highlighting scans in recovery mode as its own, display-only pass.
            if not getattr(scanner, "_recover", False):
                self.count += 1
            return real(scanner)

        monkeypatch.setattr(_Scanner, "scan", counting)


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> _Scans:
    return _Scans(monkeypatch)


@pytest.mark.parametrize("text", ["int", "int -> int", "dict[text, int]", "Option"])
def test_a_bare_type_entry_is_lexed_once(text: str, scans: _Scans) -> None:
    session = _session()
    scans.count = 0

    result = session.eval_entry(text)

    assert result.kind == "type"
    assert scans.count == 1


@pytest.mark.parametrize("text", ["1 +", "nope", "1 / 0"])
def test_a_rejected_entry_is_lexed_once(text: str, scans: _Scans) -> None:
    session = _session()
    scans.count = 0

    assert not session.eval_entry(text).ok
    assert scans.count == 1


def test_a_file_that_is_no_program_is_lexed_once(tmp_path: Path, scans: _Scans) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text("int -> int\n")
    scans.count = 0

    results = session.load_file(path)

    assert [r.kind for r in results] == ["type"]
    assert scans.count == 1


def test_a_file_that_is_no_program_loads_into_a_session_that_was_never_opened(
    tmp_path: Path,
) -> None:
    path = tmp_path / "t.agl"
    path.write_text("int -> int\n")

    results = ReplSession().load_file(path)

    assert [(r.ok, r.kind) for r in results] == [(True, "type")]


def test_a_syntax_error_file_reports_the_error_at_file_positions(
    tmp_path: Path, scans: _Scans
) -> None:
    session = _session()
    path = tmp_path / "bad.agl"
    path.write_text("let a = 1\nlet b = (\n")
    scans.count = 0

    (result,) = session.load_file(path)

    assert not result.ok
    assert {d.source_label for d in result.diagnostics} == {str(path)}
    assert scans.count == 1


@pytest.mark.parametrize("text", ["1 + 2", "let a = 1", "int", "1 / 0"])
def test_the_plain_console_lexes_an_entry_once_from_prompt_to_result(
    text: str, scans: _Scans
) -> None:
    session = _session()
    scans.count = 0

    run_plain_console(session, stdin=io.StringIO(text + "\n"), stdout=io.StringIO())

    assert scans.count == 1


@pytest.mark.parametrize("text", ["1 + 2", "let a = 1", "int"])
def test_the_rich_console_lexes_an_entry_once_from_prompt_to_result(
    text: str, scans: _Scans
) -> None:
    def scanned(keystrokes: str) -> int:
        scans.count = 0
        drive(keystrokes, session=ReplSession(default_stdlib=False))
        return scans.count

    # Opening the session lexes its own bootstrap source.
    assert scanned(text + "\r\x04") - scanned("\x04") == 1


@pytest.mark.parametrize("text", ["int;", "Option[int];", "int -> int;"])
def test_a_bare_type_entry_may_end_with_a_semicolon(text: str) -> None:
    session = _session()

    result = session.eval_entry(text)

    assert result.kind == "type"
    assert result.ok


def test_an_unknown_name_followed_by_a_semicolon_is_rejected_not_crashed() -> None:
    session = _session()

    result = session.eval_entry("nope;")

    assert not result.ok


@pytest.mark.parametrize("text", ["let x = 42", "record R", "ask $"])
def test_classifying_an_entry_from_its_lex_scans_nothing_more(text: str, scans: _Scans) -> None:
    lexed = lex_source(text)
    scans.count = 0

    assert is_incomplete(lexed) == is_incomplete(text)
    assert has_runnable_statements(lexed)
    assert scans.count == 1  # only the str spelling above lexes


def test_lexing_the_same_text_again_is_a_new_pass(scans: _Scans) -> None:
    scans.count = 0

    lex_source("1 + 2")
    lex_source("1 + 2")

    assert scans.count == 2
