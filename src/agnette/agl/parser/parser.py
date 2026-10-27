"""AgL parser: build a Lark LALR instance and produce ``syntax.Program``.

The module-level ``_PARSER`` is built once at import time from
``grammar/agl.lark`` (loaded via ``importlib.resources`` anchored on
``agl``).  ``cache=True`` persists the compiled LALR tables to a
grammar-hash-keyed temp file so repeated process starts (every ``agm exec`` /
``agm repl`` invocation) reload them instead of rebuilding from scratch.

``parse_program(text)`` is the public entry point. It feeds the source
string to ``_PARSER`` and passes the resulting Lark tree to ``AstBuilder``,
which groups every chain of builtin operators; a chain applying a user
operator stays a ``RawInfixChain`` for scope to group. Lark's ``UnexpectedToken``
and ``LexError``s are wrapped into ``AglSyntaxError``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace as dc_replace
from typing import NoReturn, cast

from lark import Lark, Tree
from lark.exceptions import UnexpectedToken, VisitError
from lark.lexer import Token

import agnette.agl.syntax as syntax
from agnette.agl.lexer import (
    LexedSource,
    lex_replay,
    lexed_slice,
    lexed_source,
    replayed_parse,
    tab_warning_labels,
    token_collector,
)
from agnette.agl.lexer.errors import (
    IncompleteInputError,
    LexError,
    UnterminatedTripleQuotedStringError,
)
from agnette.agl.lexer.lexer import build_parser
from agnette.agl.lexer.positions import Rebase, token_span
from agnette.agl.lexer.tokens import VERBATIM_END, VERBATIM_START
from agnette.agl.parser.errors import AglSyntaxError, syntax_error_from_lark
from agnette.agl.parser.transform import AstBuilder
from agnette.agl.recursion import frontend_recursion_boundary
from agnette.agl.syntax.spans import SourceId


def _reraise_stamped(err: AglSyntaxError, source: SourceId | None) -> NoReturn:
    """Re-raise *err*, stamping its span with *source* when one is supplied."""
    if source is not None:
        raise AglSyntaxError(str(err), span=dc_replace(err.span, source=source)) from err
    raise err


# Module-level parser instance — built once, reused for every parse call.
_PROGRAM_START = "start"
_TYPE_START = "type_entry"
_PARSER: Lark = build_parser(_PROGRAM_START)

_TYPED_CALL_WITHOUT_CALL_RE = re.compile(r"::\s*\[.*\]\s*$", re.DOTALL)

# A second Lark instance rooted at the ``type_entry`` grammar rule, built lazily.
# Used only by :func:`parse_type_entry_seeded` for the REPL's bare-type-entry fallback;
# keeping a separate start symbol avoids perturbing the program parser's start rule.
_TYPE_PARSER: Lark | None = None


def _type_parser() -> Lark:
    """Return the lazily-built ``type_entry``-rooted Lark instance."""
    global _TYPE_PARSER
    if _TYPE_PARSER is None:
        _TYPE_PARSER = build_parser(_TYPE_START)
    return _TYPE_PARSER


def has_unterminated_triple_quoted_string(source: str | LexedSource) -> bool:
    """Return ``True`` when lexing *source* ends inside a triple-quoted string."""
    return isinstance(lexed_source(source).failure, UnterminatedTripleQuotedStringError)


def has_open_verbatim_block(source: str | LexedSource) -> bool:
    """Return ``True`` when a `$`-literal block payload is still open.

    An unclosed block payload is not a parse failure — the lexer happily ends
    it at end of input — so :func:`is_incomplete_source` cannot see it. A REPL
    still needs to keep prompting, which is what this answers. Only the *last*
    payload can reach the buffer end, and it is a block (rather than an inline
    payload that is already complete) exactly when its header line has no text
    after the opener.

    A bare `$` header with nothing typed after it yet (e.g. ``ask $``) raises
    an ``IncompleteInputError`` instead of closing silently;
    :func:`is_incomplete_source` already reports that as incomplete, so any
    lex error here is simply not an open block.
    """
    lexed = lexed_source(source)
    text = lexed.text
    if lexed.failure is not None:
        return False
    payload_starts: list[int] = []
    for token in lexed.tokens:
        if token.type == VERBATIM_START:
            payload_starts.append(token_span(token).end_offset)
        elif token.type == VERBATIM_END and token.end_pos == len(text) and payload_starts:
            line_end = text.find("\n", payload_starts[-1])
            header_tail = text[payload_starts[-1] : len(text) if line_end < 0 else line_end]
            return header_tail.strip() == ""
    return False


def _lalr_parse(parser: Lark, start: str, text: str) -> Tree:
    """Parse *text* from grammar start *start* with *parser*, raising Lark's own failures.

    Within a replay of *text* the first parse is the only one that runs
    (:func:`~agnette.agl.lexer.replayed_parse`).
    """
    return replayed_parse(start, text, parser.parse)


def _parse_tree(parser: Lark, start: str, text: str, *, source: SourceId | None) -> Tree:
    """Parse *text* with *parser*, mapping any lex/parse error to ``AglSyntaxError``.

    Shared by the program parser and the ``type_expr`` parser so the error
    wrapping exists in one place. Returns the raw Lark tree; the caller
    transforms it.

    Installs :func:`~agnette.agl.lexer.token_collector` around the parse so a
    Lark-derived error can be diagnosed against the parse's own materialized
    token pass (e.g. the `$`-spacing hint) without re-lexing *text*, and labels
    the parse's TAB advisories with *source*.
    """
    with (
        token_collector() as tokens,
        tab_warning_labels(None if source is None else source.label),
    ):
        try:
            return _lalr_parse(parser, start, text)
        except LexError as exc:
            _reraise_stamped(AglSyntaxError(str(exc), span=exc.span), source)
        except UnexpectedToken as exc:
            _reraise_stamped(syntax_error_from_lark(exc, source_text=text, tokens=tokens), source)


def _transform_tree(
    tree: Tree,
    *,
    start_id: int,
    source: SourceId | None,
    allow_late_uses: bool = False,
) -> tuple[object, int]:
    """Transform a Lark tree via ``AstBuilder``, unwrapping ``VisitError``.

    Returns ``(result, next_node_id)`` where ``next_node_id`` is the first id NOT
    consumed by the builder's counter (the seed for the next incremental parse).
    """
    builder = AstBuilder(start_id=start_id, source=source, allow_late_uses=allow_late_uses)
    try:
        result = builder.transform(tree)
    except VisitError as exc:
        # Lark wraps transformer exceptions in VisitError.  A syntax error is
        # re-raised with the source stamped so it carries the module path;
        # stack exhaustion on deeply nested source is re-raised for the
        # frontend recursion boundary.
        if isinstance(exc.orig_exc, AglSyntaxError):
            _reraise_stamped(exc.orig_exc, source)
        raise exc.orig_exc from None
    return result, builder.next_node_id


def _parse_to_program(
    text: str,
    *,
    start_id: int,
    source: SourceId | None = None,
    allow_late_uses: bool = False,
) -> tuple[syntax.Program, int]:
    """Parse *text*, reporting the next unused node id.

    Shared body for :func:`parse_program`, :func:`parse_program_seeded` and
    :func:`parse_repl_transcript`. Node ids are assigned starting at
    *start_id*; the returned ``int`` is the first id NOT consumed (the seed for
    a subsequent incremental parse), read from the builder's counter rather
    than assuming the root holds the maximum.

    When *source* is supplied, every ``SourceSpan`` the builder constructs is
    stamped with that ``SourceId``; the same id is also stamped on any
    ``AglSyntaxError`` raised during parsing.
    """
    with frontend_recursion_boundary():
        tree = _parse_tree(_PARSER, _PROGRAM_START, text, source=source)
        result, next_id = _transform_tree(
            tree, start_id=start_id, source=source, allow_late_uses=allow_late_uses
        )
    return cast(syntax.Program, result), next_id


def is_incomplete_source(source: str | LexedSource) -> bool:
    """Return ``True`` when *source* parses as a *prefix* of a valid program.

    This is the structured signal a REPL needs to decide whether pressing Enter
    should submit the entry or insert a continuation newline.  It distinguishes
    "the user has not finished typing" from "the user made a real mistake".

    The classification reads the raw Lark failure rather than the lossy
    ``AglSyntaxError`` message, so it tracks the grammar exactly (this is the one
    place — alongside the rest of the parser package — permitted to import Lark):

    - A clean parse → complete, unless the source ends with a body-less
      ``record``/``exception`` declaration, which can still take an indented
      field block (a blank line submits it as declared).
    - ``UnexpectedToken`` at the **end of input** (token type ``$END``) → the
      parser ran out of tokens while still expecting more.  This covers every
      unterminated block header (``record R =``, ``enum E``, ``case x of``,
      ``try``, ``do agent``, ``if c =>``), a dangling binary operator
      (``1 +``), and an open ``let x =``.  All are treated as "needs more
      input".
    - ``UnexpectedToken`` on a real token where an ``_INDENT`` was expected (the
      user hit Enter right after a block-opening ``=>``/header but has not yet
      indented the suite body) → incomplete.
    - ``IncompleteInputError`` (an unterminated triple-quoted string, or an
      empty `$` verbatim literal running to end of input) → incomplete. Other
      lexical failures remain complete so the REPL submits and the user sees the
      genuine error instead of being trapped in a continuation prompt.
    - Any other failure (a wrong token mid-line such as ``let = 5``) → complete.
    """

    lexed = lexed_source(source)
    text = lexed.text
    try:
        with lex_replay(lexed):
            result = _ends_unfinished(_lalr_parse(_PARSER, _PROGRAM_START, text))
    except UnexpectedToken as exc:
        # The LALR parser reports a premature end of input as an unexpected
        # ``$END`` token (it never raises ``UnexpectedEOF``), so this single
        # branch classifies every unterminated block / dangling operator.
        token: Token = exc.token
        if token.type == "$END":
            typed_call_without_call = (
                exc.expected == {"LPAR"} and _TYPED_CALL_WITHOUT_CALL_RE.search(text) is not None
            )
            result = not typed_call_without_call
        else:
            result = "_INDENT" in exc.expected
    except LexError as exc:
        result = isinstance(exc, IncompleteInputError)

    return result


_BODYLESS_CAPABLE_DECLARATIONS = frozenset(
    {"record_def", "builtin_record_def", "exception_def", "builtin_exception_def"}
)


def _ends_unfinished(tree: Tree) -> bool:
    """Return ``True`` when the module's final child may still gain a body or an item.

    That is a body-less record/exception, or an inner attribute that nothing follows and
    only inner attributes precede.
    """
    module_block = cast(Tree, tree.children[0])
    last = module_block.children[-1] if module_block.children else None
    if not isinstance(last, Tree):
        return False
    if last.data == "inner_attribute":
        # After any other item it can only be an error; only a header may be unfinished.
        return all(
            isinstance(child, Tree) and child.data == "inner_attribute"
            for child in module_block.children
        )
    return last.data in _BODYLESS_CAPABLE_DECLARATIONS and not any(
        isinstance(child, Tree) and str(child.data).endswith("_body") for child in last.children
    )


def parse_program(
    text: str,
    *,
    start_id: int = 0,
    source: SourceId | None = None,
) -> syntax.Program:
    """Parse *text* as an AgL program and return a ``syntax.Program`` AST.

    Parameters
    ----------
    text:
        The source code to parse.
    start_id:
        The first ``node_id`` to assign (default ``0`` → unchanged behaviour).
        Used by incremental sessions to keep node ids globally unique across
        entries; prefer :func:`parse_program_seeded` there to also recover the
        next seed.
    source:
        Optional :class:`~agnette.agl.syntax.spans.SourceId` to stamp on every
        ``SourceSpan`` in the resulting AST.  When ``None`` (the default),
        spans carry ``UNKNOWN_SOURCE`` (label ``"<agl>"``).  Pass this from
        the module loader so that multi-file diagnostics identify the origin file.

    Returns
    -------
    syntax.Program
        The root AST node of the parsed program.

    Raises
    ------
    AglSyntaxError
        On any lex or parse error, with a :class:`~agnette.agl.syntax.spans.SourceSpan`
        carrying 1-based line/column information.  If *source* was supplied,
        the error span is stamped with that ``SourceId``.
    """
    program, _next_id = _parse_to_program(text, start_id=start_id, source=source)
    return program


def parse_repl_transcript(text: str, *, source: SourceId | None = None) -> syntax.Program:
    """Parse a saved REPL transcript for top-level entry boundary discovery.

    A syntax error carries *source*, when given.

    Header ordering is deferred because each top-level item is subsequently
    parsed as an independent REPL entry before it can be evaluated.
    """

    program, _next_id = _parse_to_program(text, start_id=0, source=source, allow_late_uses=True)
    return program


def parse_program_seeded(
    text: str,
    *,
    start_id: int,
    source: SourceId | None = None,
) -> tuple[syntax.Program, int]:
    """Parse *text* with node ids starting at *start_id* for incremental use.

    Like :func:`parse_program` but returns ``(program, next_start_id)`` where
    ``next_start_id`` is the first ``node_id`` NOT consumed by this parse — the
    seed to pass as *start_id* for the next entry so that node ids remain
    globally unique across all entries in a REPL session.

    Parameters
    ----------
    text:
        The source code to parse.
    start_id:
        The first ``node_id`` to assign.
    source:
        Optional :class:`~agnette.agl.syntax.spans.SourceId` stamped on every span.
        See :func:`parse_program` for details.

    Returns
    -------
    tuple[syntax.Program, int]
        The parsed program and the next unused ``node_id``.

    Raises
    ------
    AglSyntaxError
        On any lex or parse error.
    """
    return _parse_to_program(text, start_id=start_id, source=source)


def _rebased_tree(tree: Tree, rebase: Rebase) -> Tree:
    """Return a copy of *tree* with every position rebased."""
    children = [
        _rebased_tree(child, rebase)
        if isinstance(child, Tree)
        else rebase.token(child)
        if isinstance(child, Token)
        else child
        for child in tree.children
    ]
    copy = Tree(tree.data, children)
    meta = tree.meta
    if not meta.empty:
        _set_span(
            copy,
            (meta.start_pos, meta.line, meta.column),
            (meta.end_pos, meta.end_line, meta.end_column),
            rebase,
        )
    return copy


def _set_span(
    tree: Tree, start: tuple[int, int, int], end: tuple[int, int, int], rebase: Rebase
) -> None:
    """Give *tree* the span from *start* to *end*, each ``(offset, line, column)``, rebased."""
    meta = tree.meta
    meta.empty = False
    meta.start_pos = rebase.offset(start[0])
    meta.line, meta.column = rebase.line(start[1]), rebase.column(start[1], start[2])
    meta.end_pos = rebase.offset(end[0])
    meta.end_line, meta.end_column = rebase.line(end[1]), rebase.column(end[1], end[2])


def entry_slices(lexed: LexedSource, ranges: Sequence[tuple[int, int]]) -> list[LexedSource | None]:
    """Read each of *ranges* of ``lexed.text``, a module's top-level entries, off its program parse.

    The module items are *ranges* in order, the first also holding the module's leading
    inner attributes. Each slice is the pass over its entry's text with the parse of that
    text already made, so reading the entry scans and parses nothing. ``None`` for an entry
    that cannot be read off (:func:`~agnette.agl.lexer.lexed_slice`: *lexed* failed), or for all of them when
    *lexed* holds no program parse of as many items.
    """
    whole = lexed.parses.get(_PROGRAM_START)
    if not isinstance(whole, Tree):
        return [None] * len(ranges)
    children = [c for c in cast(Tree, whole.children[0]).children if not isinstance(c, Token)]
    attributes = [c for c in children if c.data == "inner_attribute"]
    items = [c for c in children if c.data != "inner_attribute"]
    if len(items) != len(ranges):
        return [None] * len(ranges)
    slices: list[LexedSource | None] = []
    for index, ((start, end), item) in enumerate(zip(ranges, items)):
        sliced = lexed_slice(lexed, start, end)
        if sliced is not None:
            rebase = Rebase(lexed.text, start)
            entry = [*attributes, item] if index == 0 else [item]
            block = Tree("module_block", [_rebased_tree(c, rebase) for c in entry])
            root = Tree("start", [block])
            first, last = entry[0].meta, entry[-1].meta
            for node in (block, root):
                _set_span(
                    node,
                    (first.start_pos, first.line, first.column),
                    (last.end_pos, last.end_line, last.end_column),
                    rebase,
                )
            sliced.parses[_PROGRAM_START] = root
        slices.append(sliced)
    return slices


def parse_type_entry_seeded(
    text: str,
    *,
    start_id: int,
    source: SourceId | None = None,
) -> tuple[tuple[syntax.Attribute, ...], syntax.TypeExpr, int]:
    """Parse *text* as a bare type entry: leading inner attributes, then one type expression.

    The REPL uses it to recognize a bare type entry (``int``, ``array[T]``, a
    declared record/enum name, ...) so it can echo the resolved type. It roots
    the parse at the ``type_entry`` rule, so any input that is not a type
    expression raises ``AglSyntaxError`` and the caller falls back to normal
    evaluation. Returns ``(attributes, type_expr, next_start_id)`` so the caller
    can keep assigning ids past the result.
    """
    with frontend_recursion_boundary():
        tree = _parse_tree(_type_parser(), _TYPE_START, text, source=source)
        result, next_id = _transform_tree(tree, start_id=start_id, source=source)
    attributes, type_expr = cast(tuple[tuple[syntax.Attribute, ...], syntax.TypeExpr], result)
    return attributes, type_expr, next_id
