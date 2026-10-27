"""AglLexer: Lark custom lexer for the AgL DSL.

Composes :mod:`~agnette.agl.lexer.scanner` (raw tokens) with
:mod:`~agnette.agl.lexer.layout` (INDENT/DEDENT injection) to produce the full
token stream required by the Lark LALR parser.

The lexer is wired as::

    Lark(grammar, parser="lalr", lexer=AglLexer, propagate_positions=True)

The ``lex`` method receives ``lexer_state`` and ``parser_state`` from Lark.
We read the source text from ``lexer_state.text`` and yield
:class:`lark.lexer.Token` objects with full position information.

Grammar terminal names
----------------------
Lark creates uppercase terminal names from grammar string literals (e.g.
``"pass"`` → terminal ``PASS``).  Because the raw scanner emits lowercase
keyword types (``"pass"``, ``"let"``, etc.), ``lex()`` applies a one-to-one
remapping: ``GRAMMAR_TOKEN_REMAP`` maps each lowercase keyword type to its
uppercase equivalent so the LALR parser can look up the right parse-table
entry.

The public :func:`~agnette.agl.lexer.tokenize` helper goes through
``layout(scan())`` directly and preserves the lowercase types that tests
and downstream tooling rely on.
"""

from __future__ import annotations

import contextvars
import importlib.resources
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import cache
from typing import Iterator, TypeVar, cast

from lark import Lark, Tree
from lark.exceptions import UnexpectedToken
from lark.lexer import Lexer, LexerState, Token

from agnette.agl.diagnostics import Diagnostic
from agnette.agl.keywords import (
    KW_AS,
    KW_END,
    KW_EXPORT,
    KW_EXTENDS,
    KW_HIDING,
    KW_IMPORT,
    KW_SCOPE,
    KW_TYPE,
    KW_USE,
    OPERATOR_SOFT_KEYWORDS,
)
from agnette.agl.lexer.errors import LexError, ModuleRouteError
from agnette.agl.lexer.layout import layout
from agnette.agl.lexer.operators import (
    EXPRESSION_END_TYPES,
    INFIX_OPERAND_START_TYPES,
    SLASH_LEFT_OPERAND_TYPES,
    promotes_as_operator,
    scanner_token_type,
)
from agnette.agl.lexer.positions import LineIndex, Rebase, range_at, token_span, touched_ranges
from agnette.agl.lexer.scanner import _Scanner
from agnette.agl.lexer.tokens import (
    ARROW,
    ASSIGN,
    CALL_LBRACE,
    COLON,
    COMMA,
    DCOLON,
    DEDENT,
    DO_LSQB,
    END,
    EQ,
    EXPORT,
    GRAMMAR_TOKEN_REMAP,
    HIDING,
    IMPORT,
    INDENT,
    INDEX_LSQB,
    LAYOUT_TOKENS,
    LBRACE,
    LPAR,
    LSQB,
    MODPATH,
    MODQUAL,
    NAME,
    NEWLINE,
    OP_NAME,
    RBRACE,
    RPAR,
    RSQB,
    SCOPE,
    SEMICOLON,
    SLASH,
    STAR,
    THIN_ARROW,
    TYPEARG_LSQB,
    USE,
    WILDCARD,
)
from agnette.agl.syntax.advisories import SpacedQualifier
from agnette.util.scoping import ScopedVar

_T = TypeVar("_T")


@contextmanager
def _ambient_list_sink(var: contextvars.ContextVar[list[_T] | None]) -> Iterator[list[_T]]:
    """Bind *var* to a fresh empty list for the ``with`` block and yield it.

    Shared install/teardown for every ambient sink below: a ``ContextVar``
    holding a list that a single materialized lexer pass deposits into,
    keeping nested/reentrant parses isolated.
    """
    sink: list[_T] = []
    with ScopedVar(var, sink):
        yield sink


# Ambient sink for TAB advisories produced during a Lark-driven parse.  The
# lexer scans the source exactly once (no separate TAB pass); when a sink is
# active, ``AglLexer.lex`` deposits the scan's TAB advisories into it so the
# caller (e.g. ``PipelineDriver.prepare``) can surface them alongside parse
# diagnostics.
_TAB_WARNING_SINK: contextvars.ContextVar[list[Diagnostic] | None] = contextvars.ContextVar(
    "agl_tab_warning_sink", default=None
)


@contextmanager
def tab_warning_collector() -> Iterator[list[Diagnostic]]:
    """Collect TAB advisories produced by parses within the ``with`` block.

    Yields a list that ``AglLexer.lex`` appends to as it scans.  The list is
    populated even when the parse fails (the scan runs to completion before the
    grammar is consulted), so callers get every TAB advisory on every path.
    """
    with _ambient_list_sink(_TAB_WARNING_SINK) as sink:
        yield sink


@contextmanager
def tab_warning_labels(source_label: str | None) -> Iterator[None]:
    """Label the TAB advisories produced within the block as coming from *source_label*.

    The scanner sees text, not its origin; the parse that knows the source
    stamps its advisories so each one names its own module.  Applied even when
    the block raises.
    """
    sink = _TAB_WARNING_SINK.get()
    start = 0 if sink is None else len(sink)
    try:
        yield
    finally:
        if sink is not None and source_label is not None:
            sink[start:] = [replace(w, source_label=source_label) for w in sink[start:]]


