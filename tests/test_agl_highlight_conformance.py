"""AgL grammar parity against exact lexical recognition, not semantic resolution."""

import re
from dataclasses import dataclass

import pytest

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category, Span
from agnette.agl.highlight.grammar_eval import evaluate
from agnette.agl.highlight.lexical import lexical_spans
from agnette.agl.keywords import KEYWORDS, OPERATOR_SOFT_KEYWORDS, SOFT_KEYWORDS
from agnette.agl.lexer import tokenize_recover
from agnette.agl.lexer.scanner import scan
from agnette.agl.parser.parser import parse_program
from agnette.agl.scope.symbols import BUILTIN_CALL_NAMES
from agnette.agl.syntax import BUILTIN_TYPE_NAMES, Call
from agnette.agl.value_syntax.errors import ValueSyntaxError
from agnette.agl.value_syntax.lexical import decode_escape
from agnette.util.text import normalize_newlines
from tests._agl_corpus import lexical_corpus


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


def _project(source: str, spans: list[Span]) -> list[Category]:
    result = _characters(source, spans)
    text, offsets = _normalized_offsets(source)
    tokens, _ = tokenize_recover(text)
    significant = [
        token for token in tokens if token.type not in {"_INDENT", "_DEDENT", "_NEWLINE"}
    ]
    for index, token in enumerate(significant):
        start, end = offsets[token.start_pos], offsets[token.end_pos]
        spelling = text[token.start_pos : token.end_pos]
        next_type = significant[index + 1].type if index + 1 < len(significant) else None
        category: Category | None = None
        # TASK_11: remove this temporary collapse of contextual constructs.
        if token.type in {"MODPATH", "MODQUAL"}:
            raw = list(scan(spelling))
            for raw_index, part in enumerate(raw):
                if part.type == "NAME":
                    following = raw[raw_index + 1].type if raw_index + 1 < len(raw) else next_type
                    plain = _plain_name(str(part), following)
                elif part.type in KEYWORDS:
                    plain = Category.KEYWORD
                elif part.type == "SLASH":
                    plain = Category.OPERATOR
                else:
                    assert part.type == "DCOLON", part
                    plain = Category.OPERATOR
                a, b = (
                    offsets[token.start_pos + part.start_pos],
                    offsets[token.start_pos + part.end_pos],
                )
                result[a:b] = [plain] * (b - a)
        elif token.type == "WILDCARD":
            category = Category.NAME
        elif token.type == "AT":
            category = Category.OPERATOR
        elif spelling in SOFT_KEYWORDS and token.type != "STRING_FRAGMENT":
            category = (
                Category.OPERATOR_WORD if spelling in OPERATOR_SOFT_KEYWORDS else Category.NAME
            )
        elif token.type in {"NAME", "OP_NAME"} and result[start] in {
            Category.TYPE,
            Category.CONSTRUCTOR,
            Category.ATTRIBUTE,
        }:
            category = _plain_name(spelling, next_type)
        # Keep semantic-only role collapse when removing the contextual block above.
        elif token.type in {"NAME", "OP_NAME"} and result[start] in {
            Category.FUNCTION,
            Category.LOCAL,
            Category.PARAMETER,
        }:
            category = _plain_name(spelling, next_type)
        elif spelling == "at" and result[start] == Category.KEYWORD:
            category = Category.NAME
        if category is not None:
            result[start:end] = [category] * (end - start)
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
    ],
)
def test_temporary_context_projection_retains_plain_lexical_structure(source: str) -> None:
    assert _characters(source, evaluate(AGL_GRAMMAR, source)) == _project(
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
    actual = _characters(source, evaluate(AGL_GRAMMAR, source))
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
    actual = _characters(source, evaluate(AGL_GRAMMAR, source))
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
    for blank in re.finditer(r"(?:^|(?<=[\r\n]))[ \t]+(?=[\r\n]|$)", source):
        for index in range(blank.start(), blank.end()):
            if actual[index] == Category.TEXT and expected[index] == Category.VERBATIM:
                allowed[index] = "verbatim-blank-indentation"
    text, offsets = _normalized_offsets(source)
    tokens, errors = tokenize_recover(text)
    invalid_escapes: set[int] = set()
    for error in errors:
        position = error.span.start_offset
        if text[position : position + 1] == "\\":
            try:
                decode_escape(text, position)
            except ValueSyntaxError as invalid:
                if str(error) == invalid.message:
                    invalid_escapes.add(position)
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
    for token in tokens:
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
            holes: list[int] = []
            for index in range(start, end):
                if actual[index] != Category.INTERPOLATION:
                    continue
                if source.startswith("%{", index):
                    holes.append(index)
                elif source[index] == "}" and holes:
                    opening = holes.pop()
                    for position in range(opening, index + 1):
                        if expected[position] == Category.STRING:
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
                for index in range(start, end):
                    if expected[index] == Category.ERROR:
                        allowed[index] = reason
                # Recovery retains real closing delimiters even after an error.
                for closing in tokens:
                    if (
                        closing.type == "TEMPLATE_END"
                        and token.start_pos <= closing.start_pos < closing.end_pos <= token.end_pos
                    ):
                        for index in range(offsets[closing.start_pos], offsets[closing.end_pos]):
                            if (
                                actual[index] == Category.ERROR
                                and expected[index] == Category.STRING
                            ):
                                allowed[index] = reason
            elif spelling.startswith("$"):
                for index in range(start, end):
                    if expected[index] == Category.ERROR or actual[index] == Category.VERBATIM:
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
    actual = _characters(source, evaluate(AGL_GRAMMAR, source))
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
    assert _characters(source, evaluate(AGL_GRAMMAR, source)) == expanded


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_relative_verbatim_divergence_uses_original_newline_offsets(newline: str) -> None:
    source = newline.join(["def f() =", "  let x = $", "    hi %{value}", "  print(1)"])
    case = _comparison("relative-newlines", source)
    _assert_conformance(case)
    assert "relative-verbatim-dedent" in case.allowed.values()
    assert source.index("value") not in case.allowed


@pytest.mark.parametrize("source", ['"bad\\n', '"bad\\n\nlet x=1', "'bad\\t"])
def test_valid_escapes_in_unterminated_strings_are_not_invalid_escape_divergences(
    source: str,
) -> None:
    actual = _characters(source, evaluate(AGL_GRAMMAR, source))
    expected = _project(source, lexical_spans(source))
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
    actual = _characters(source, evaluate(AGL_GRAMMAR, source))
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
@pytest.mark.parametrize(
    "body, complete",
    [
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
    ],
)
def test_holes_track_only_curly_nesting_and_recover_at_line_end(
    newline: str, opening: str, closing: str, body: str, complete: bool
) -> None:
    source = (opening + "%{ " + body + (closing if complete else "") + "\nlet x=1").replace(
        "\n", newline
    )
    case = _comparison("mixed-hole-brackets", source)
    recovered = source.index("let")
    assert case.actual[recovered:] == case.expected[recovered:]
    assert case.actual[recovered : recovered + 3] == [Category.KEYWORD] * 3
    assert not set(range(recovered, len(source))) & case.allowed.keys()
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


def test_verbatim_trailing_blank_line_indentation_is_trivia() -> None:
    source = "$\n  hello\n  \nlet x=1"
    actual = _characters(source, evaluate(AGL_GRAMMAR, source))
    start = source.index("  \n")
    assert actual[start : start + 3] == [Category.TEXT] * 3


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_cross_line_builtin_call_has_only_a_name_spelling_divergence(newline: str) -> None:
    source = f"(print{newline} (1))"
    assert isinstance(parse_program(source).body.items[0], Call)
    actual = _characters(source, evaluate(AGL_GRAMMAR, source))
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
    actual = _characters(source, evaluate(AGL_GRAMMAR, source))
    expected = _project(source, lexical_spans(source))
    return Comparison(
        name, source, actual, expected, _allowed_differences(source, actual, expected)
    )


@pytest.mark.parametrize("case", lexical_corpus(), ids=lambda case: case[0])
def test_full_corpus_conformance(case: tuple[str, str]) -> None:
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
    ("deferred-verbatim-bracket-diagnostic", "[$\n  x\nlet x=1"),
    ("cross-line-builtin-call", "(print\n (1))"),
    ("incomplete-triple-buffering", '"""abc %{ {1: print(2)} }'),
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
