"""A module's top-level entries are read off its one lex and parse, as scanning each would."""

from __future__ import annotations

import pytest

from agnette.agl.lexer import LexedSource, lex_replay, lex_source, lexed_slice
from agnette.agl.parser import entry_slices, parse_program_seeded, parse_repl_transcript

_SOURCES = {
    "plain": "let a = 1\nlet b = a + 1\nb\n",
    "same-line": "let a = 1; let b = 2\nlet c = a; b\n",
    "indented": "def f(x: int) -> int =\n  let y = x + 1\n  if y > 2 => y else => 0\n\nf(2)\n",
    "attributes": '@!doc("m")\n@!allow("x")\nlet a = 1\ndef g() -> int =\n  a\n',
    "scope": "scope S\n  def q() -> int = 9\n\n  scope T\n    let k = 3\n  end T\nend S\n\nS::q()\n",
    "comments": "let a = 1 # one\n# alone\n\n\nlet b = 2 # two\n",
    "records": "record P\n  x: int\n  y: int\nenum E =\n  | A\n  | B(v: int)\nlet p = P(x = 1)\n",
    "text": 'let s = "a {1 + 1} b"\nlet t = $"x {s}"\nlet u = $\n  raw\n  more\n',
}


def _parsed(source: str) -> tuple[LexedSource, list[tuple[int, int]]]:
    """The one lex and program parse of *source*, and its entries' ranges as a load has them."""
    lexed = lex_source(source)
    with lex_replay(lexed):
        program = parse_repl_transcript(source)
    spans = [(item.span.start_offset, item.span.end_offset) for item in program.body.items]
    if program.attributes:
        spans[0] = (program.attributes[0].span.start_offset, spans[0][1])
    return lexed, spans


@pytest.mark.parametrize("name", sorted(_SOURCES))
def test_an_entry_read_off_the_file_is_the_entry_scanned_and_parsed(name: str) -> None:
    source = _SOURCES[name]
    lexed, spans = _parsed(source)

    slices = entry_slices(lexed, spans)

    assert len(slices) == len(spans)
    for (start, end), read in zip(spans, slices):
        assert read is not None
        text = source[start:end]
        scanned = lex_source(text)
        assert read.text == text
        assert [
            (t.type, str(t), t.start_pos, t.end_pos, t.line, t.column) for t in read.tokens
        ] == [(t.type, str(t), t.start_pos, t.end_pos, t.line, t.column) for t in scanned.tokens]
        with lex_replay(read):
            from_slice = parse_program_seeded(text, start_id=5)
        assert repr(from_slice) == repr(parse_program_seeded(text, start_id=5))


@pytest.mark.parametrize(
    "source",
    ["def f() -> int =\n\t1\nlet b = 2\n", "let a = 1\nlet b = app/config ::x\nlet c = 3\n"],
    ids=("tab", "spaced-qualifier"),
)
def test_an_entry_with_advisories_is_left_to_be_scanned(source: str) -> None:
    lexed, spans = _parsed(source)

    slices = entry_slices(lexed, spans)

    assert slices[-1] is not None
    assert None in slices


def test_no_entry_is_read_off_a_file_without_a_program_parse() -> None:
    lexed = lex_source("let a = 1\nlet b = 2\n")

    assert entry_slices(lexed, [(0, 9), (10, 19)]) == [None, None]


def test_a_failed_lex_has_no_slice() -> None:
    lexed = lex_source('let a = 1\nlet s = "unterminated\n')

    assert lexed.failure is not None
    assert lexed_slice(lexed, 0, 9) is None