# Ambient sink for spaced-qualifier advisories, mirroring the TAB sink above.
# ``_merge_modqual`` deposits into it as it walks the token stream, so the
# advisories are complete whether or not the grammar later accepts the program.
_SPACED_QUALIFIER_SINK: contextvars.ContextVar[list[SpacedQualifier] | None] = (
    contextvars.ContextVar("agl_spaced_qualifier_sink", default=None)
)


@contextmanager
def spaced_qualifier_collector() -> Iterator[list[SpacedQualifier]]:
    """Collect spaced-qualifier advisories produced by lexing within the ``with`` block.

    Yields a list that the module-qualifier merge pass appends to.  Callers keep
    the result alongside the parsed module so later passes can explain a
    reference that whitespace turned into an unrelated expression.
    """
    with _ambient_list_sink(_SPACED_QUALIFIER_SINK) as sink:
        yield sink


# Ambient sink for the lexer's single materialized token pass.  A Lark parse
# error only ever sees the *offending* token; a diagnostic that must read
# other tokens on the same line (e.g. the `$`-spacing hint) needs the whole
# pass without re-lexing the source, so ``AglLexer.lex`` deposits its result
# list here once scanning succeeds (a lex error leaves the sink empty).
_TOKEN_SINK: contextvars.ContextVar[list[Token] | None] = contextvars.ContextVar(
    "agl_token_sink", default=None
)


@contextmanager
def token_collector() -> Iterator[list[Token]]:
    """Collect the materialized token list produced by a parse within the ``with`` block.

    Yields a list that ``AglLexer.lex`` fills with every token of the parse's
    single lex pass once scanning completes without a ``LexError``.
    """
    with _ambient_list_sink(_TOKEN_SINK) as sink:
        yield sink


def _deposit(var: contextvars.ContextVar[list[_T] | None], items: Iterable[_T]) -> None:
    """Append *items* to the ambient sink *var*, if one is active."""
    sink = var.get()
    if sink is not None:
        sink.extend(items)


@dataclass(frozen=True, slots=True)
class LexedSource:
    """One lex pass over a source text, with everything any consumer of the pass reads.

    A pass records its advisories whether or not a sink is active when it runs, so
    a replay (:func:`lex_replay`) feeds the active sinks exactly as scanning would.
    ``tokens`` is empty when the pass ended in ``failure``; the advisories are
    then those found before it. TAB advisories are unlabeled (see :func:`tab_warning_labels`).
    ``failure`` is never raised itself; each use raises ``failure.renewed()``.

    ``parses`` records what each grammar start rule made of the tokens, as the first
    parse within a replay found it (a tree or the parser's rejection): a later parse of
    the same text from that start reuses it (:func:`replayed_parse`).
    """

    text: str
    tokens: tuple[Token, ...]
    spaced_qualifiers: tuple[SpacedQualifier, ...]
    tab_warnings: tuple[Diagnostic, ...]
    failure: LexError | None = None
    parses: dict[str, Tree | UnexpectedToken] = field(
        default_factory=dict, compare=False, repr=False
    )


_LEX_REPLAY: contextvars.ContextVar[LexedSource | None] = contextvars.ContextVar(
    "agl_lex_replay", default=None
)


def lex_source(source: str) -> LexedSource:
    """Scan *source* through the whole lexer pipeline, once, recording the pass.

    Lexing depends on the text alone, not on the grammar start rule, so a caller that
    reads one text several times (a REPL entry classified, then evaluated) lexes it here
    and hands the pass on: :func:`lex_replay` for parses, or the :class:`LexedSource`
    itself.
    """
    scanner = _Scanner(source)
    with spaced_qualifier_collector() as spaced:
        try:
            after_remap = list(_remap(layout(scanner.scan())))
            tokens = tuple(
                _mark_typearg_lsqb(
                    _remap_adjacent_brackets(apply_module_passes(after_remap, source))
                )
            )
            failure = None
        except LexError as exc:
            tokens, failure = (), exc.renewed()
    return LexedSource(source, tokens, tuple(spaced), tuple(scanner.tab_warnings), failure)


def lexed_source(source: str | LexedSource) -> LexedSource:
    """Return *source* if it is already a recorded pass, else lex it."""
    return source if isinstance(source, LexedSource) else lex_source(source)


@contextmanager
def lex_replay(lexed: LexedSource | None) -> Iterator[None]:
    """Make parses within the block reuse *lexed* for the text it recorded.

    A parse of any other text, and every parse without *lexed*, scans its text as usual.
    """
    with ScopedVar(_LEX_REPLAY, lexed):
        yield


