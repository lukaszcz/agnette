"""AgL custom lexer package.

Public API
----------
- :class:`LexError` — span-aware lexical error raised by the scanner /
  layout filter.
- :class:`IncompleteInputError` — a :class:`LexError` subtype raised only when
  more input could complete the construct; :class:`UnterminatedTripleQuotedStringError`
  narrows it to an unclosed triple-quoted string.
- :func:`tokenize` — strict token iterator for tests and tooling.
- :func:`tokenize_recover` — tokens plus diagnostics from every lexer stage;
  rejected scanner spans carry recovery-only error or incomplete-tail tokens.
- :data:`RESERVED_OPERATOR_SPELLINGS` — operator spellings with their own token types.
- :func:`lex_comment_spans` — offsets of the ``#`` comments the scan skipped
  for standalone consumers (comments carry no token).
- :func:`lex_open_quote_start` — recovery scanner recognition under the REPL's
  legacy raw-quote completion policy; not AgL string semantics.
- :func:`spaced_qualifier_collector` — collects lexical advisories for
  qualifier runs broken by whitespace before ``::``.
- :func:`token_collector` — the lexer's single materialized token pass, for a
  diagnostic that must read tokens beyond the offending one without re-lexing.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    tab_warning_labels,
    token_collector,
    unclosed_scope_path,
)
from agnette.agl.lexer.scanner import (
    RESERVED_OPERATOR_SPELLINGS,
    _Scanner,
    lex_comment_spans,
    lex_open_quote_start,
    scan,
)

__all__ = [
    "RESERVED_OPERATOR_SPELLINGS",
    "IncompleteInputError",
    "LexError",
    "UnterminatedTripleQuotedStringError",
    "lex_comment_spans",
    "lex_open_quote_start",
    "spaced_qualifier_collector",
    "tab_warning_collector",
    "tab_warning_labels",
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
    """Return tokens and diagnostics without rejecting malformed or incomplete input.

    Strict-accepted sources retain exactly the strict token stream order, values,
    and positions. Spans index normalized source; skipped trivia, trimmed literal
    margins, and synthetic layout positions are not a literal source partition.
    Consumers sort spans, clip overlaps, and fill unclaimed slices separately.
    Layout and clinging-slash diagnostics flag spans without replacing tokens.
    ``STRING_TAIL`` / ``VERBATIM_TAIL`` identify incomplete literals from their
    opening delimiter to EOF, including tails with Unicode errors. Recursion
    exhaustion flags the abandoned construct through EOF without dropping earlier
    diagnostics. Abandoned constructs receive zero-width closers
    so subsequent CODE lines retain their layout. Diagnostics follow strict encounter order
    (Unicode preflight, scanner/layout with lookahead, then module passes).
    """
    result = _tokenize_recover(source)
    return result.tokens, result.errors


@dataclass(frozen=True, slots=True)
class _RecoveryResult:
    tokens: list[Token]
    errors: list[LexError]
    comments: list[tuple[int, int]]


def _tokenize_recover(source: str) -> _RecoveryResult:
    """Materialize tokens, diagnostics and skipped comments in one recovery pass."""
    scanner = _Scanner(source, recover=True)
    tokens = apply_module_passes(
        list(layout(scanner.scan(), errors=scanner.errors)), source, errors=scanner.errors
    )
    return _RecoveryResult(tokens, scanner.errors, scanner.comment_spans)
