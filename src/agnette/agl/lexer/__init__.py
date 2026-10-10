"""AgL custom lexer package.

Public API
----------
- :class:`LexError` — span-aware lexical error raised by the scanner /
  layout filter.
- :class:`IncompleteInputError` — a :class:`LexError` subtype raised only when
  more input could complete the construct; :class:`UnterminatedTripleQuotedStringError`
  narrows it to an unclosed triple-quoted string.
- :func:`tokenize` — strict token iterator for tests and tooling.
- :func:`tokenize_recover` — tokens plus scanner diagnostics; rejected spans
  carry the recovery-only ``ERROR`` token type.
- :func:`lex_comment_spans` — offsets of the ``#`` comments the scan skipped
  (comments carry no token, so highlighters read them from here).
- :func:`spaced_qualifier_collector` — collects lexical advisories for
  qualifier runs broken by whitespace before ``::``.
- :func:`token_collector` — the lexer's single materialized token pass, for a
  diagnostic that must read tokens beyond the offending one without re-lexing.
"""

from __future__ import annotations

from typing import Iterator

from lark.lexer import Token

from agnette.agl.lexer.errors import (
    IncompleteInputError,
    LexError,
    UnterminatedTripleQuotedStringError,
)
from agnette.agl.lexer.layout import layout
from agnette.agl.lexer.lexer import (
    apply_module_passes,
    spaced_qualifier_collector,
    tab_warning_collector,
    token_collector,
    unclosed_scope_path,
)
from agnette.agl.lexer.scanner import _Scanner, lex_comment_spans, scan

__all__ = [
    "IncompleteInputError",
    "LexError",
    "UnterminatedTripleQuotedStringError",
    "lex_comment_spans",
    "spaced_qualifier_collector",
    "tab_warning_collector",
    "token_collector",
    "unclosed_scope_path",
    "tokenize",
    "tokenize_recover",
]


def tokenize(source: str) -> Iterator[Token]:
    """Tokenize *source* and yield :class:`lark.lexer.Token` objects.

    This is the public convenience entry point for tests and tooling; the
    Lark parser uses :class:`AglLexer` directly.
    """
    return iter(apply_module_passes(list(layout(scan(source))), source))


def tokenize_recover(source: str) -> tuple[list[Token], list[LexError]]:
    """Return tokens and scanner diagnostics without rejecting malformed literals.

    Strict-accepted sources retain exactly the strict token stream order, values,
    and positions. Spans index normalized source; skipped trivia, trimmed literal
    margins, and synthetic layout positions are not a literal source partition.
    Consumers sort spans, clip overlaps, and fill unclaimed slices separately.
    Layout, module-path and incomplete-input errors still raise.
    """
    scanner = _Scanner(source, recover=True)
    tokens = apply_module_passes(list(layout(scanner.scan())), source)
    return tokens, scanner.errors