def lexed_slices(
    lexed: LexedSource, ranges: Sequence[tuple[int, int]], lines: LineIndex
) -> list[LexedSource | None]:
    """Return the pass over each of *ranges* of ``lexed.text``, read off *lexed*, not scanned.

    *ranges* are disjoint and in order, and *lines* indexes the text. A slice's tokens are
    those of *lexed* within its range, rebased to it. Each spaced qualifier and TAB advisory
    of *lexed* goes to the one slice whose range starts last at or before it, the first
    slice taking those before it, keeping the whole pass's message with only its positions
    rebased. One walk over each of *lexed*'s sequences. The dedents closing a range's last
    block past its end sit on its last token, as scanning its text alone puts them; the newline
    after it is no token of it. A range is no pass of its own, and reads ``None``, when a
    token crosses its boundary; all of them are when *lexed* failed.
    """
    if lexed.failure is not None:
        return [None] * len(ranges)
    starts = [start for start, _ in ranges]
    rebases = [Rebase(lines, start) for start in starts]

    tokens: list[list[Token]] = [[] for _ in ranges]
    last_code: list[Token | None] = [None] * len(ranges)
    crossed: set[int] = set()
    for token in lexed.tokens:
        start_pos, end_pos = cast(int, token.start_pos), cast(int, token.end_pos)
        index = range_at(starts, start_pos)
        anchor = last_code[index]
        if starts[index] <= start_pos and end_pos <= ranges[index][1]:
            rebased = rebases[index].token(token)
            tokens[index].append(rebased)
            if token.type not in LAYOUT_TOKENS:
                last_code[index] = rebased
        elif token.type == DEDENT and start_pos >= ranges[index][1] and anchor is not None:
            # A range ending in a block, short of the newline, is closed by dedents past its end;
            # scanning its text alone closes it at its last token.
            tokens[index].append(
                Token(
                    DEDENT,
                    token.value,
                    cast(int, anchor.start_pos),
                    anchor.line,
                    anchor.column,
                    anchor.end_line,
                    anchor.end_column,
                    anchor.end_pos,
                )
            )
        elif token.type != NEWLINE or start_pos < ranges[index][1]:
            # The newline after a range is no token of it; any other token outside is a crossing.
            crossed.update(touched_ranges(ranges, starts, start_pos, end_pos))
    spaced: list[list[SpacedQualifier]] = [[] for _ in ranges]
    for qualifier in lexed.spaced_qualifiers:
        offset = qualifier.run_start_offset
        index = range_at(starts, offset)
        rebase = rebases[index]
        spaced[index].append(
            replace(
                qualifier,
                run_start_offset=rebase.offset(offset),
                dcolon_span=rebase.span(qualifier.dcolon_span),
            )
        )
    tabs: list[list[Diagnostic]] = [[] for _ in ranges]
    for advisory in lexed.tab_warnings:
        index = range_at(starts, lines.offset(advisory.line, cast(int, advisory.column)))
        tabs[index].append(rebases[index].advisory(advisory))
    return [
        None
        if index in crossed
        else LexedSource(lexed.text[start:end], tuple(entry), tuple(qualifiers), tuple(warnings))
        for index, ((start, end), entry, qualifiers, warnings) in enumerate(
            zip(ranges, tokens, spaced, tabs)
        )
    ]


def replayed_parse(start: str, text: str, parse: Callable[[str], Tree]) -> Tree:
    """Return *parse*'s tree of *text* from grammar start *start*, reusing a replayed reading.

    Within :func:`lex_replay` of *text*, only the first parse from *start* runs; a later one
    feeds the active sinks as that parse's lexing did and returns, or raises, what it found.
    *parse* runs the parser over *text*.
    """
    replay = _LEX_REPLAY.get()
    if replay is None or replay.text != text:
        return parse(text)
    found = replay.parses.get(start)
    if found is None:
        try:
            found = parse(text)
        except UnexpectedToken as exc:
            found = replay.parses[start] = exc
            raise
        replay.parses[start] = found
        return found
    _feed_advisories(replay)
    _deposit(_TOKEN_SINK, replay.tokens)
    if isinstance(found, UnexpectedToken):
        raise found
    return found


def _feed_advisories(lexed: LexedSource) -> None:
    """Deposit the advisories of *lexed* to the active sinks, whether or not the pass failed."""
    _deposit(_SPACED_QUALIFIER_SINK, lexed.spaced_qualifiers)
    _deposit(_TAB_WARNING_SINK, lexed.tab_warnings)


def _remap(tokens: Iterator[Token]) -> Iterator[Token]:
    """Remap lowercase keyword token types to the uppercase names the grammar expects."""
    for tok in tokens:
        mapped = GRAMMAR_TOKEN_REMAP.get(tok.type)
        if mapped is not None:
            tok = Token(
                mapped,
                str(tok),
                start_pos=tok.start_pos,
                line=tok.line,
                column=tok.column,
                end_line=tok.end_line,
                end_column=tok.end_column,
                end_pos=tok.end_pos,
            )
        yield tok


_ITEM_START_TYPES = frozenset({"_NEWLINE", "_INDENT", "_DEDENT", "SEMICOLON"})

# The three header keywords whose clause a hiding promotion can terminate.
_HEADER_TYPES = frozenset({IMPORT, USE, EXPORT})


def _retype(tok: Token, new_type: str) -> Token:
    """Return a copy of *tok* with a new token type, preserving value and span."""
    return Token(
        new_type,
        str(tok),
        start_pos=tok.start_pos,
        line=tok.line,
        column=tok.column,
        end_line=tok.end_line,
        end_column=tok.end_column,
        end_pos=tok.end_pos,
    )


def _scope_path_end(tokens: list[Token], index: int) -> int | None:
    """Return the index after a NAME (DCOLON NAME)* scope path, if present."""
    if index >= len(tokens) or tokens[index].type != NAME:
        return None
    index += 1
    while index < len(tokens) and tokens[index].type == DCOLON:
        if index + 1 >= len(tokens) or tokens[index + 1].type != NAME:
            return None
        index += 2
    return index


def _is_scope_path(tokens: list[Token], index: int) -> bool:
    """Whether ``tokens[index:]`` begins with a complete scope path."""
    return _scope_path_end(tokens, index) is not None


def _is_scope_closer(tokens: list[Token], index: int) -> bool:
    """Whether ``tokens[index]`` is followed by a complete, line-ending scope path.

    An indented region body dedents before its closer, so a ``_DEDENT`` ends
    the path exactly as a newline does.
    """
    path_end = _scope_path_end(tokens, index + 1)
    return path_end is not None and (
        path_end == len(tokens) or tokens[path_end].type in {"_NEWLINE", "SEMICOLON", "_DEDENT"}
    )


