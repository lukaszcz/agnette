"""Exact lexical highlighting and recovery-based REPL compatibility helpers.

The lexer owns recognition. Its diagnostic spans can overlap and even move
backwards in multiline templates; projection, not lexer mutation, makes a
source partition. Trivia is TEXT and offsets always index the original input.
"""

from __future__ import annotations

import heapq

from lark.lexer import Token

from agnette.agl.highlight.categories import Category, Span
from agnette.agl.keywords import KEYWORDS, OPERATOR_SOFT_KEYWORDS
from agnette.agl.lexer import (
    IncompleteInputError,
    _tokenize_recover,
    lex_open_quote_start,
    tokenize_recover,
)
from agnette.agl.lexer.tokens import SOFT_KEYWORD_TOKENS, STRING_TAIL, VERBATIM_TAIL
from agnette.agl.scope.symbols import BUILTIN_CALL_NAMES
from agnette.agl.syntax import BUILTIN_TYPE_NAMES
from agnette.agl.value_syntax.errors import ValueSyntaxError
from agnette.agl.value_syntax.lexical import decode_escape
from agnette.util.text import normalize_newlines

_LAYOUT_TOKENS = frozenset({"_NEWLINE", "_INDENT", "_DEDENT"})
_KEYWORD_TOKENS = KEYWORDS | SOFT_KEYWORD_TOKENS
_DECL_TYPE_KEYWORDS = frozenset({"record", "enum", "exception", "type"})
_DECL_CONSTRUCTOR_KEYWORDS = frozenset({"record", "exception"})
_CALL_LOOKAHEAD = frozenset({"LPAR", "DCOLON"})
_OPERATOR_TOKENS = frozenset(
    {
        "ARROW",
        "THIN_ARROW",
        "ASSIGN",
        "DCOLON",
        "AT",
        "EQ",
        "NEQ",
        "LE",
        "GE",
        "LT",
        "GT",
        "PLUS",
        "MINUS",
        "STAR",
        "SLASH",
        "PIPE",
        "EQ_EQ",
        "WILDCARD",
    }
)
_PUNCTUATION_TOKENS = frozenset(
    {
        "LPAR",
        "RPAR",
        "LSQB",
        "RSQB",
        "LBRACE",
        "RBRACE",
        "COLON",
        "COMMA",
        "DOT",
        "SEMICOLON",
    }
)


def _category_for(
    token_type: str,
    token_text: str,
    type_names: frozenset[str],
    constructor_names: frozenset[str],
    next_sig_type: str | None,
) -> Category:
    """Classify references; declaration and attribute sites override this result."""
    if token_type in _KEYWORD_TOKENS:
        if token_text in {"true", "false", "null"}:
            return Category.KEYWORD_CONSTANT
        if token_text in OPERATOR_SOFT_KEYWORDS:
            return Category.OPERATOR_WORD
        return Category.KEYWORD
    if token_type in {"TEMPLATE_START", "TEMPLATE_END", "STRING_FRAGMENT", STRING_TAIL}:
        return Category.STRING
    if token_type in {"VERBATIM_START", "VERBATIM_END", VERBATIM_TAIL}:
        return Category.VERBATIM
    if token_type in {"INTERP_START", "INTERP_END"}:
        return Category.INTERPOLATION
    if token_type == "ENV_HOLE":
        return Category.ENV_HOLE
    if token_type in {"INT", "DECIMAL"}:
        return Category.NUMBER
    if token_type in _OPERATOR_TOKENS:
        return Category.OPERATOR
    if token_type in _PUNCTUATION_TOKENS:
        return Category.PUNCTUATION
    if token_type in {"PLACEHOLDER", "PLACEHOLDER_NUM"}:
        return Category.PLACEHOLDER
    if token_type in {"MODPATH", "MODQUAL"}:
        return Category.MODULE
    if token_type in {"NAME", "OP_NAME"}:
        known_type = token_text in BUILTIN_TYPE_NAMES or token_text in type_names
        known_constructor = token_text in constructor_names
        if known_constructor and (not known_type or next_sig_type in _CALL_LOOKAHEAD):
            return Category.CONSTRUCTOR
        if known_type:
            return Category.TYPE
        if token_text in BUILTIN_CALL_NAMES and next_sig_type in _CALL_LOOKAHEAD:
            return Category.BUILTIN_CALL
        return Category.NAME
    if token_type == "ERROR":
        return Category.ERROR
    return Category.TEXT


