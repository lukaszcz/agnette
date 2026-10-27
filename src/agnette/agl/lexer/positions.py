"""Token positions as source spans."""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import replace
from typing import cast

from lark.lexer import Token

from agnette.agl.diagnostics import Diagnostic
from agnette.agl.syntax.spans import UNKNOWN_SOURCE, SourceId, SourceSpan


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
        self._starts = [0, *(match.end() for match in re.finditer("\n", text))]

    def line(self, offset: int) -> int:
        """The 1-based line holding the 0-based *offset*."""
        return bisect_right(self._starts, offset)

    def offset(self, line: int, column: int) -> int:
        """The 0-based offset of 1-based *column* on 1-based *line*."""
        return self._starts[line - 1] + column - 1


class Rebase:
    """Maps positions in a text to the positions they have in its tail starting at *start*.

    Positions are 1-based lines and columns and 0-based offsets, as tokens carry them. With
    *end*, the tail stops there: a position past it, such as the newline a closing layout
    token covers after the tail's last item, is read as *end*.
    """

    def __init__(self, lines: LineIndex, start: int, end: int | None = None) -> None:
        self._lines = lines
        self._start = start
        self._end = end
        self._line = lines.line(start)
        self._first_column = start - lines.offset(self._line, 1)

    def offset(self, offset: int) -> int:
        return offset - self._start

    def line(self, line: int) -> int:
        return line - self._line + 1

    def column(self, line: int, column: int) -> int:
        return column - self._first_column if line == self._line else column

    def position(self, offset: int, line: int, column: int) -> tuple[int, int, int]:
        """The rebased ``(offset, line, column)`` of a position, bounded by the tail's end."""
        if self._end is not None and offset > self._end:
            offset = self._end
            line = self._lines.line(offset)
            column = offset - self._lines.offset(line, 1) + 1
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

    def advisory(self, advisory: Diagnostic) -> Diagnostic:
        """Return a copy of *advisory* with its positions, and only they, rebased."""
        line, end_line = advisory.line, cast(int, advisory.end_line)
        return replace(
            advisory,
            line=self.line(line),
            column=self.column(line, cast(int, advisory.column)),
            end_line=self.line(end_line),
            end_column=self.column(end_line, cast(int, advisory.end_column)),
        )