def _is_as(token: Token) -> bool:
    """Whether *token* is ``as`` before or after parser keyword remapping."""
    return scanner_token_type(token.type) == KW_AS


# Token types that can spell one selected member of a ``use`` header.
_MEMBER_TYPES = frozenset({NAME, OP_NAME})
_USE_HEADER_END_TYPES = _MEMBER_TYPES | {STAR, "RBRACE"}


def _is_use_declaration(tokens: list[Token], index: int) -> bool:
    """Whether an item-start ``use`` has a declaration-shaped token header."""

    end = index + 1
    while end < len(tokens) and tokens[end].type not in _ITEM_START_TYPES:
        end += 1
    header = tokens[index + 1 : end]
    if not header:
        return False
    if header[0].type == SLASH and (
        len(header) < 2 or header[1].type != NAME or header[0].end_pos != header[1].start_pos
    ):
        return False
    if header[0].type == SLASH and not any(
        token.type == DCOLON or _is_as(token) for token in header
    ):
        return False
    # ``hiding`` atoms and ``as`` targets both spell a member name, which the
    # grammar admits as an ordinary name or an operator.
    for hiding_index, token in enumerate(header):
        if not (
            token.type == NAME
            and str(token) == "hiding"
            and hiding_index > 0
            and header[hiding_index - 1].type in {STAR, "RBRACE"}
        ):
            continue
        if hiding_index + 1 >= len(header) or header[hiding_index + 1].type not in _MEMBER_TYPES:
            return False
    if any(
        _is_as(token)
        and (position + 1 >= len(header) or header[position + 1].type not in _MEMBER_TYPES)
        for position, token in enumerate(header)
    ):
        return False
    for position in range(index + 1, end):
        if tokens[position].type != DCOLON:
            continue
        if (
            position + 1 >= end
            or tokens[position].end_pos != tokens[position + 1].start_pos
            or (position > index + 1 and tokens[position - 1].end_pos != tokens[position].start_pos)
        ):
            return False
    current_module = header[0].type == DCOLON
    if current_module and sum(token.type == DCOLON for token in header) == 1:
        return any(_is_as(token) for token in header)
    return header[-1].type in _USE_HEADER_END_TYPES


def _promote_soft_keywords(tokens: list[Token]) -> list[Token]:
    """Contextually promote soft keywords in the post-layout token stream.

    Rules:
    - 'import' → IMPORT, 'use' → USE, and 'export' → EXPORT at item-start.
    - 'scope' → SCOPE at item-start before a scope path.
    - 'end' → END only for a complete closer at its region's layout level.
    - an operator word → its upper-cased type in operator position only.
    """
    result: list[Token] = []
    scope_layouts: list[int] = []
    layout_depth = 0
    prev_type: str | None = None  # None means start-of-stream

    for index, tok in enumerate(tokens):
        tt = tok.type
        tv = str(tok)

        if tt == "_INDENT":
            layout_depth += 1
        elif tt == "_DEDENT":
            layout_depth -= 1

        if tt == NAME:
            at_item_start = prev_type is None or prev_type in _ITEM_START_TYPES
            if tv == KW_IMPORT and at_item_start:
                tok = _retype(tok, IMPORT)
            elif tv == KW_USE and at_item_start and _is_use_declaration(tokens, index):
                tok = _retype(tok, USE)
            elif tv == KW_EXPORT and at_item_start:
                tok = _retype(tok, EXPORT)
            elif tv == KW_SCOPE and at_item_start and _is_scope_path(tokens, index + 1):
                tok = _retype(tok, SCOPE)
                scope_layouts.append(layout_depth)
            elif (
                tv == KW_END
                and at_item_start
                and scope_layouts
                and layout_depth == scope_layouts[-1]
                and _is_scope_closer(tokens, index)
            ):
                tok = _retype(tok, END)
                scope_layouts.pop()
            elif tv in OPERATOR_SOFT_KEYWORDS and promotes_as_operator(
                tv, prev_type, tokens[index + 1].type if index + 1 < len(tokens) else None
            ):
                tok = _retype(tok, tv.upper())

        result.append(tok)
        prev_type = tok.type

    return result


def _promote_hiding(tokens: list[Token]) -> list[Token]:
    """Promote only a header's hiding-clause delimiter, not path atoms named ``hiding``."""
    result: list[Token] = []
    header: str | None = None
    hiding_promoted = False
    brace_depth = 0
    saw_dcolon = False
    saw_alias = False
    for tok in tokens:
        if tok.type in _HEADER_TYPES:
            header = tok.type
            hiding_promoted = False
            brace_depth = 0
            saw_dcolon = False
            saw_alias = False
        elif tok.type in {"_NEWLINE", "_INDENT", "_DEDENT", "SEMICOLON"}:
            header = None
        elif tok.type == LBRACE:
            brace_depth += 1
        elif tok.type == "RBRACE":
            brace_depth -= 1
        elif header is not None and tok.type == DCOLON:
            saw_dcolon = True
        elif header is not None and _is_as(tok):
            saw_alias = True
        elif (
            header is not None
            and not hiding_promoted
            and brace_depth == 0
            and tok.type == NAME
            and str(tok) == KW_HIDING
            and result
            and (
                result[-1].type in {STAR, WILDCARD, "RBRACE"}
                or (header in {IMPORT, EXPORT} and result[-1].type == MODPATH)
                or (
                    header in {IMPORT, USE}
                    and result[-1].type in {NAME, OP_NAME}
                    and (saw_dcolon or saw_alias)
                )
            )
        ):
            tok = _retype(tok, HIDING)
            hiding_promoted = True
        result.append(tok)
    return result


