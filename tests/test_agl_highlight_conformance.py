"""AgL grammar parity against exact lexical recognition, not semantic resolution."""

import re
from dataclasses import dataclass

import pytest

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category, Span
from agnette.agl.highlight.grammar_eval import evaluate
from agnette.agl.highlight.lexical import _token_spans, lexical_spans
from agnette.agl.keywords import KEYWORDS, OPERATOR_SOFT_KEYWORDS, SOFT_KEYWORDS
from agnette.agl.lexer import _tokenize_recover, tokenize_recover
from agnette.agl.lexer.scanner import scan
from agnette.agl.parser.parser import parse_program
from agnette.agl.scope.symbols import BUILTIN_CALL_NAMES
from agnette.agl.syntax import BUILTIN_TYPE_NAMES, Call
from agnette.agl.value_syntax.errors import ValueSyntaxError
from agnette.agl.value_syntax.lexical import decode_escape
from agnette.util.text import normalize_newlines
from tests._agl_corpus import lexical_corpus_batches


def _characters(source: str, spans: list[Span]) -> list[Category]:
    result: list[Category] = []
    for start, end, category in spans:
        assert start == len(result)
        assert end > start
        result.extend([category] * (end - start))
    assert len(result) == len(source)
    return result


def _plain_name(spelling: str, next_type: str | None) -> Category:
    if spelling in OPERATOR_SOFT_KEYWORDS:
        return Category.OPERATOR_WORD
    if spelling in BUILTIN_TYPE_NAMES:
        return Category.TYPE
    if spelling in BUILTIN_CALL_NAMES and next_type in {"LPAR", "DCOLON"}:
        return Category.BUILTIN_CALL
    return Category.NAME


def _normalized_offsets(source: str) -> tuple[str, list[int]]:
    return normalize_newlines(source), [
        0,
        *(match.end() for match in re.finditer(r"\r\n|[\s\S]", source)),
    ]


_CONTEXT_ROLES = {
    Category.TYPE,
    Category.CONSTRUCTOR,
    Category.ATTRIBUTE,
    Category.MODULE,
    Category.FUNCTION,
    Category.LOCAL,
    Category.PARAMETER,
}


def _project(source: str, spans: list[Span]) -> list[Category]:
    """Symmetric coarse roles; never manufacture expected contextual recognition.

    Rescan only styled code spans, not token/layout history. Contextual names
    retain canonical builtin categories; only attribute/module punctuation is
    source-normalized. Strings, errors, numbers and other core categories stay
    untouched. Independent examples verify the finer contextual contract.
    """
    result = _characters(source, spans)
    for start, end, original in spans:
        if original not in _CONTEXT_ROLES | {
            Category.NAME,
            Category.KEYWORD,
            Category.OPERATOR_WORD,
            Category.OPERATOR,
        }:
            continue
        spelling = source[start:end]
        _, offsets = _normalized_offsets(spelling)
        for token in scan(spelling):
            a, b = start + offsets[token.start_pos], start + offsets[token.end_pos]
            word = source[a:b]
            category: Category | None = None
            if original in {Category.MODULE, Category.NAME, Category.OPERATOR} and word == "/*":
                category = Category.OPERATOR
            elif token.type in {"NAME", "OP_NAME"} and original in _CONTEXT_ROLES:
                marker = re.match(r"[ \t\r\n]*(\(|::)", source[b:])
                following = ("LPAR" if marker[1] == "(" else "DCOLON") if marker else None
                category = _plain_name(word, following)
            elif word in SOFT_KEYWORDS:
                category = _plain_name(word, None)
            elif word == "at" and original == Category.KEYWORD:
                category = Category.NAME
            elif word == "_" and original == Category.OPERATOR:
                category = Category.NAME
            elif original == Category.ATTRIBUTE and token.type == "AT":
                category = Category.OPERATOR
            elif original == Category.MODULE and token.type in {"SLASH", "DCOLON"}:
                category = Category.OPERATOR
            if category is not None:
                result[a:b] = [category] * (b - a)
    return result


@pytest.mark.parametrize(
    "source",
    [
        "record Box\n  value: int\nlet b = Box(1)\nBox",
        "enum Choice = | Yes | No\nYes No",
        "@arg-pos(1) def f(x: int) -> int = x",
        'import std/text\nstd/text::split("x")',
        "scope x\nend\nx and y or not z\nlet and = 1\nf(with = 2)",
        "infixl + at 5",
        "foo/*",
    ],
)
def test_context_projection_retains_plain_lexical_structure(source: str) -> None:
    assert _project(source, evaluate(AGL_GRAMMAR, source)) == _project(
        source, lexical_spans(source)
    )


