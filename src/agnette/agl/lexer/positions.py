"""Token positions as source spans."""

from __future__ import annotations

from typing import cast

from lark.lexer import Token

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


class Rebase:
    """Maps positions in a text to the positions they have in its tail starting at *start*.

    Positions are 1-based lines and columns and 0-based offsets, as tokens carry them.
    """

    def __init__(self, text: str, start: int) -> None:
        self._start = start
        self._line = text.count("\n", 0, start) + 1
        self._first_column = start - (text.rfind("\n", 0, start) + 1)

    def offset(self, offset: int) -> int:
        return offset - self._start

    def line(self, line: int) -> int:
        return line - self._line + 1

    def column(self, line: int, column: int) -> int:
        return column - self._first_column if line == self._line else column

    def token(self, tok: Token) -> Token:
        """Return a copy of *tok* at its rebased position."""
        start, end_line, end = (
            cast(int, tok.start_pos),
            cast(int, tok.end_line),
            cast(int, tok.end_pos),
        )
        line = cast(int, tok.line)
        return Token(
            tok.type,
            tok.value,
            self.offset(start),
            self.line(line),
            self.column(line, cast(int, tok.column)),
            self.line(end_line),
            self.column(end_line, cast(int, tok.end_column)),
            self.offset(end),
        )