def _merge_modpath(tokens: list[Token]) -> list[Token]:
    """Merge module-header paths into single MODPATH tokens.

    Pattern: immediately following an IMPORT, USE, or EXPORT token, consume
    NAME (SLASH NAME)* into a single MODPATH token whose value is the slash
    path (e.g. "foo/bar", "utils"). Every pair in the run is adjacent in the
    source, the rule module qualifiers obey: ``a/b`` is a path, ``a / b`` is
    division, so a spaced header separator ends the path and fails to parse.

    This keeps slash-separated module headers distinct from division in the
    expression grammar. By merging the path in the lexer, the grammar sees a
    single MODPATH token rather than the raw NAME SLASH ... sequence.
    """
    result: list[Token] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        has_plain_path = i + 1 < n and tokens[i + 1].type == NAME
        has_anchored_use_path = (
            tok.type == USE
            and i + 2 < n
            and tokens[i + 1].type == SLASH
            and tokens[i + 2].type == NAME
            and tokens[i + 1].end_pos == tokens[i + 2].start_pos
        )
        if tok.type in (IMPORT, USE, EXPORT) and (has_plain_path or has_anchored_use_path):
            result.append(tok)
            i += 1
            anchored = has_anchored_use_path
            anchor = tokens[i] if anchored else None
            if anchored:
                i += 1
            # Absorb NAME (SLASH NAME)*. Module path segments may begin with
            # either lowercase or uppercase letters.
            j = i
            last_seg = tokens[j]
            seg_parts: list[str] = [str(last_seg)]
            j += 1
            while (
                j + 1 < n
                and tokens[j].type == SLASH
                and tokens[j + 1].type == NAME
                and last_seg.end_pos == tokens[j].start_pos
                and tokens[j].end_pos == tokens[j + 1].start_pos
            ):
                last_seg = tokens[j + 1]
                seg_parts.append(str(last_seg))
                j += 2
            modpath_value = ("/" if anchored else "") + "/".join(seg_parts)
            first_tok = anchor if anchor is not None else tokens[i]
            last_tok = tokens[j - 1]
            merged = Token(
                MODPATH,
                modpath_value,
                start_pos=first_tok.start_pos,
                line=first_tok.line,
                column=first_tok.column,
                end_line=last_tok.end_line,
                end_column=last_tok.end_column,
                end_pos=last_tok.end_pos,
            )
            result.append(merged)
            # A byte-adjacent ``/*`` is the wildcard tail. The raw scanner makes
            # it an OP_NAME; retype it as the grammar's single WILDCARD token.
            # Whitespace anywhere in the tail leaves the operator alone, so the
            # header fails to parse — the same adjacency rule module qualifiers
            # obey.
            i = j
            if (
                i < n
                and tokens[i].type == OP_NAME
                and str(tokens[i]) == "/*"
                and tokens[i].start_pos == merged.end_pos
            ):
                tail = tokens[i]
                result.append(
                    Token(
                        WILDCARD,
                        "/*",
                        start_pos=tail.start_pos,
                        line=tail.line,
                        column=tail.column,
                        end_line=tail.end_line,
                        end_column=tail.end_column,
                        end_pos=tail.end_pos,
                    )
                )
                i += 1
            continue
        result.append(tok)
        i += 1
    return result


def _member_reference(
    tokens: list[Token], dcolon_index: int, source: str
) -> tuple[str, str, bool] | None:
    """Describe the reference following a ``::`` as ``(member, text, type_qualified)``.

    Consumes a ``NAME``, an optional balanced ``[...]`` type-argument group, and
    an optional ``:: NAME`` variant tail, then slices the source across the run.
    Returns ``None`` when no name follows the ``::`` and there is nothing to
    quote back to the author.
    """
    n = len(tokens)
    index = dcolon_index + 1
    if index >= n or tokens[index].type != NAME:
        return None
    member = str(tokens[index])
    start = tokens[index].start_pos
    end = tokens[index].end_pos
    index += 1
    if index < n and tokens[index].type == LSQB:
        depth = 0
        while index < n:
            if tokens[index].type == LSQB:
                depth += 1
            elif tokens[index].type == RSQB:
                depth -= 1
                if depth == 0:
                    end = tokens[index].end_pos
                    index += 1
                    break
            index += 1
    type_qualified = (
        index + 1 < n and tokens[index].type == DCOLON and tokens[index + 1].type == NAME
    )
    if type_qualified:
        end = tokens[index + 1].end_pos
    return member, source[start:end], type_qualified


def _record_spaced_qualifier(
    tokens: list[Token],
    dcolon_index: int,
    run_start: Token,
    segments: list[str],
    *,
    source: str,
    seen: set[int],
) -> None:
    """Deposit one spaced-qualifier advisory into the ambient sink, if any is active.

    The first run recorded for a given ``::`` wins.  ``_merge_modqual`` scans
    left to right, so that is the longest run reaching the ``::`` — the whole
    route rather than one of its suffixes.
    """
    sink = _SPACED_QUALIFIER_SINK.get()
    dcolon_span = token_span(tokens[dcolon_index])
    dcolon_offset = dcolon_span.start_offset
    if sink is None or dcolon_offset in seen:
        return
    reference = _member_reference(tokens, dcolon_index, source)
    if reference is None:
        return
    member, member_text, type_qualified = reference
    seen.add(dcolon_offset)
    sink.append(
        SpacedQualifier(
            segments=tuple(segments),
            anchored=run_start.type == SLASH,
            run_start_offset=token_span(run_start).start_offset,
            dcolon_span=dcolon_span,
            member=member,
            member_text=member_text,
            type_qualified=type_qualified,
        )
    )


