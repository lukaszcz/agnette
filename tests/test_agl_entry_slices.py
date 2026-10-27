"""A module's top-level entries are read off its one lex and parse, as scanning each would."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

from agnette.agl.lexer import LexedSource, lex_replay, lex_source
from agnette.agl.parser import entry_slices, parse_program_seeded, parse_repl_transcript

_SOURCES = {
    "plain": "let a = 1\nlet b = a + 1\nb\n",
    "same-line": "let a = 1; let b = 2\nlet c = a; b\n",
    "indented": "def f(x: int) -> int =\n  let y = x + 1\n  if y > 2 => y else => 0\n\nf(2)\n",
    "attributes": '@!doc("m")\n@!allow("x")\nlet a = 1\ndef g() -> int =\n  a\n',
    "scope": "scope S\n  def q() = 9\n\n  scope T\n    let k = 3\n  end T\nend S\n\nS::q()\n",
    "comments": "let a = 1 # one\n# alone\n\n\nlet b = 2 # two\n",
    "records": "record P\n  x: int\nenum E =\n  | A\n  | B(v: int)\nlet p = P(1)\n",
    "tab": "let a = 1\ndef f() -> int =\n\t1\nlet b = 2\ndef g() -> int =\n\t\t2\n",
    "spaced-qualifier": "let a = 1\nlet b = app/config ::x\nlet c = 3\nlet d = a::y\n",
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
        assert read.tab_warnings == scanned.tab_warnings
        assert read.spaced_qualifiers == scanned.spaced_qualifiers
        with lex_replay(read):
            from_slice = parse_program_seeded(text, start_id=5)
        assert repr(from_slice) == repr(parse_program_seeded(text, start_id=5))


def test_no_entry_is_read_off_a_file_without_a_program_parse() -> None:
    lexed = lex_source("let a = 1\nlet b = 2\n")

    assert entry_slices(lexed, [(0, 9), (10, 19)]) == [None, None]


class _Counted[T](tuple[T, ...]):
    """A tuple counting the elements its iterations yield."""

    reads = 0

    def __iter__(self) -> Iterator[T]:
        for item in super().__iter__():
            self.reads += 1
            yield item


class _CountedText(str):
    """A text counting the characters its newline searches cover."""

    scanned = 0

    def count(self, sub: str, start: int | None = 0, end: int | None = None, /) -> int:
        self.scanned += (len(self) if end is None else end) - (start or 0)
        return super().count(sub, start, end)

    def rfind(self, sub: str, start: int | None = 0, end: int | None = None, /) -> int:
        self.scanned += (len(self) if end is None else end) - (start or 0)
        return super().rfind(sub, start, end)


def _slicing_work(entries: int) -> int:
    """The elements and characters read off a file of *entries* entries to slice them."""
    source = "".join(
        f"def f{i}() -> int =\n\t{i}\nlet q{i} = app/config ::x{i}\n" for i in range(entries)
    )
    lexed, spans = _parsed(source)
    text = _CountedText(lexed.text)
    tokens, spaced, tabs = (
        _Counted(lexed.tokens),
        _Counted(lexed.spaced_qualifiers),
        _Counted(lexed.tab_warnings),
    )
    assert spaced and tabs

    slices = entry_slices(
        replace(lexed, text=text, tokens=tokens, spaced_qualifiers=spaced, tab_warnings=tabs),
        spans,
    )

    assert all(slices)
    return tokens.reads + spaced.reads + tabs.reads + text.scanned


def test_slicing_a_file_reads_it_in_linear_work() -> None:
    small, large = _slicing_work(100), _slicing_work(200)

    assert large < 2.5 * small


def test_no_entry_is_read_off_a_file_for_other_ranges_than_its_entries() -> None:
    source = "let a = 1\nlet b = 2\n"
    lexed, spans = _parsed(source)

    assert entry_slices(lexed, spans[:1]) == [None]