def _next_significant_types(tokens: list[Token]) -> list[str | None]:
    result: list[str | None] = [None] * len(tokens)
    nxt: str | None = None
    for index in range(len(tokens) - 1, -1, -1):
        result[index] = nxt
        if tokens[index].type not in _LAYOUT_TOKENS:
            nxt = tokens[index].type
    return result


def _attribute_styles(tokens: list[Token]) -> tuple[dict[int, Category], set[int]]:
    """Classify @name prefixes; balanced arguments are transparent to declarations."""
    forced: dict[int, Category] = {}
    covered: set[int] = set()
    significant = [i for i, token in enumerate(tokens) if token.type not in _LAYOUT_TOKENS]
    position = 0
    while position < len(significant):
        at_index = significant[position]
        if tokens[at_index].type != "AT" or position + 1 >= len(significant):
            position += 1
            continue
        name_index = significant[position + 1]
        if tokens[name_index].type != "NAME":
            position += 1
            continue
        forced[at_index] = forced[name_index] = Category.ATTRIBUTE
        covered.update((at_index, name_index))
        position += 2
        if position < len(significant) and tokens[significant[position]].type == "LPAR":
            depth = 0
            scan = position
            while scan < len(significant):
                depth += {"LPAR": 1, "RPAR": -1}.get(tokens[significant[scan]].type, 0)
                scan += 1
                if depth == 0:
                    covered.update(significant[position:scan])
                    position = scan
                    break
    return forced, covered


def _decl_site_styles(
    text: str,
    tokens: list[Token],
) -> tuple[dict[int, Category], set[str], set[str]]:
    """Seed local type/constructor names and recognize contextual infix ``at``."""
    forced, attribute_tokens = _attribute_styles(tokens)
    types: set[str] = set()
    constructors: set[str] = set()
    expect: str | None = None
    in_enum_variants = False
    infix_state: str | None = None
    prev_sig: str | None = None
    for index, token in enumerate(tokens):
        ttype = token.type
        if ttype in _LAYOUT_TOKENS or index in attribute_tokens:
            continue
        is_name = ttype in {"NAME", "OP_NAME"}
        if infix_state == "expect_op":
            infix_state = "expect_at"
        elif infix_state == "expect_at":
            if is_name and text[token.start_pos : token.end_pos] == "at":
                forced[index] = Category.KEYWORD
            infix_state = None
        if is_name:
            name = text[token.start_pos : token.end_pos]
            if expect in _DECL_TYPE_KEYWORDS:
                forced[index] = Category.TYPE
                types.add(name)
                if expect in _DECL_CONSTRUCTOR_KEYWORDS:
                    constructors.add(name)
                in_enum_variants = expect == "enum"
            elif in_enum_variants and prev_sig == "PIPE":
                forced[index] = Category.CONSTRUCTOR
                constructors.add(name)
            expect = None
        elif ttype in KEYWORDS:
            expect = ttype if ttype in _DECL_TYPE_KEYWORDS else None
            in_enum_variants = False
            if ttype in {"infixl", "infixr"}:
                infix_state = "expect_op"
        else:
            expect = None
        prev_sig = ttype
    return forced, types, constructors


def _source_offsets(source: str) -> list[int]:
    """Map normalized lexer boundaries back to original CRLF/lone-CR boundaries."""
    offsets = [0]
    index = 0
    while index < len(source):
        index += 2 if source.startswith("\r\n", index) else 1
        offsets.append(index)
    return offsets


