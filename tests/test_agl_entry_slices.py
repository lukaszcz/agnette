"""A module's top-level entries are read off its one lex and parse, as scanning each would."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from types import FrameType
from typing import cast

import pytest
from lark.lexer import Token

from agnette.agl.lexer import LexedSource, lex_replay, lex_source
from agnette.agl.parser import entry_slices, parse_program_seeded, parse_program_tree

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


def _position(token: Token) -> tuple[str, str, int, int, int, int, int | None, int | None]:
    return (
        token.type,
        str(token),
        cast(int, token.start_pos),
        cast(int, token.end_pos),
        cast(int, token.line),
        cast(int, token.column),
        token.end_line,
        token.end_column,
    )


def _parsed(source: str) -> tuple[LexedSource, list[tuple[int, int]]]:
    """The one lex and program parse of *source*, and its entries' ranges as a load has them."""
    lexed = lex_source(source)
    with lex_replay(lexed):
        tree = parse_program_tree(source)
    return lexed, tree.entry_ranges()


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


def test_an_entry_of_a_file_without_a_program_parse_is_read_off_its_lex_alone() -> None:
    source = "let a = 1\nlet b = 2\n"
    lexed = lex_source(source)

    slices = entry_slices(lexed, [(0, 9), (10, 19)])

    assert [s and s.text for s in slices] == ["let a = 1", "let b = 2"]
    assert all(s and "start" not in s.parses for s in slices)
    assert [s and s.tokens for s in slices] == [lex_source(s.text).tokens for s in slices if s]


def test_no_entry_is_read_off_a_failed_lex() -> None:
    lexed = lex_source('let a = "unterminated\nlet b = 2\n')

    assert lexed.failure is not None
    assert entry_slices(lexed, [(0, 21), (22, 31)]) == [None, None]


def test_an_entry_of_several_items_carries_their_parse() -> None:
    source = "let a = 1; let b = a\nlet c = 3\n"
    lexed, _ = _parsed(source)
    ranges = [(0, 20), (21, 30)]

    slices = entry_slices(lexed, ranges)

    for (start, end), read in zip(ranges, slices):
        assert read is not None
        text = source[start:end]
        with lex_replay(read):
            from_slice = parse_program_seeded(text, start_id=0)
        assert repr(from_slice) == repr(parse_program_seeded(text, start_id=0))
        assert "start" in read.parses


def test_an_entry_of_no_items_is_read_off_its_lex_alone() -> None:
    source = "let a = 1\n# alone\nlet b = 2\n"
    lexed, _ = _parsed(source)

    slices = entry_slices(lexed, [(0, 9), (10, 17), (18, 27)])

    assert [s and "start" in s.parses for s in slices] == [True, False, True]


def test_the_entries_an_item_crosses_are_no_pass_of_their_own() -> None:
    source = "let a = 1\ndef f() -> int =\n  2\nlet b = 3\n"
    lexed, _ = _parsed(source)

    slices = entry_slices(lexed, [(0, 9), (10, 26), (27, 30), (31, 40)])

    assert [s is None for s in slices] == [False, True, True, False]


def test_the_entries_a_token_crosses_are_no_pass_of_their_own() -> None:
    source = "let a = 1 + 22\nlet b = 3\n"
    lexed = lex_source(source)

    slices = entry_slices(lexed, [(0, 13), (13, 14), (15, 24)])

    assert [s is None for s in slices] == [True, True, False]


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


type _Tracer = Callable[[FrameType, str, object], _Tracer | None]
_SLICING_SOURCES = ("lexer/lexer.py", "lexer/positions.py", "parser/parser.py")


def _slicing_work(entries: int) -> int:
    """The elements and characters read off a file of *entries* entries to slice them.

    Plus the lines of lexer and parser code the slicing runs, so a loop over the entries made
    for each token counts.
    """
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

    lines = 0

    def count_lines(frame: FrameType, event: str, arg: object) -> _Tracer | None:
        nonlocal lines
        if event == "line" and frame.f_code.co_filename.endswith(_SLICING_SOURCES):
            lines += 1
        return count_lines

    previous = sys.gettrace()
    sys.settrace(count_lines)
    try:
        slices = entry_slices(
            replace(lexed, text=text, tokens=tokens, spaced_qualifiers=spaced, tab_warnings=tabs),
            spans,
        )
    finally:
        sys.settrace(previous)

    assert all(slices)
    return tokens.reads + spaced.reads + tabs.reads + text.scanned + lines


def test_slicing_a_file_reads_it_in_linear_work() -> None:
    small, medium, large = _slicing_work(100), _slicing_work(200), _slicing_work(400)

    # Linear work doubles with the file, up to a constant; a quadratic share adds a growing excess.
    assert medium - 2 * small < small // 100
    assert large - 2 * medium < small // 100


@pytest.mark.parametrize(
    ("source", "ranges"),
    [
        (
            "let a = 1\ndef f() -> int =\n  let y = a\n  y + 1\n\nlet b = f()\n",
            [(0, 9), (10, 46), (48, 59)],
        ),
        (
            "let a = 1\ndef f() -> int =\n  case a of\n    | 1 =>\n      2 # note\nlet b = f()\n",
            [(0, 9), (10, 64), (65, 76)],
        ),
    ],
    ids=["newline-after", "comment-last"],
)
def test_an_entry_ending_in_an_indented_block_is_read_without_the_newline_after_it(
    source: str, ranges: list[tuple[int, int]]
) -> None:
    lexed, _ = _parsed(source)

    slices = entry_slices(lexed, ranges)

    for (start, end), read in zip(ranges, slices):
        assert read is not None
        text = source[start:end]
        with lex_replay(read):
            from_slice = parse_program_seeded(text, start_id=0)
        assert repr(from_slice) == repr(parse_program_seeded(text, start_id=0))
        assert "start" in read.parses


@pytest.mark.parametrize(
    "entry",
    [
        "def v() -> int =\n  $\n    line one\n    line two\n",
        'def v(n: int) -> int =\n  "a{n}"\n',
        'let v = """\nabc\n"""\n',
        'let v = """\nabc\n""" # c\n',
        "scope S\n  def q() -> int =\n    1\nend S # tail\n",
        "def v() -> int =\n  1 # c\n",
        "let a = 1\n",
        "let a = 1\n# c",
        "let a = 1 # t\n# c",
        "let a = 1; let b = 2\n# c",
        "scope S\n  let q = 1\nend S # t\n\n# c",
    ],
    ids=(
        "verbatim",
        "interpolation",
        "triple-quoted",
        "triple-quoted-comment",
        "scope-end",
        "comment",
        "plain",
        "comment-line",
        "comment-after-item",
        "items-comment-line",
        "scope-end-comment-line",
    ),
)
def test_an_entry_is_read_as_scanned_and_parsed_or_not_at_all(entry: str) -> None:
    source = f"let pre = 0\n{entry}\nlet post = 1\n"
    start = len("let pre = 0\n")
    ranges = [(0, 11), (start, start + len(entry)), (start + len(entry) + 1, len(source) - 1)]
    lexed, _ = _parsed(source)

    read = entry_slices(lexed, ranges)[1]

    if read is None:
        return
    assert [_position(t) for t in read.tokens] == [_position(t) for t in lex_source(entry).tokens]
    with lex_replay(read):
        from_slice = parse_program_seeded(entry, start_id=0)
    assert repr(from_slice) == repr(parse_program_seeded(entry, start_id=0))
