"""Token positions as source spans."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from lark.lexer import Token

from agnette.agl.diagnostics import Diagnostic
from agnette.agl.syntax.spans import UNKNOWN_SOURCE, SourceId, SourceSpan

#: Columns a tab advances indentation to the next multiple of.
TAB_LEN = 4


def token_span(tok: Token, source: SourceId = UNKNOWN_SOURCE) -> SourceSpan:
    """Return *tok*'s span in *source*.

    Every token the AgL lexer emits, and every token Lark borrows a position
    from one, carries all six position fields; Lark merely types them optional.
    """
    return SourceSpan(
        start_line=cast(int, tok.line),
        start_col=cast(int, tok.column),
        end_line=cast(int, tok.end_line),
        end_col=cast(int, tok.end_column),
        start_offset=cast(int, tok.start_pos),
        end_offset=cast(int, tok.end_pos),
        source=source,
    )


class LineIndex:
    """Where each line of a text starts, to place offsets on lines in logarithmic time."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._starts = [0, *(match.end() for match in re.finditer("\n", text))]

    def line(self, offset: int) -> int:
        """The 1-based line holding the 0-based *offset*."""
        return bisect_right(self._starts, offset)

    def offset(self, line: int, column: int) -> int:
        """The 0-based offset of 1-based lexer *column* on 1-based *line*; see :meth:`column`.

        A column inside a tab's expansion is the tab's.
        """
        begin = self._starts[line - 1]
        width = 0
        offset = begin
        while width + 1 < column and self.text[offset : offset + 1] in (" ", "\t"):
            width += TAB_LEN - width % TAB_LEN if self.text[offset] == "\t" else 1
            offset += 1
        return offset + column - width - 1

    def line_start(self, offset: int) -> int:
        """The 0-based offset where the line holding *offset* starts."""
        return self._starts[self.line(offset) - 1]

    def column(self, offset: int) -> int:
        """The 1-based column of *offset* as the lexer counts it, indentation tab-expanded."""
        begin = self._starts[self.line(offset) - 1]
        indent = self.text[begin:offset]
        body = indent.lstrip(" \t")
        width = 0
        for char in indent[: len(indent) - len(body)]:
            width += TAB_LEN - width % TAB_LEN if char == "\t" else 1
        return width + len(body) + 1


def range_at(starts: Sequence[int], offset: int) -> int:
    """The index of the range starting last at or before *offset*; the first before them all."""
    return max(bisect_right(starts, offset) - 1, 0)


def entry_padding(lines: LineIndex, start: int, base: int) -> int:
    """The columns of indentation the entry of a file starting at *start* is given.

    An entry beginning its line carries the line's own indentation. One that follows another
    item on its line is indented by the *base* of the file, where its later lines are measured
    from, so that read alone it is laid out as in the file.
    """
    return 0 if lines.line_start(start) == start else base


def entry_text(lines: LineIndex, start: int, end: int, padding: int) -> str:
    """The text of the entry of a file at ``[start, end)``: itself, indented by its *padding*."""
    return " " * padding + lines.text[start:end]


def tab_extent(advisory: Diagnostic) -> tuple[int, int, int, int]:
    """Return the ``(line, column, end_line, end_column)`` of a located TAB advisory."""
    return (
        cast(int, advisory.line),
        cast(int, advisory.column),
        cast(int, advisory.end_line),
        cast(int, advisory.end_column),
    )


def tab_position(advisory: Diagnostic) -> tuple[int, int]:
    """Return the ``(line, column)`` of a located TAB advisory."""
    line, column, _end_line, _end_column = tab_extent(advisory)
    return line, column


class Rebase:
    """Maps positions in a text to the positions they have in the entry starting at *start*.

    Positions are 1-based lines and columns and 0-based offsets, as tokens carry them. The
    entry is the tail of the text from *start* (:func:`entry_text`, indented by *padding*,
    :func:`entry_padding`).
    """

    def __init__(
        self,
        lines: LineIndex,
        start: int,
        padding: int = 0,
    ) -> None:
        self._start = start
        self._line = lines.line(start)
        self._padding = padding
        self._first_column = lines.column(start) - 1 - self._padding

    def offset(self, offset: int) -> int:
        return offset - self._start + self._padding

    def line(self, line: int) -> int:
        return line - self._line + 1

    def column(self, line: int, column: int) -> int:
        return column if line != self._line else column - self._first_column

    def position(self, offset: int, line: int, column: int) -> tuple[int, int, int]:
        """The rebased ``(offset, line, column)`` of a position."""
        return self.offset(offset), self.line(line), self.column(line, column)

    def token(self, tok: Token) -> Token:
        """Return a copy of *tok* at its rebased position."""
        start, line, column = self.position(
            cast(int, tok.start_pos), cast(int, tok.line), cast(int, tok.column)
        )
        end, end_line, end_column = self.position(
            cast(int, tok.end_pos), cast(int, tok.end_line), cast(int, tok.end_column)
        )
        return Token(tok.type, tok.value, start, line, column, end_line, end_column, end)

    def span(self, span: SourceSpan) -> SourceSpan:
        """Return a copy of *span* at its rebased position."""
        return replace(
            span,
            start_line=self.line(span.start_line),
            start_col=self.column(span.start_line, span.start_col),
            end_line=self.line(span.end_line),
            end_col=self.column(span.end_line, span.end_col),
            start_offset=self.offset(span.start_offset),
            end_offset=self.offset(span.end_offset),
        )

    def diagnostic(self, diagnostic: Diagnostic) -> Diagnostic:
        """Return a copy of *diagnostic* with its positions rebased."""
        line, column, end_line, end_column = tab_extent(diagnostic)
        return replace(
            diagnostic,
            line=self.line(line),
            column=self.column(line, column),
            end_line=self.line(end_line),
            end_column=self.column(end_line, end_column),
        )