def _project(text: str, candidates: list[Span]) -> list[Span]:
    """Later source starts win overlaps; at equal starts the later candidate wins."""
    ordered = sorted((a, index, b, category) for index, (a, b, category) in enumerate(candidates))
    boundaries = sorted({0, len(text), *(p for a, b, _ in candidates for p in (a, b))})
    active: list[tuple[int, int, int, Category]] = []
    cursor = 0
    spans: list[Span] = []
    for start, end in zip(boundaries, boundaries[1:]):
        while cursor < len(ordered) and ordered[cursor][0] <= start:
            a, index, b, category = ordered[cursor]
            heapq.heappush(active, (-a, -index, b, category))
            cursor += 1
        while active and active[0][2] <= start:
            heapq.heappop(active)
        category = active[0][3] if active else Category.TEXT
        spans.append((start, end, category))
    return spans


def _escape_spans(text: str, start: int, end: int) -> list[Span]:
    """Split raw escapes using the same decoder as the scanner."""
    spans: list[Span] = []
    cursor = start
    while (escape := text.find("\\", cursor, end)) >= 0:
        if escape > cursor:
            spans.append((cursor, escape, Category.STRING))
        try:
            _, stop = decode_escape(text, escape)
            category = Category.STRING_ESCAPE
        except ValueSyntaxError as error:
            stop = error.end
            category = Category.ERROR
        cursor = min(stop, end)
        spans.append((escape, cursor, category))
    if cursor < end:
        spans.append((cursor, end, Category.STRING))
    return spans


def _token_categories(
    text: str,
    tokens: list[Token],
    type_names: frozenset[str],
    constructor_names: frozenset[str],
) -> list[Category]:
    """Share classification independently of exact or legacy span projection."""
    forced, local_types, local_constructors = _decl_site_styles(text, tokens)
    types = type_names | local_types
    constructors = constructor_names | local_constructors
    next_sig = _next_significant_types(tokens)
    categories: list[Category] = []
    literal_modes: list[Category] = []
    for index, token in enumerate(tokens):
        if token.type in {"VERBATIM_START", "TEMPLATE_START"}:
            literal_modes.append(
                Category.VERBATIM if token.type == "VERBATIM_START" else Category.STRING
            )
        elif token.type in {"VERBATIM_END", "TEMPLATE_END"} and literal_modes:
            literal_modes.pop()
        start, end = token.start_pos, token.end_pos
        category = forced.get(index)
        if category is None:
            category = _category_for(
                token.type, text[start:end], types, constructors, next_sig[index]
            )
        if token.type == "STRING_FRAGMENT" and literal_modes:
            category = literal_modes[-1]
        categories.append(category)
    return categories


def _literal_backgrounds(text: str, tokens: list[Token]) -> list[Span]:
    """Use delimiter structure, never cooked fragment widths, for raw extents.

    Holes overlay TEXT backgrounds so their trivia is code, not literal content;
    nested literals in those holes supply their own backgrounds.
    """
    stack: list[tuple[int, Category]] = []
    spans: list[Span] = []
    for index, token in enumerate(tokens):
        start, end = token.start_pos, token.end_pos
        if start is None or end is None:
            continue
        # A bracket-deferred `$` resumes CODE without a payload or closer.
        if token.type == "VERBATIM_START" and (
            index + 1 == len(tokens)
            or tokens[index + 1].type
            not in {"STRING_FRAGMENT", "INTERP_START", "VERBATIM_END", VERBATIM_TAIL}
        ):
            continue
        if token.type in {"TEMPLATE_START", "VERBATIM_START", "INTERP_START"} and end > start:
            category = {
                "TEMPLATE_START": Category.STRING,
                "VERBATIM_START": Category.VERBATIM,
                "INTERP_START": Category.TEXT,
            }[token.type]
            stack.append((start, category))
        elif token.type in {"TEMPLATE_END", "VERBATIM_END", "INTERP_END"} and stack:
            opening, category = stack.pop()
            spans.append((opening, end, category))
        elif token.type == "ERROR":
            # Recovery can omit a verbatim closer. A rejected opener owns only
            # its consumed extent, never subsequent CODE trivia.
            for index, (opening, _) in enumerate(stack):
                if opening == start:
                    spans.extend((a, end, c) for a, c in stack[index:])
                    del stack[index:]
                    break
    spans.extend((start, len(text), category) for start, category in stack if start < len(text))
    return spans