def _merge_modqual(tokens: list[Token], source: str) -> list[Token]:
    """Merge byte-adjacent slash-qualified prefixes into ``MODQUAL`` tokens.

    A qualifier is ``[SLASH] NAME (SLASH NAME)* DCOLON`` with every pair in
    that run adjacent in the source. The token value retains its optional
    leading slash so the AST builder can distinguish anchored references.
    ``NAME DCOLON LSQB`` remains unmerged for typed calls.

    A run whose only defect is whitespace before the ``::`` emits its tokens
    unchanged — the grammar must not see a qualifier there — but is recorded as
    a :class:`~agnette.agl.syntax.advisories.SpacedQualifier` advisory so a later
    pass can explain the mis-parse.
    """
    result: list[Token] = []
    seen_dcolons: set[int] = set()
    i = 0
    n = len(tokens)
    while i < n:
        start = tokens[i]
        name_index = i
        if start.type == SLASH:
            name_index += 1
            if (
                name_index >= n
                or tokens[name_index].type != NAME
                or start.end_pos != tokens[name_index].start_pos
            ):
                result.append(start)
                i += 1
                continue
        elif start.type != NAME:
            result.append(start)
            i += 1
            continue

        last_name = tokens[name_index]
        segments = [str(last_name)]
        j = name_index + 1
        while (
            j + 1 < n
            and tokens[j].type == SLASH
            and tokens[j + 1].type == NAME
            and last_name.end_pos == tokens[j].start_pos
            and tokens[j].end_pos == tokens[j + 1].start_pos
        ):
            last_name = tokens[j + 1]
            segments.append(str(last_name))
            j += 2

        if j < n and tokens[j].type == DCOLON and last_name.end_pos != tokens[j].start_pos:
            _record_spaced_qualifier(tokens, j, start, segments, source=source, seen=seen_dcolons)

        if (
            j < n
            and tokens[j].type == DCOLON
            and last_name.end_pos == tokens[j].start_pos
            and (j + 1 >= n or tokens[j + 1].type != LSQB)
        ):
            qualifier_value = "/".join(segments)
            if start.type == SLASH:
                qualifier_value = "/" + qualifier_value
            last_tok = tokens[j]
            result.append(
                Token(
                    MODQUAL,
                    qualifier_value,
                    start_pos=start.start_pos,
                    line=start.line,
                    column=start.column,
                    end_line=last_tok.end_line,
                    end_column=last_tok.end_column,
                    end_pos=last_tok.end_pos,
                )
            )
            i = j + 1
            continue

        result.append(start)
        i += 1
    return result


# A merged qualifier is not itself an expression, but a following slash still
# clings to a path operand that failed to acquire its final name.


def _reaches_a_spaced_dcolon(tokens: list[Token], slash_index: int) -> bool:
    """Whether a slash heads a tight segment run ending at a whitespace-split ``::``.

    Such a run is a qualifier whose only defect is the gap before its ``::``;
    :func:`_merge_modqual` has already recorded it as a
    :class:`~agnette.agl.syntax.advisories.SpacedQualifier`, whose diagnostic names
    the tight spelling. Leaving it to that pass beats a blunt lexical error.
    """
    index = slash_index
    n = len(tokens)
    while (
        index + 1 < n
        and tokens[index].type == SLASH
        and tokens[index + 1].type == NAME
        and tokens[index].end_pos == tokens[index + 1].start_pos
    ):
        index += 2
        if index < n and tokens[index].type == DCOLON:
            return True
    return False


_BRACES = frozenset({LBRACE, CALL_LBRACE})
_OPENERS = frozenset({LPAR, LSQB, INDEX_LSQB, DO_LSQB, TYPEARG_LSQB}) | _BRACES
_CLOSERS = frozenset({RPAR, RSQB, RBRACE})
_ITEM_ENDS = frozenset({NEWLINE, INDENT, DEDENT, SEMICOLON, COMMA, EQ, ARROW, ASSIGN})
_CONTAINER_TYPES = frozenset({"array", "dict"})


