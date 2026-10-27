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
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from typing import NoReturn, cast

from lark import Lark, Tree
from lark.exceptions import UnexpectedToken, VisitError
from lark.lexer import Token

import agnette.agl.syntax as syntax
from agnette.agl.lexer import (
    LexedSource,
    lex_replay,
    lexed_slices,
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
from agnette.agl.lexer.positions import LineIndex, Rebase, token_span
from agnette.agl.lexer.tokens import VERBATIM_END, VERBATIM_START
from agnette.agl.parser.errors import AglSyntaxError, syntax_error_from_lark
from agnette.agl.parser.transform import (
    MISPLACED_INNER_ATTRIBUTE_MESSAGE,
    AstBuilder,
    syntax_error_from_meta,
)
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
) -> tuple[syntax.Program, int]:
    """Parse *text*, reporting the next unused node id.

    Shared body for :func:`parse_program` and :func:`parse_program_seeded`. Node ids are
    assigned starting at *start_id*; the returned ``int`` is the first id NOT consumed (the seed for
    a subsequent incremental parse), read from the builder's counter rather
    than assuming the root holds the maximum.

    When *source* is supplied, every ``SourceSpan`` the builder constructs is
    stamped with that ``SourceId``; the same id is also stamped on any
    ``AglSyntaxError`` raised during parsing.
    """
    with frontend_recursion_boundary():
        tree = _parse_tree(_PARSER, _PROGRAM_START, text, source=source)
        result, next_id = _transform_tree(tree, start_id=start_id, source=source)
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


@dataclass(frozen=True, slots=True)
class ProgramTree:
    """A program's parse tree, not yet converted to an AST.

    A host that converts each top-level entry itself (the REPL, which assigns node ids
    and sources entry by entry) reads the entry boundaries off the tree and converts
    only the items it has not converted already.
    """

    tree: Tree
    text: str

    def children(self) -> list[Tree]:
        """The module's inner attributes and items, in order."""
        return [c for c in cast(Tree, self.tree.children[0]).children if not isinstance(c, Token)]

    def misplaced_attribute(self) -> Tree | None:
        """The first inner attribute after an item, which may only head the module."""
        seen_item = False
        for child in self.children():
            if child.data != "inner_attribute":
                seen_item = True
            elif seen_item:
                return child
        return None

    def entry_ranges(self) -> list[tuple[int, int]]:
        """The source ranges of the module's entries: one per item, or one for attributes alone.

        The module's leading inner attributes head the first entry.
        """
        children = self.children()
        attributes = [c.meta for c in children if c.data == "inner_attribute"]
        ranges = [
            (c.meta.start_pos, c.meta.end_pos) for c in children if c.data != "inner_attribute"
        ]
        if not ranges:
            ranges = [(min(a.start_pos for a in attributes), max(a.end_pos for a in attributes))]
        ranges[0] = (min([ranges[0][0], *(a.start_pos for a in attributes)]), ranges[0][1])
        return ranges

    def items_from(self, offset: int, *, start_id: int) -> tuple[syntax.Item, ...]:
        """Convert the items starting at or after *offset* to AST, with ids from *start_id*.

        Spans are the file's own. Header ordering is not enforced: the entry that runs an
        item judges it.
        """
        items = [
            c for c in self.children() if c.data != "inner_attribute" and c.meta.start_pos >= offset
        ]
        if not items:
            return ()
        root = _module_tree(items, Rebase(LineIndex(self.text), 0))
        with frontend_recursion_boundary():
            program, _ = _transform_tree(root, start_id=start_id, source=None, allow_late_uses=True)
        return cast(syntax.Program, program).body.items


def parse_program_tree(text: str, *, source: SourceId | None = None) -> ProgramTree:
    """Parse *text* as a program, leaving the tree unconverted.

    A syntax error carries *source*, when given. Within a :func:`~agnette.agl.lexer.lex_replay`
    of *text*, the tree is recorded on the lex for :func:`entry_slices`.
    """
    with frontend_recursion_boundary():
        program = ProgramTree(_parse_tree(_PARSER, _PROGRAM_START, text, source=source), text)
    misplaced = program.misplaced_attribute()
    if misplaced is not None:
        _reraise_stamped(
            syntax_error_from_meta(misplaced.meta, MISPLACED_INNER_ATTRIBUTE_MESSAGE), source
        )
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
    """Return a copy of *tree* with every position rebased.

    Walks iteratively: the tree of source nested too deep to convert is still copied, and
    its conversion reports the nesting.
    """
    root = Tree(tree.data, [])
    pending = [(tree, root)]
    while pending:
        original, copy = pending.pop()
        for child in original.children:
            if isinstance(child, Tree):
                copied = Tree(child.data, [])
                pending.append((child, copied))
                copy.children.append(copied)
            else:
                copy.children.append(rebase.token(child) if isinstance(child, Token) else child)
        meta = original.meta
        if not meta.empty:
            _set_span(
                copy,
                (meta.start_pos, meta.line, meta.column),
                (meta.end_pos, meta.end_line, meta.end_column),
                rebase,
            )
    return root


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


def _module_tree(children: Sequence[Tree], rebase: Rebase) -> Tree:
    """The program tree of the module whose top-level *children* are given, all rebased."""
    block = Tree("module_block", [_rebased_tree(c, rebase) for c in children])
    root = Tree("start", [block])
    first, last = children[0].meta, children[-1].meta
    for node in (block, root):
        _set_span(
            node,
            (first.start_pos, first.line, first.column),
            (last.end_pos, last.end_line, last.end_column),
            rebase,
        )
    return root


def entry_slices(lexed: LexedSource, ranges: Sequence[tuple[int, int]]) -> list[LexedSource | None]:
    """Read each of *ranges* of ``lexed.text``, a module's top-level entries, off its one pass.

    Each slice is the pass over its entry's text, read off the tokens of *lexed* without
    scanning (:func:`~agnette.agl.lexer.lexed_slices`). When *lexed* also records the
    module's program parse, a slice holding the module's top-level items whole also carries
    their parse, so reading it parses nothing; the module's leading inner attributes belong
    to the first entry. ``None`` for an entry that is not a pass of its own: all of them
    when *lexed* failed.
    """
    lines = LineIndex(lexed.text)
    slices = lexed_slices(lexed, ranges, lines)
    whole = lexed.parses.get(_PROGRAM_START)
    if not isinstance(whole, Tree):
        return slices
    entries: list[list[Tree]] = [[] for _ in ranges]
    starts = [start for start, _ in ranges]
    for child in ProgramTree(whole, lexed.text).children():
        begin, end = child.meta.start_pos, child.meta.end_pos
        first = max(bisect_right(starts, begin) - 1, 0)
        if starts[first] <= begin and end <= ranges[first][1]:
            entries[first].append(child)
            continue
        # An item crossing an entry's boundary leaves every entry it touches no parse of its own.
        for index in range(first, len(ranges)):
            if starts[index] < end and ranges[index][1] > begin:
                slices[index] = None
    for index, (sliced, (start, _), entry) in enumerate(zip(slices, ranges, entries)):
        if sliced is None or not entry:
            continue
        sliced.parses[_PROGRAM_START] = _module_tree(entry, Rebase(lines, start))
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