@pytest.mark.parametrize(
    "source",
    [
        "# comment\nlet n = 12.34 + 56\ntrue false null () [] {} : , . ; := :: ? ?12 _",
        '"hello\\n\\u0041\\uD83D\\uDE00\\${HOME} ${HOME} %{ {1: "nested"} }"',
        "'hello\\' %{ {1: 'nested'} }'",
        '"""hello %{ {1: "nested"} }"""',
        '$ hello \\%{literal} %{ {1: "nested"} } ${HOME} # literal   \nnext',
        'resource-path record-id αname foo"bar a+b ++ <=> 123abc １２',
        "int decimal str bool print(1) print::[int](1) print-value print",
        "1if 1true 1int 1print(2) +if resource+if",
    ],
)
def test_core_constructs_match_exact_lexical_categories(source: str) -> None:
    assert _characters(source, evaluate(AGL_GRAMMAR, source)) == _characters(
        source, lexical_spans(source)
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_triple_strings_keep_state_but_line_delimiters_are_text(newline: str) -> None:
    source = f'"""one{newline}two %{{ {{1: 2}} }}{newline}three""" let x = 1'
    actual = _characters(source, evaluate(AGL_GRAMMAR, source))
    expected = _characters(source, lexical_spans(source))
    for index, character in enumerate(source):
        assert actual[index] == (Category.TEXT if character in "\r\n" else expected[index])


@pytest.mark.parametrize("word", sorted(KEYWORDS | SOFT_KEYWORDS | BUILTIN_TYPE_NAMES))
def test_canonical_word_inventory_and_identifier_boundaries(word: str) -> None:
    source = f"{word} {word}-suffix α{word} {word}α"
    actual = _project(source, evaluate(AGL_GRAMMAR, source))
    expected = _project(source, lexical_spans(source))
    assert actual == expected


@pytest.mark.parametrize("word", sorted(BUILTIN_CALL_NAMES))
def test_builtin_call_inventory(word: str) -> None:
    source = f"{word}(1) {word}::[int](1) {word}-suffix(1)"
    assert _characters(source, evaluate(AGL_GRAMMAR, source)) == _characters(
        source, lexical_spans(source)
    )


@pytest.mark.parametrize(
    "source",
    [
        '$\n  hello %{ {1: "nested"} }\nnext(1)',
        '"%{ {1: {2: {3: 4}}} }"',
        "'''one\ntwo''' let x = 1",
        "\"%{'''one\ntwo'''}\"",
        "𐐀name + 😀 ++ ≤ λname _name name+tail ${HOME}",
    ],
)
def test_nested_states_and_unicode_preserve_categories(source: str) -> None:
    actual = _project(source, evaluate(AGL_GRAMMAR, source))
    expected = _project(source, lexical_spans(source))
    allowed = _allowed_differences(source, actual, expected)
    assert all(a == e or index in allowed for index, (a, e) in enumerate(zip(actual, expected)))


# Construct-specific discrepancies, never file exemptions. Each has a witness.
DIVERGENCES = {
    "literal-line-delimiters": "Line-local engines assign TEXT to newlines, even inside literals.",
    "relative-verbatim-dedent": (
        "States cannot compare indentation with a header; only column-zero dedent ends them."
    ),
    "invalid-template-escape-recovery": (
        "An invalid escape rejects its template; online states cannot recolor its emitted prefix."
    ),
    "invalid-interpolation-escape-recovery": (
        "A rejected nested quote leaves overlapping literal backgrounds in scanner recovery; "
        "finite states mark the rejected suffix as ERROR through that literal's failing line."
    ),
    "rejected-template-diagnostics": (
        "Unterminated strings/holes reject prefixes; triple buffering rejects hole code too."
    ),
    "cross-line-builtin-call": (
        "Line-local lookahead cannot promote a builtin name whose call marker is on a later line."
    ),
    "incomplete-triple-buffering": (
        "Triple-string holes remain buffered until closing; incomplete tails hide their code."
    ),
    "verbatim-blank-indentation": (
        "Line-local rules cannot distinguish interior from trailing blank-line indentation."
    ),
    "rejected-verbatim-structure": (
        "Empty or under-indented payload diagnostics require layout state."
    ),
    "deferred-verbatim-bracket-diagnostic": (
        "The scanner defers a bracketed dollar's diagnostic until brackets close; "
        "online states flag only that marker immediately."
    ),
}


@dataclass(frozen=True)
class Comparison:
    name: str
    source: str
    actual: list[Category]
    expected: list[Category]
    allowed: dict[int, str]


def _buffered_holes(source: str, start: int, end: int) -> list[int]:
    """Locate candidates only; the recovery lexer validates ownership and closure."""
    return [index for index in range(start, end - 1) if source.startswith("%{", index)]


def _buffered_code_evidence(source: str, start: int, end: int) -> dict[int, Category]:
    """Unbuffer holes with a nonbuffering quoted host; retain real owner bounds.

    The recovery lexer owns raw categories, including nested literals and bad
    characters. Drop only the wrapper's retroactive diagnostic; project names
    with the same coarse P as both compared outputs, without contextual replay.
    """
    evidence: dict[int, Category] = {}
    for opening in _buffered_holes(source, start, end):
        # Parent BODY tokens already classify nested quotes, holes and braces.
        # A candidate inside that body cannot replace those independent facts.
        if opening in evidence:
            continue
        # Complete only the candidate's BODY to expose its real literal opener.
        prefix = normalize_newlines(source[start:opening])
        probe, _ = tokenize_recover(prefix + "%{0}" + source[start : start + 3])
        if not any(part.type == "INTERP_START" and part.start_pos == len(prefix) for part in probe):
            continue
        fragment = '"' + source[opening:end]
        text, offsets = _normalized_offsets(fragment)
        recovery = _tokenize_recover(text)
        tokens = recovery.tokens
        depth = 0
        stop = len(fragment)
        for part in tokens:
            depth += {"INTERP_START": 1, "INTERP_END": -1}.get(part.type, 0)
            if part.type == "INTERP_END" and depth == 0:
                assert part.end_pos is not None
                stop = offsets[part.end_pos]
                break
        spans = [
            (offsets[a], offsets[b], category)
            for a, b, category in _token_spans(
                text,
                [part for part in tokens if part.type != "ERROR" or part.start_pos != 0],
                recovery.comments,
                frozenset(),
                frozenset(),
            )
        ]
        for index, category in enumerate(_project(fragment, spans)[1:stop], opening):
            evidence.setdefault(index, category)
    return evidence


def _verbatim_evidence(source: str, start: int, end: int) -> list[Category]:
    """Repair only block margins; the recovery lexer owns all body categories."""
    fragment = source[start:end]
    text, offsets = _normalized_offsets(fragment)
    positions: list[int | None] = list(range(len(text)))
    probe = text
    if re.match(r"\$[ \t]*\n", text):
        probe = ""
        positions = []
        for line_index, match in enumerate(re.finditer(r"[^\n]*\n|[^\n]+$", text)):
            line = match[0]
            opening = match.start()
            if line_index == 0:
                probe += line
                positions.extend(range(len(line)))
            else:
                margin = len(line) - len(line.lstrip(" \t"))
                # Uniform one-column margins avoid the rejected layout without
                # changing payload spelling, including nested quotes and errors.
                probe += " " + line[margin:]
                positions.append(None)
                positions.extend(range(opening + margin, opening + len(line)))
    recovery = _tokenize_recover(probe)
    spans = _token_spans(
        probe,
        [part for part in recovery.tokens if part.type != "ERROR" or part.start_pos != 0],
        recovery.comments,
        frozenset(),
        frozenset(),
    )
    # Removed block margins remain literal background, not code evidence.
    evidence = [Category.VERBATIM] * len(fragment)
    for position, category in zip(positions, _project(probe, spans), strict=True):
        if position is not None:
            evidence[offsets[position] : offsets[position + 1]] = [category] * (
                offsets[position + 1] - offsets[position]
            )
    return evidence


def _allowed_differences(
    source: str, actual: list[Category], expected: list[Category]
) -> dict[int, str]:
    allowed: dict[int, str] = {}
    for index, character in enumerate(source):
        if (
            character in "\r\n"
            and actual[index] == Category.TEXT
            and expected[index]
            in {
                Category.STRING,
                Category.VERBATIM,
            }
        ):
            allowed[index] = "literal-line-delimiters"
    blank_indentation = {
        index
        for blank in re.finditer(r"(?:^|(?<=[\r\n]))[ \t]+(?=[\r\n]|$)", source)
        for index in range(blank.start(), blank.end())
    }
    for index in blank_indentation:
        if actual[index] == Category.TEXT and expected[index] == Category.VERBATIM:
            allowed[index] = "verbatim-blank-indentation"
    text, offsets = _normalized_offsets(source)
    recovery = _tokenize_recover(text)
    tokens, errors = recovery.tokens, recovery.errors
    quote_stack: list[int] = []
    closing_owners: dict[int, int] = {}
    for token_index, token in enumerate(tokens):
        if token.type == "TEMPLATE_START":
            quote_stack.append(token.start_pos)
        elif token.type == "ERROR":
            # Recovery closes the rejected owner, not an abandoned nested quote.
            if token.start_pos in quote_stack:
                del quote_stack[quote_stack.index(token.start_pos) + 1 :]
        elif token.type == "TEMPLATE_END" and quote_stack:
            owner = quote_stack.pop()
            if token.start_pos < token.end_pos:
                closing_owners[token_index] = owner
    invalid_escapes: set[int] = set()
    for error in errors:
        position = error.span.start_offset
        if text[position : position + 1] == "\\":
            try:
                decode_escape(text, position)
            except ValueSyntaxError as invalid:
                if str(error) == invalid.message:
                    invalid_escapes.add(position)
    online: list[Category] | None = None
    significant = [
        token for token in tokens if token.type not in {"_INDENT", "_DEDENT", "_NEWLINE"}
    ]
    for token, following in zip(significant, significant[1:]):
        if (
            token.type == "NAME"
            and str(token) in BUILTIN_CALL_NAMES
            and following.type in {"LPAR", "DCOLON"}
            and "\n" in text[token.end_pos : following.start_pos]
        ):
            for index in range(offsets[token.start_pos], offsets[token.end_pos]):
                if actual[index] == Category.NAME and expected[index] == Category.BUILTIN_CALL:
                    allowed[index] = "cross-line-builtin-call"
    bracket_depth = 0
    for token_index, token in enumerate(tokens):
        bracket_depth += {
            "LPAR": 1,
            "LSQB": 1,
            "LBRACE": 1,
            "INTERP_START": 1,
            "RPAR": -1,
            "RSQB": -1,
            "RBRACE": -1,
            "INTERP_END": -1,
        }.get(token.type, 0)
        start, end = offsets[token.start_pos], offsets[token.end_pos]
        if token.type == "VERBATIM_START" and bracket_depth > 0:
            for index in range(start, end):
                if actual[index] == Category.ERROR and expected[index] == Category.VERBATIM:
                    allowed[index] = "deferred-verbatim-bracket-diagnostic"
        if token.type == "STRING_TAIL" and text[token.start_pos :].startswith(('"""', "'''")):
            owner = next(
                part for part in reversed(tokens[:token_index]) if part.type == "TEMPLATE_START"
            )
            # A nested unfinished literal owns its tail, not its buffered host hole.
            stop = start if token.start_pos > owner.start_pos else end
            for position, category in _buffered_code_evidence(
                source, offsets[owner.start_pos], stop
            ).items():
                if expected[position] == Category.STRING and actual[position] == category:
                    allowed[position] = "incomplete-triple-buffering"
        if token.type == "VERBATIM_START" and re.match(r"\$[ \t]*\n", text[token.start_pos :]):
            line_start = max(source.rfind("\n", 0, start), source.rfind("\r", 0, start)) + 1
            if source[line_start:start].startswith((" ", "\t")):
                # Only the approximation's spill after the exact payload is
                # exempt, and only when it is still classified as VERBATIM.
                payload_end = next(
                    (
                        offsets[part.start_pos]
                        for part in tokens
                        if part.type == "VERBATIM_END" and part.start_pos > token.start_pos
                    ),
                    len(source),
                )
                boundary = re.search(r"(?:\r\n|\r|\n)(?=[^ \t\r\n])", source[payload_end:])
                stop = payload_end + boundary.end() if boundary else len(source)
                for index in range(payload_end, stop):
                    if actual[index] == Category.VERBATIM and expected[index] not in {
                        Category.VERBATIM,
                        Category.INTERPOLATION,
                    }:
                        allowed[index] = "relative-verbatim-dedent"
        if token.type == "ERROR":
            # Scanner recovery can retain a nested quote's raw background after
            # its real/synthetic closer. Exempt only the rejected literal's
            # STRING suffix, never the recovered statements beyond its extent.
            for position in invalid_escapes:
                nested = [
                    part
                    for part in tokens
                    if part.type == "TEMPLATE_START"
                    and token.start_pos < part.start_pos < position < token.end_pos
                    and any(
                        hole.type == "INTERP_START"
                        and token.start_pos <= hole.start_pos < part.start_pos
                        for hole in tokens
                    )
                ]
                if nested:
                    for index in range(offsets[position], end):
                        if actual[index] == Category.ERROR and expected[index] == Category.STRING:
                            allowed[index] = "invalid-interpolation-escape-recovery"
            spelling = source[start:end]
            if spelling.startswith(('"', "'")):
                reason = (
                    "invalid-template-escape-recovery"
                    if any(
                        token.start_pos <= position < token.end_pos for position in invalid_escapes
                    )
                    else "rejected-template-diagnostics"
                )
                if online is None:
                    # Drop retroactive diagnostics, retaining real raw-token/escape evidence.
                    online = _characters(
                        source,
                        [
                            (offsets[a], offsets[b], category)
                            for a, b, category in _token_spans(
                                text,
                                [part for part in tokens if part.type != "ERROR"],
                                recovery.comments,
                                frozenset(),
                                frozenset(),
                            )
                        ],
                    )
                failure = min(
                    (offsets[p] for p in invalid_escapes if token.start_pos <= p < token.end_pos),
                    default=end,
                )
                buffered = (
                    _buffered_code_evidence(source, start, failure)
                    if spelling.startswith(('"""', "'''"))
                    else {}
                )
                for index in range(start, failure):
                    category = buffered.get(index, online[index])
                    if expected[index] == Category.ERROR and (
                        actual[index] == category
                        or (
                            source[index] in "\r\n"
                            and actual[index] == Category.TEXT
                            and category == Category.STRING
                        )
                    ):
                        allowed[index] = reason
                # Only this rejected owner's real closer after failure may be ERROR.
                for closing_index, closing in enumerate(tokens):
                    if (
                        closing_owners.get(closing_index) == token.start_pos
                        and failure <= offsets[closing.start_pos]
                        and token.start_pos <= closing.start_pos < closing.end_pos <= token.end_pos
                    ):
                        for index in range(offsets[closing.start_pos], offsets[closing.end_pos]):
                            if (
                                actual[index] == Category.ERROR
                                and expected[index] == Category.STRING
                            ):
                                allowed[index] = reason
            elif spelling.startswith("$") and any(
                part.type == "VERBATIM_START" and part.start_pos == token.start_pos
                for part in tokens
            ):
                evidence = _verbatim_evidence(source, start, end)
                for index in range(start, end):
                    category = evidence[index - start]
                    if expected[index] == Category.ERROR and (
                        actual[index] == category
                        or (
                            actual[index] == Category.TEXT
                            and category in {Category.VERBATIM, Category.TEXT}
                            and (source[index] in "\r\n" or index in blank_indentation)
                        )
                    ):
                        allowed[index] = "rejected-verbatim-structure"
    return allowed


@pytest.mark.parametrize(
    "source, word",
    [
        ('"""one\ntwo""" let x = 1', "two"),
        ("$\n  hello %{value}\nnext", "value"),
        ("def f() =\n  let x = $\n    hi %{value}\n  print(1)", "value"),
        ('"bad\\q"\nlet x = 1', "let"),
        ('"%{ "bad\\q" }"\nlet x=1', "let"),
        ('$ hi %{ "bad\\q" }\nlet x=1', "let"),
        ("[$\n  x\n]\nlet x=1", "x"),
        ('"%{ $\n  x\n}"\nlet x=1', "x"),
        ("$ hi\nlet x = 1", "let"),
        ("$\rlet x=1", "let"),
        ("$\r\nlet x=1", "let"),
        ("[$ true]\rlet x=1", "let"),
        ("[$ true]\rlet x=1", "true"),
        ("[$\n  x\nlet x=1", "x"),
        ("[$\n  x\nlet x=1", "let"),
    ],
)
def test_divergences_do_not_exempt_unrelated_or_payload_code(source: str, word: str) -> None:
    actual = _project(source, evaluate(AGL_GRAMMAR, source))
    expected = _project(source, lexical_spans(source))
    index = source.index(word)
    actual[index] = Category.VERBATIM if expected[index] != Category.VERBATIM else Category.ERROR
    assert actual[index] != expected[index]
    assert index not in _allowed_differences(source, actual, expected)


@pytest.mark.parametrize(
    "source, word, category",
    [
        ("record Box\n  value: int", "int", Category.TYPE),
        ('@doc("text") def f() = 12', "12", Category.NUMBER),
        ("import std/text\nlet x = true", "true", Category.KEYWORD_CONSTANT),
        ('"with"', "with", Category.STRING),
    ],
)
def test_context_projection_does_not_collapse_core_categories(
    source: str, word: str, category: Category
) -> None:
    expected = _project(source, lexical_spans(source))
    start = source.index(word)
    assert expected[start : start + len(word)] == [category] * len(word)


@pytest.mark.parametrize(
    "source, spelling, replacement",
    [
        ("record Box", "record", Category.ATTRIBUTE),
        ("record Box", "Box", Category.NUMBER),
        ("import std/text", "std", Category.ERROR),
        ("@tag(12)", "12", Category.TYPE),
        ("int", "int", Category.NAME),
        ("print(1)", "print", Category.NAME),
        ("true", "true", Category.NAME),
        ("12", "12", Category.MODULE),
        ("?2", "?2", Category.CONSTRUCTOR),
        ("x = 1", "=", Category.MODULE),
        ("# import", "import", Category.KEYWORD),
        ('"scope"', "scope", Category.KEYWORD),
        ('"\\\\n"', "\\\\n", Category.ATTRIBUTE),
        ('"${HOME}"', "${HOME}", Category.MODULE),
        ('"%{ x }"', "%{", Category.ATTRIBUTE),
        ("$ import", "import", Category.KEYWORD),
        ('"bad\\\\q"', "\\\\q", Category.TYPE),
    ],
)
def test_symmetric_projection_does_not_erase_core_regressions(
    source: str, spelling: str, replacement: Category
) -> None:
    spans = evaluate(AGL_GRAMMAR, source)
    expected = _project(source, spans)
    start = source.index(spelling)
    end = start + len(spelling)
    # Split at source boundaries: simulate a wrong core classification rather
    # than relying on the grammar's current span coalescing.
    mutated: list[Span] = []
    for a, b, category in spans:
        if a < start:
            mutated.append((a, min(b, start), category))
        if a < end and b > start:
            mutated.append((max(a, start), min(b, end), replacement))
        if b > end:
            mutated.append((max(a, end), b, category))
    assert _project(source, mutated) != expected


@pytest.mark.parametrize("category", [Category.FUNCTION, Category.LOCAL, Category.PARAMETER])
def test_semantic_only_roles_project_to_plain_names(category: Category) -> None:
    source = "let value = 1"
    spans = lexical_spans(source)
    overlay = [(a, b, category if source[a:b] == "value" else original) for a, b, original in spans]
    assert _project(source, overlay) == _characters(source, spans)


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_context_projection_uses_original_newline_offsets(newline: str) -> None:
    source = newline.join(
        ["record Box", "  value: int", "let b = Box(1)", "scope s", "end", "s with x"]
    )
    expected = _project(source.replace(newline, "\n"), lexical_spans(source.replace(newline, "\n")))
    expanded: list[Category] = []
    for character, category in zip(source.replace(newline, "\n"), expected, strict=True):
        expanded.extend([category] * (len(newline) if character == "\n" else 1))
    assert _project(source, lexical_spans(source)) == expanded
    assert _project(source, evaluate(AGL_GRAMMAR, source)) == expanded


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_relative_verbatim_divergence_uses_original_newline_offsets(newline: str) -> None:
    source = newline.join(["def f() =", "  let x = $", "    hi %{value}", "  print(1)"])
    case = _comparison("relative-newlines", source)
    _assert_conformance(case)
    assert "relative-verbatim-dedent" in case.allowed.values()
    assert source.index("value") not in case.allowed


@pytest.mark.parametrize(
    "source, spelling, replacement",
    [
        ('"bad\\n', "bad", Category.NUMBER),
        ('"bad\\n', "\\n", Category.NUMBER),
        ('"bad\\n', "\\n", Category.STRING),
        ('"""bad\\n%{1\nlet x=1', "\\n", Category.NUMBER),
        ('"""bad\\n%{1\nlet x=1', "let", Category.STRING),
        ('"abc\\n%{1} tail', "tail", Category.NUMBER),
        ('"bad\\q"\nlet x=1', "bad", Category.NUMBER),
        ('"bad\\q suffix"\nlet x=1', "suffix", Category.STRING),
        ('"bad\\q"\nlet x=1', "let", Category.STRING),
    ],
)
def test_diagnosed_literal_exemptions_reject_core_mutations(
    source: str, spelling: str, replacement: Category
) -> None:
    case = _comparison("diagnosed-literal-mutation", source)
    _assert_conformance(case)
    index = source.index(spelling)
    assert case.actual[index] != replacement
    case.actual[index] = replacement
    assert index not in _allowed_differences(source, case.actual, case.expected)


@pytest.mark.parametrize("outer", ['"', '"""'])
@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize(
    "before_failure, nested",
    [(False, "'"), (False, "'''"), (True, "'"), (True, "'''"), (True, '"'), (True, '"""')],
)
def test_rejected_literal_closer_exemption_keeps_nested_delimiters_strict(
    outer: str, nested: str, complete: bool, before_failure: bool
) -> None:
    hole = "%{ " + nested + "hi" + nested + " }"
    body = "abc" + hole + " tail\\q" if before_failure else "abc\\q" + hole + " tail"
    source = outer + body + (outer if complete else "")
    case = _comparison("nested-delimiters-around-failure", source)
    _assert_conformance(case)
    opening = source.index(nested + "hi", len(outer))
    closing = opening + len(nested) + len("hi")
    replacement = Category.ERROR if before_failure else Category.STRING
    for start in (opening, closing):
        positions = range(start, start + len(nested))
        assert all(case.actual[index] != replacement for index in positions)
        mutated = case.actual.copy()
        for index in positions:
            mutated[index] = replacement
        assert not set(positions) & _allowed_differences(source, mutated, case.expected).keys()
    if complete:
        positions = range(len(source) - len(outer), len(source))
        assert all(case.actual[index] == Category.ERROR for index in positions)
        assert all(case.expected[index] == Category.STRING for index in positions)
        assert all(case.allowed[index] == "invalid-template-escape-recovery" for index in positions)
        mutated = case.actual.copy()
        for index in positions:
            mutated[index] = Category.NUMBER
        assert not set(positions) & _allowed_differences(source, mutated, case.expected).keys()


@pytest.mark.parametrize(
    "source, spelling, replacement",
    [
        ('""" payload %{1 """', "1", Category.KEYWORD),
        ('"""%{1', "%{1", Category.STRING),
        ("'''%{1\nlet x=1", "1", Category.KEYWORD),
        ('"""%{let x = 12 + \'hi\'\nlet y=2', "let", Category.NUMBER),
        ('"""%{let x = 12 + \'hi\'\nlet y=2', "12", Category.STRING),
        ('"""%{let x = 12 + \'hi\'\nlet y=2', "hi", Category.KEYWORD),
        ('"""%{let x = 12 + \'hi\'\nlet y=2', "+", Category.NUMBER),
        ('"""%{let x = 12 + \'hi\'\nlet y=2', " ", Category.STRING),
        ('"""%{1\t+ 2', "\t", Category.NUMBER),
        ('"""%{ # }', "#", Category.KEYWORD),
        ('"""%{let x = 12 + \'hi\' }', "let", Category.NUMBER),
        ('"""%{let x = 12 + \'hi\' }', "12", Category.KEYWORD),
        ('"""%{let x = 12 + \'hi\' }', "hi", Category.KEYWORD),
        ('"""%{let x = 12 + \'hi\' }', "+", Category.NUMBER),
        ('"""%{let x = 12 + \'hi\' }', " ", Category.OPERATOR),
        ('"""%{let x = 12 + \'hi\' }', "12", Category.ERROR),
        ('"""%{let x = 12 + \'hi\' }\nlet y=2', "12", Category.OPERATOR),
        ('"""%{ {1: {2: {3: 4}}} }', "4", Category.KEYWORD),
        ('"""%{ {1: \'%{2}\'} }', "2", Category.KEYWORD),
    ],
)
def test_buffered_hole_exemptions_require_exact_code_evidence(
    source: str, spelling: str, replacement: Category
) -> None:
    case = _comparison("buffered-hole-mutation", source)
    _assert_conformance(case)
    index = source.index(spelling)
    assert case.actual[index] == case.expected[index] or index in case.allowed
    assert case.actual[index] != replacement
    positions = range(index, index + len(spelling))
    for position in positions:
        case.actual[position] = replacement
    assert not set(positions) & _allowed_differences(source, case.actual, case.expected).keys()


@pytest.mark.parametrize(
    "source",
    [
        '"""%{1',
        '"""%{1\n# recovered comment\nlet x=1',
        "'''%{1\nlet x=1",
        '"""%{let x = 12 + \'hi\'\nlet y=2',
        '"""%{ {1: {2: {3: 4}}} }',
        '"""%{ {1: \'%{2}\'} }',
    ],
)
def test_buffered_holes_keep_eof_and_recovery_bounds(source: str) -> None:
    case = _comparison("buffered-hole-bounds", source)
    _assert_conformance(case)
    assert source.index("%{") in case.allowed
    if "\nlet" in source:
        recovered = source.index("\nlet") + 1
        assert case.actual[recovered : recovered + 3] == [Category.KEYWORD] * 3
        assert case.actual[recovered:] == case.expected[recovered:]
        assert not set(range(recovered, len(source))) & case.allowed.keys()


@pytest.mark.parametrize(
    "source, positions",
    [
        ('"""%{foo%{1}}', (8, 9, 11)),
        ('"""%{foo%{1}', (8, 9, 11)),
        ('"""%{ {1: 2} }', (6, 11)),
        ('"""%{ {1: \'%{2}\'} }', (13, 16)),
        ('"""\\%{1}', (4, 5, 7)),
        ('"""%{ \'foo\\%{1}\' }', (11, 12, 14)),
    ],
)
def test_buffered_hole_topology_rejects_fabricated_delimiters(
    source: str, positions: tuple[int, ...]
) -> None:
    case = _comparison("fabricated-hole-delimiters", source)
    _assert_conformance(case)
    for changed in [positions, *((position,) for position in positions)]:
        mutated = case.actual.copy()
        for index in changed:
            assert mutated[index] != Category.INTERPOLATION
            mutated[index] = Category.INTERPOLATION
        allowed = _allowed_differences(source, mutated, case.expected)
        assert not set(changed) & allowed.keys()


@pytest.mark.parametrize("nested", ["'%{2}'", "'''%{2}'''"])
def test_buffered_parent_retains_real_nested_quoted_hole_evidence(nested: str) -> None:
    source = '"""%{ {1: ' + nested + "} }"
    case = _comparison("real-nested-quoted-hole", source)
    _assert_conformance(case)
    opening = source.index("%{", source.index("%{") + 2)
    assert all(index in case.allowed for index in range(opening, opening + 4))
    for index, replacement in [(opening, Category.PUNCTUATION), (opening + 2, Category.NAME)]:
        mutated = case.actual.copy()
        mutated[index] = replacement
        assert index not in _allowed_differences(source, mutated, case.expected)


@pytest.mark.parametrize("quote", ['"""', "'''"])
@pytest.mark.parametrize("suffix", ["", "\nmore literal"])
def test_incomplete_nested_triple_hole_has_a_live_bounded_divergence(
    quote: str, suffix: str
) -> None:
    source = quote + " payload %{1 " + quote + suffix
    case = _comparison("unfinished-nested-triple", source)
    _assert_conformance(case)
    opening = source.index("%{")
    assert all(
        case.allowed[index] == "incomplete-triple-buffering"
        for index in range(opening, opening + 4)
    )
    assert not set(range(opening)) & case.allowed.keys()
    for index, replacement in [
        (source.index("payload"), Category.NUMBER),
        (opening, Category.STRING_ESCAPE),
        (opening + 1, Category.PUNCTUATION),
        (opening + 2, Category.VERBATIM),
        (opening + 3, Category.NUMBER),
        (opening + 4, Category.NUMBER),
    ]:
        mutated = case.actual.copy()
        mutated[index] = replacement
        assert index not in _allowed_differences(source, mutated, case.expected)


@pytest.mark.parametrize(
    "source", ['"bad\\n', '"bad\\n\nlet x=1', '"bad\\n\n# recovered comment\nlet x=1', "'bad\\t"]
)
def test_valid_escapes_in_unterminated_strings_are_not_invalid_escape_divergences(
    source: str,
) -> None:
    actual = _project(source, evaluate(AGL_GRAMMAR, source))
    expected = _project(source, lexical_spans(source))
    _assert_conformance(_comparison("valid-escape-recovery", source))
    assert (
        "invalid-template-escape-recovery"
        not in _allowed_differences(source, actual, expected).values()
    )


@pytest.mark.parametrize(
    "source",
    [
        '"""bad\\q\nlet x=1',
        "'''bad\\uD800\nlet x=1",
        '"%{ {{{1\nlet x=1',
        '"""%{ {{{1\nlet x=1',
        '"""%{ "bad\\q\nlet x=1',
        '"%{ "bad\\q" }"\nlet x=1',
        '$ hi %{ "bad\\q" }\nlet x=1',
        '"""%{ "bad\\q" }"""\nlet x=1',
        "$ hi %{ {{{1\nlet x=1",
        "$\n  hi %{ {{{1\nlet x=1",
        '"%{ {{{1\nlet x=1\n}',
        '"""%{ {{{1\nlet x=1\n}',
    ],
)
def test_aborted_templates_do_not_leak_states_into_recovered_code(source: str) -> None:
    actual = _project(source, evaluate(AGL_GRAMMAR, source))
    expected = _project(source, lexical_spans(source))
    start = source.index("let")
    assert actual[start:] == expected[start:]
    _assert_conformance(_comparison("recovery", source))
    assert (
        not set(range(start, len(source))) & _allowed_differences(source, actual, expected).keys()
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "opening, closing",
    [
        ('"', '"'),
        ("'", "'"),
        ('"""', '"""'),
        ("'''", "'''"),
        ("$ hi ", ""),
        ("$\n  hi ", ""),
    ],
)
def test_holes_track_only_curly_nesting_and_recover_at_line_end(
    newline: str, opening: str, closing: str
) -> None:
    for body, complete in (
        ("{1: 2) }", False),
        ("{1: 2] }", False),
        ("([{1: 2)]}", False),
        ("{[1) } (", False),
        ("{1: 2) } }", True),
        ("{1: 2] } }", True),
        ("([1 }", True),
        ("([{1: 2)]} }", True),
        ("{[1) } }", True),
        ('{1: "})] %{ {2: 3) } }"} }', True),
        ("{1: '}([ %{ [2 }'} }", True),
    ):
        source = (opening + "%{ " + body + (closing if complete else "") + "\nlet x=1").replace(
            "\n", newline
        )
        case = _comparison("mixed-hole-brackets", source)
        recovered = source.index("let")
        assert case.actual[recovered:] == case.expected[recovered:], source
        assert case.actual[recovered : recovered + 3] == [Category.KEYWORD] * 3, source
        assert not set(range(recovered, len(source))) & case.allowed.keys(), source
        _assert_conformance(case)


@pytest.mark.parametrize("opening", "([{")
@pytest.mark.parametrize("closing", ")]}")
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_root_brackets_keep_all_bracket_depth_across_lines(
    opening: str, closing: str, newline: str
) -> None:
    source = f"{opening}1{newline}{closing} $ hi{newline}let x=1"
    actual = _characters(source, evaluate(AGL_GRAMMAR, source))
    assert actual == _characters(source, lexical_spans(source))
    start = source.index("$")
    assert actual[start : start + 4] == [Category.VERBATIM] * 4


@pytest.mark.parametrize("source", ['"%{ # }" let x=1', '"%{ {1: #} }" let x=1'])
def test_hash_in_interpolation_is_error_not_comment(source: str) -> None:
    assert _characters(source, evaluate(AGL_GRAMMAR, source)) == _characters(
        source, lexical_spans(source)
    )


@pytest.mark.parametrize(
    "source",
    [
        "[$\n  x\n]\nlet x=1",
        "({[$\n  x\n]})\nlet x=1",
        "[ $ true ]\nlet x=1",
        '"%{ $\n  x\n}"\nlet x=1',
        "$ hi %{ $\n  x\n}\nlet x=1",
    ],
)
def test_rejected_bracketed_verbatim_does_not_open_a_payload(source: str) -> None:
    case = _comparison("bracketed-verbatim", source)
    _assert_conformance(case)
    word = "x" if "x\n" in source else "true"
    start = source.index(word)
    assert case.actual[start : start + len(word)] == case.expected[start : start + len(word)]
    assert start not in case.allowed
    recovered = source.index("let")
    assert case.actual[recovered:] == case.expected[recovered:]
    assert not set(range(recovered, len(source))) & case.allowed.keys()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("replacement", [Category.NUMBER, Category.NAME, Category.STRING])
def test_rejected_verbatim_prefix_requires_verbatim_evidence(
    newline: str, replacement: Category
) -> None:
    source = "$" + newline + "let x=1"
    case = _comparison("rejected-verbatim-prefix", source)
    _assert_conformance(case)
    assert case.allowed[0] == "rejected-verbatim-structure"
    case.actual[0] = replacement
    assert 0 not in _allowed_differences(source, case.actual, case.expected)


@pytest.mark.parametrize(
    "source, spelling, replacement",
    [
        ("$\nlet x=1", "\n", Category.NUMBER),
        ("$\n ", " ", Category.NUMBER),
        ('$ hi %{ "bad\\q" }\nlet x=1', "\\q", Category.VERBATIM),
        ("$\n  hi %{ # }\n x\nlet x=1", "#", Category.TEXT),
        ("$\n  hi %{1}\n x\nlet x=1", "%{", Category.VERBATIM),
        ("$\n  hi %{1}\n x\nlet x=1", "1}", Category.VERBATIM),
        ("$\n  hi\n x\nlet x=1", "hi", Category.TEXT),
        ("$\n  hi\n x\nlet x=1", "hi", Category.KEYWORD),
        ("$\n  hi\n x\nlet x=1", "x\n", Category.NUMBER),
        ("$\nlet x=1", "let", Category.VERBATIM),
        ("[$\n  x\n]\nlet x=1", "x\n", Category.VERBATIM),
    ],
)
def test_rejected_verbatim_extent_keeps_payload_and_recovery_categories_strict(
    source: str, spelling: str, replacement: Category
) -> None:
    case = _comparison("rejected-verbatim-mutation", source)
    _assert_conformance(case)
    index = source.index(spelling)
    assert case.actual[index] != replacement
    case.actual[index] = replacement
    assert index not in _allowed_differences(source, case.actual, case.expected)


def test_verbatim_trailing_blank_line_indentation_is_trivia() -> None:
    source = "$\n  hello\n  \nlet x=1"
    actual = _characters(source, evaluate(AGL_GRAMMAR, source))
    start = source.index("  \n")
    assert actual[start : start + 3] == [Category.TEXT] * 3


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_cross_line_builtin_call_has_only_a_name_spelling_divergence(newline: str) -> None:
    source = f"(print{newline} (1))"
    assert isinstance(parse_program(source).body.items[0], Call)
    actual = _project(source, evaluate(AGL_GRAMMAR, source))
    expected = _project(source, lexical_spans(source))
    allowed = _allowed_differences(source, actual, expected)
    assert allowed == dict.fromkeys(range(1, 6), "cross-line-builtin-call")
    assert all(a == e or index in allowed for index, (a, e) in enumerate(zip(actual, expected)))


@pytest.mark.parametrize(
    "source",
    [
        "let x = 123.45 + ?1 and true",
        'import std/text\n@doc("abc") record Box\n  value: int',
        '"abc\\n%{ {1: print(2)} }${HOME}"',
        "'abc\\t%{ {1: print(2)} }'",
        '"""abc\ndef %{ {1: print(2)} }"""',
        "$ hello %{ {1: print(2)} }\nlet x=1",
        '"%{ "bad\\q" }"\nlet x=1',
        '$ hi %{ "bad\\q" }\nlet x=1',
        "[$\n  x\n]\nlet x=1",
        "[$\n  x\nlet x=1",
        "$\n  hi\n  \n  bye\nlet x=1",
        "def f() =\n  let x = $\n    hi %{value}\n  print(1)",
        '"""bad\\q\nlet x=1',
        '"%{ {{{1\nlet x=1\n}',
        '"%{ # }" let x=1',
        '"""%{ {1: 2) }\nlet x=1',
        '"""%{ {1: 2] }\nlet x=1',
        '"%{ ([1 }"\nlet x=1',
        "$ hi %{ ([{1: 2)]} }\nlet x=1",
        '"bad\\uD800"\nlet x=1',
        '"bad\\u12z4"\nlet x=1',
        "'bad\\q'\nlet x=1",
        '"unfinished\\',
        '"${bad name} %{x}"',
        "αname 𐐀name + 😀 ++ ≤ ?12 １２ @ () [] {} # comment",
    ],
)
def test_core_constructs_and_malformed_prefixes_are_total_and_conform(source: str) -> None:
    for end in range(len(source) + 1):
        _assert_conformance(_comparison(f"prefix-{end}", source[:end]))


def _comparison(name: str, source: str) -> Comparison:
    actual = _project(source, evaluate(AGL_GRAMMAR, source))
    expected = _project(source, lexical_spans(source))
    return Comparison(
        name, source, actual, expected, _allowed_differences(source, actual, expected)
    )


@pytest.mark.parametrize("cases", lexical_corpus_batches(), ids=lambda cases: cases[0][0])
def test_full_corpus_conformance(cases: tuple[tuple[str, str], ...]) -> None:
    for case in cases:
        comparison = _comparison(*case)
        _assert_conformance(comparison)


def _assert_conformance(case: Comparison) -> None:
    unexpected = [
        (index, case.source[max(0, index - 20) : index + 30], expected, actual)
        for index, (actual, expected) in enumerate(zip(case.actual, case.expected, strict=True))
        if actual != expected and index not in case.allowed
    ]
    assert not unexpected, (case.name, unexpected[:10])


DIVERGENCE_WITNESSES = [
    ("literal-line-delimiters", '"""one\ntwo"""'),
    ("relative-verbatim-dedent", "def f() =\n  let x = $\n    hello\n  print(1)"),
    ("invalid-template-escape-recovery", '"bad\\q"\nlet x=1'),
    ("invalid-interpolation-escape-recovery", '"%{ "bad\\q" }"\nlet x=1'),
    ("invalid-interpolation-escape-recovery", '$ hi %{ "bad\\q" }\nlet x=1'),
    ("rejected-template-diagnostics", '"bad\\n\nlet x=1'),
    ("rejected-verbatim-structure", "$\nlet x=1"),
    ("rejected-verbatim-structure", "$\n  hi %{1}\n x\nlet x=1"),
    ("deferred-verbatim-bracket-diagnostic", "[$\n  x\nlet x=1"),
    ("cross-line-builtin-call", "(print\n (1))"),
    ("incomplete-triple-buffering", '"""abc %{ {1: print(2)} }'),
    ("incomplete-triple-buffering", '""" payload %{1 """'),
    ("verbatim-blank-indentation", "$\n  hi\n  \n  bye\nlet x=1"),
]


def test_every_divergence_has_a_handwritten_witness() -> None:
    assert {name for name, _ in DIVERGENCE_WITNESSES} == DIVERGENCES.keys()


@pytest.mark.parametrize("divergence, source", DIVERGENCE_WITNESSES)
def test_divergence_list_is_reasoned_and_not_stale(divergence: str, source: str) -> None:
    assert DIVERGENCES[divergence]
    case = _comparison(divergence, source)
    _assert_conformance(case)
    assert any(
        reason == divergence and case.actual[index] != case.expected[index]
        for index, reason in case.allowed.items()
    ), divergence