def _type_positions(tokens: list[Token]) -> list[bool]:
    """Per index, whether the token there lies where a type is written (division is no reading).

    Entry ``len(tokens)`` is the state after the last token. Types follow an
    annotation colon (not a dict entry's), ``->`` and ``extends``, fill the
    brackets opened inside a type, and follow ``type X[...] =``. A bracket
    after ``array`` or ``dict``, one after ``::``, and one a ``::`` member
    follows (``Option[T]::None``) open type arguments; after any other name it
    may as well index a value, which this does not decide. Linear: one pass over the
    tokens, with bracket closes matched once up front (:func:`_bracket_closes`).
    """
    closes = _bracket_closes(tokens)
    in_type = False
    after_type_keyword = False
    # Per open bracket: the enclosing state, whether it holds braces, whether it
    # holds a type, and whether it interrupts a ``type`` header.
    frames: list[tuple[bool, bool, bool, bool]] = []
    positions: list[bool] = []
    for position, token in enumerate(tokens):
        positions.append(in_type)
        kind = scanner_token_type(token.type)
        if kind in _OPENERS:
            previous = tokens[position - 1] if position else None
            container = (
                kind == LSQB
                and previous is not None
                and (
                    (previous.type == NAME and str(previous).lower() in _CONTAINER_TYPES)
                    or previous.type == DCOLON
                    or (
                        previous.type == NAME
                        and previous.end_pos == token.start_pos
                        and _precedes_type_member(tokens, closes, position)
                    )
                )
            )
            frames.append((in_type, kind in _BRACES, in_type or container, after_type_keyword))
            in_type = in_type or container
            after_type_keyword = False
        elif kind in _CLOSERS and frames:
            in_type, _, _, after_type_keyword = frames.pop()
        elif kind == COLON:
            in_type = not (frames and frames[-1][1])
        elif kind in (THIN_ARROW, KW_EXTENDS):
            in_type = True
        elif kind == EQ and after_type_keyword:
            in_type = True
            after_type_keyword = False
        elif kind in _ITEM_ENDS:
            in_type = bool(frames and frames[-1][2])
            after_type_keyword = False
        elif kind == KW_TYPE:
            after_type_keyword = True
    positions.append(in_type)
    return positions


def _tight_route(tokens: list[Token], index: int) -> tuple[int, int] | None:
    """The bounds of the module route through the slash at *index*, if it lies in one.

    A route is two or more names joined by byte-adjacent slashes, a leading
    slash anchoring it. A slash joining a name to anything else (``a/b/2``,
    ``2/a/b``) is division's, not part of the route.
    """

    def joined(position: int) -> bool:
        return tokens[position].end_pos == tokens[position + 1].start_pos

    def name_at(position: int) -> bool:
        return 0 <= position < len(tokens) and tokens[position].type == NAME

    first = index
    while name_at(first - 1) and joined(first - 1):
        first -= 1
        if first > 0 and tokens[first - 1].type == SLASH and joined(first - 1):
            first -= 1
    if tokens[first].type == SLASH and first > 0 and joined(first - 1):
        first += 1  # the slash divides the operand before it
    last = index
    while name_at(last + 1) and joined(last):
        last += 1
        if (
            name_at(last + 2)
            and tokens[last + 1].type == SLASH
            and joined(last)
            and joined(last + 1)
        ):
            last += 1
    if first > last:
        return None
    names = sum(tok.type == NAME for tok in tokens[first : last + 1])
    return (first, last) if names >= 2 else None


def _clinging_slash_error(
    tokens: list[Token], index: int, type_positions: Callable[[], list[bool]]
) -> LexError:
    """Explain a clinging slash; a run of names joined by slashes is a module route.

    *type_positions* yields :func:`_type_positions` of *tokens* (computed once by the caller).
    """
    span = token_span(tokens[index])
    bounds = _tight_route(tokens, index)
    if bounds is None:
        return LexError(
            "'/' is a module path separator here; write ' / ' with spaces to divide", span=span
        )
    first, last = bounds
    route = "".join(str(tok) for tok in tokens[first : last + 1])
    return ModuleRouteError(route, divides=not type_positions()[first], span=span)


def _reject_clinging_slash(
    tokens: list[Token], *, errors: list[LexError] | None = None
) -> list[Token]:
    """Reject a division slash that clings to an operand on either side.

    Division is written with whitespace on both sides, matching ``+``, ``-``
    and ``*``, which need it because they are identifier characters. Unspaced,
    ``/`` separates module-path segments. Every slash that forms a path has
    already been merged away by this point, so a slash still touching an
    operand is a path that went nowhere, and reporting it here beats letting it
    reach the grammar as arithmetic (``a/ b``) or a stray anchor (``a /b``).
    """
    type_positions = cache(lambda: _type_positions(tokens))
    for index, tok in enumerate(tokens):
        if tok.type != SLASH or _reaches_a_spaced_dcolon(tokens, index):
            continue
        prev_tok = tokens[index - 1] if index > 0 else None
        next_tok = tokens[index + 1] if index + 1 < len(tokens) else None
        tight_left = (
            prev_tok is not None
            and scanner_token_type(prev_tok.type) in SLASH_LEFT_OPERAND_TYPES
            and prev_tok.end_pos == tok.start_pos
        )
        tight_right = (
            next_tok is not None
            and scanner_token_type(next_tok.type) in INFIX_OPERAND_START_TYPES
            and tok.end_pos == next_tok.start_pos
        )
        if tight_left or tight_right:
            error = _clinging_slash_error(tokens, index, type_positions)
            if errors is None:
                raise error
            # Preserve the slash and its neighbors; flag only its own span.
            errors.append(error)
    return tokens


def apply_module_passes(
    tokens: list[Token], source: str, *, errors: list[LexError] | None = None
) -> list[Token]:
    """Promote header words and merge paths; optionally record invalid slash spans."""
    promoted = _promote_soft_keywords(tokens)
    merged = _merge_modqual(_promote_hiding(_merge_modpath(promoted)), source)
    return _reject_clinging_slash(merged, errors=errors)


def unclosed_scope_path(source: str) -> str:
    """Return the innermost promoted scope path still open at end of *source*.

    *source* must end inside a scope region.
    """
    tokens = apply_module_passes(list(layout(_Scanner(source).scan())), source)
    open_paths: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type not in (SCOPE, END):
            index += 1
            continue
        names: list[str] = []
        index += 1
        while index < len(tokens) and tokens[index].type in (MODQUAL, NAME):
            names.append(str(tokens[index]))
            index += 1
        if token.type == SCOPE:
            open_paths.append("::".join(names))
        else:
            open_paths.pop()
    return open_paths[-1]