def _token_spans(
    text: str,
    tokens: list[Token],
    comments: list[tuple[int, int]],
    type_names: frozenset[str],
    constructor_names: frozenset[str],
) -> list[Span]:
    candidates = _literal_backgrounds(text, tokens)
    candidates.extend((a, b, Category.COMMENT) for a, b in comments)
    categories = _token_categories(text, tokens, type_names, constructor_names)
    for token, category in zip(tokens, categories):
        start, end = token.start_pos, token.end_pos
        if (
            start is not None
            and end is not None
            and end > start
            and token.type not in _LAYOUT_TOKENS | {"STRING_FRAGMENT"}
            and not (token.type == "done" and text[start:end] != "done")
        ):
            candidates.append((start, end, category))
    spans: list[Span] = []
    for start, end, category in _project(text, candidates):
        if category == Category.STRING:
            spans.extend(_escape_spans(text, start, end))
        else:
            spans.append((start, end, category))
    return spans


def declared_names(source: str) -> tuple[frozenset[str], frozenset[str]]:
    """Return recovery-recognized type and constructor declarations for fragment seeding."""
    text = normalize_newlines(source)
    tokens, _ = tokenize_recover(text)
    _, types, constructors = _decl_site_styles(text, tokens)
    return frozenset(types), frozenset(constructors)


def lexical_spans(
    source: str,
    type_names: frozenset[str] = frozenset(),
    constructor_names: frozenset[str] = frozenset(),
) -> list[Span]:
    """Return a total, nonoverlapping partition, retaining recovered code categories."""
    text = normalize_newlines(source)
    result = _tokenize_recover(text)
    offsets = _source_offsets(source)
    return [
        (offsets[a], offsets[b], c)
        for a, b, c in _token_spans(
            text, result.tokens, result.comments, type_names, constructor_names
        )
    ]


def open_string_start(source: str) -> int | None:
    """Preserve raw-quote completion policy via the recovery scanner.

    Unlike AgL syntax, this legacy policy recognizes quotes in verbatim text,
    treats interpolation as literal, and allows single quotes across newlines.
    Comments are skipped only outside quotes.
    """
    return lex_open_quote_start(source)


def repl_spans(
    source: str,
    type_names: frozenset[str] = frozenset(),
    constructor_names: frozenset[str] = frozenset(),
) -> list[Span]:
    """Return legacy diagnostic spans, not the exact lexical partition.

    Preserve plain-on-error, source-order clipping, and normalized offsets;
    the UI clips each span to its starting line. Classification is shared.
    """
    text = normalize_newlines(source)
    result = _tokenize_recover(text)
    tokens = result.tokens
    if result.errors:
        first = result.errors[0]
        tail = next((t for t in tokens if t.type == VERBATIM_TAIL), None)
        start = (
            tail.start_pos
            if tail is not None and isinstance(first, IncompleteInputError)
            else lex_open_quote_start(source)
        )
        if start is None:
            return [(0, len(source), Category.TEXT)] if source else []
        spans = repl_spans(source[:start], type_names, constructor_names)
        spans.append((start, len(source), Category.STRING))
        return spans
    categories = _token_categories(source, tokens, type_names, constructor_names)
    next_starts = [len(source)] * len(tokens)
    following = len(source)
    for index in range(len(tokens) - 1, -1, -1):
        next_starts[index] = following
        start, end = tokens[index].start_pos, tokens[index].end_pos
        if start is not None and end is not None and end > start:
            following = start
    spans = [(a, b, Category.COMMENT) for a, b in result.comments]
    for token, category, following in zip(tokens, categories, next_starts):
        start, end = token.start_pos, token.end_pos
        if start is not None and end is not None:
            end = min(end, following)
            if end > start and category != Category.TEXT:
                spans.append((start, end, category))
    return spans