def _remap_adjacent_brackets(tokens: list[Token]) -> list[Token]:
    """Turn adjacent expression brackets/braces into parser-only suffix tokens.

    A ``[`` immediately following the ``do`` keyword opens a loop bound; it is
    retagged ``DO_LSQB`` so the LALR grammar can tell ``do[expr]`` (the bound)
    apart from a ``do`` body that starts with an array literal — without that
    distinct terminal the optional ``loop_bound`` and an array-literal body both
    begin with ``LSQB``, which is the conflict this resolves.  Whitespace
    between ``do`` and ``[`` is allowed (``do [n]`` works); a newline is not,
    because layout inserts a token between them.
    """
    result: list[Token] = []
    previous: Token | None = None
    for tok in tokens:
        if tok.type == LSQB and previous is not None and previous.type == "DO":
            tok = _retype(tok, DO_LSQB)
        elif (
            tok.type == LSQB
            and previous is not None
            and scanner_token_type(previous.type) in EXPRESSION_END_TYPES
            and previous.end_pos == tok.start_pos
        ):
            tok = _retype(tok, INDEX_LSQB)
        elif (
            tok.type == LBRACE
            and previous is not None
            and scanner_token_type(previous.type) in EXPRESSION_END_TYPES
            and previous.end_pos == tok.start_pos
        ):
            tok = _retype(tok, CALL_LBRACE)
        result.append(tok)
        previous = tok
    return result


_OPEN_TYPES = frozenset({LSQB, INDEX_LSQB, TYPEARG_LSQB, DO_LSQB})


def _bracket_closes(tokens: list[Token]) -> dict[int, int]:
    """Map each opening ``[`` index to the index of the ``]`` closing it; unclosed ones are absent.

    Only square brackets pair, as in a type application's argument group; other
    delimiters are ignored. One stack pass.
    """
    closes: dict[int, int] = {}
    opened: list[int] = []
    for index, token in enumerate(tokens):
        if token.type in _OPEN_TYPES:
            opened.append(index)
        elif token.type == RSQB and opened:
            closes[opened.pop()] = index
    return closes


def _precedes_type_member(tokens: list[Token], closes: dict[int, int], index: int) -> bool:
    """Whether the ``[`` at *index* closes before ``::`` and a member that is no ``[``.

    Such a bracket group is a type application (``Type[T]::Ctor``); a following
    ``::[`` instead applies a value (``xs[i]::[T]``). *closes* is
    :func:`_bracket_closes` of *tokens*.
    """
    close = closes.get(index)
    if close is None:
        return False
    n = len(tokens)
    after = tokens[close + 1].type if close + 1 < n else None
    after_next = tokens[close + 2].type if close + 2 < n else None
    return after == DCOLON and after_next != LSQB


def _mark_typearg_lsqb(tokens: list[Token]) -> list[Token]:
    """Retag ``Type[T]::Ctor`` opening brackets for the parser.

    Only the outer ``[`` immediately adjacent to a preceding ``NAME`` is
    retagged, and only when :func:`_precedes_type_member` holds.  The guard
    preserves value-position type application such as ``xs[i]::[T]``.
    """
    closes = _bracket_closes(tokens)
    result = list(tokens)
    for i, tok in enumerate(result):
        if (
            tok.type == INDEX_LSQB
            and i > 0
            and result[i - 1].type == NAME
            and _precedes_type_member(result, closes, i)
        ):
            result[i] = _retype(tok, TYPEARG_LSQB)
    return result


class AglLexer(Lexer):
    """Custom Lark lexer for AgL.

    Accepted by the Lark parser via ``lexer=AglLexer``; its configuration is
    received but not used (the grammar's terminal regex patterns are not needed
    because we generate all tokens ourselves).

    ``__future_interface__ = 1`` selects Lark's ``lex(lexer_state,
    parser_state)`` calling convention, matching this lexer's signature.
    """

    __future_interface__ = 1

    def __init__(self, _lexer_conf: object) -> None:
        # The scanner handles all tokenization.
        pass

    def lex(self, lexer_state: LexerState, parser_state: object) -> Iterator[Token]:
        """Tokenize ``lexer_state.text`` and yield Lark tokens.

        Keyword token types are remapped from lowercase (scanner convention)
        to the uppercase names expected by the Lark grammar.
        """
        text = lexer_state.text.text
        replay = _LEX_REPLAY.get()
        lexed = replay if replay is not None and replay.text == text else lex_source(text)
        # Advisories reach the sinks even when the pass failed or the parse later does.
        _feed_advisories(lexed)
        if lexed.failure is not None:
            raise lexed.failure.renewed()
        _deposit(_TOKEN_SINK, lexed.tokens)
        return iter(lexed.tokens)


def load_grammar() -> str:
    """Load the canonical grammar file via importlib.resources (package-anchored)."""
    return (
        importlib.resources.files("agnette.agl")
        .joinpath("grammar/agl.lark")
        .read_text(encoding="utf-8")
    )


def build_parser(start: str = "start") -> Lark:
    """Build a Lark instance over the canonical grammar rooted at *start*.

    The one place the grammar file and the Lark options are named, so every
    parser -- the program parser and each alternate-start parser -- is built
    from identical settings and shares one on-disk cache shape.
    """
    return Lark(
        load_grammar(),
        parser="lalr",
        lexer=AglLexer,
        propagate_positions=True,
        maybe_placeholders=True,
        start=start,
        cache=True,
    )
