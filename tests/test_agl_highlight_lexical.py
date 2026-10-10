"""UI-free lexical categories, source partitions, and recovery tails."""

from __future__ import annotations

import itertools
from unittest.mock import patch

import pytest
from lark.lexer import Token

from agnette.agl.highlight import Category, Span, lexical_spans
from agnette.agl.highlight.lexical import open_string_start, repl_spans
from agnette.agl.lexer import tokenize_recover
from tests._agl_corpus import lexical_corpus


def _partition(source: str, spans: list[Span]) -> None:
    cursor = 0
    for start, end, category in spans:
        assert start == cursor
        assert start < end <= len(source)
        assert isinstance(category, Category)
        cursor = end
    assert cursor == len(source)


@pytest.mark.parametrize(
    ("source", "word", "category"),
    [
        ("let x = true", "let", Category.KEYWORD),
        ("true false null", "false", Category.KEYWORD_CONSTANT),
        ("x and y", "and", Category.OPERATOR_WORD),
        ("let and = 1", "and", Category.NAME),
        ("record Box", "Box", Category.TYPE),
        ("enum A | A", "A", Category.TYPE),
        ("enum A | B", "B", Category.CONSTRUCTOR),
        ("type Id = int", "Id", Category.TYPE),
        ("exception Boom", "Boom", Category.TYPE),
        ("infixl |> at 5", "at", Category.KEYWORD),
        ("let at = 1", "at", Category.NAME),
        ('@doc("x") def f() = 1', "doc", Category.ATTRIBUTE),
        ("@", "@", Category.OPERATOR),
        ("@ 1", "@", Category.OPERATOR),
        ("import std/text", "std/text", Category.MODULE),
        ("foo/bar::baz", "foo/bar::", Category.MODULE),
        ("import std/text/*", "/*", Category.OPERATOR),
        ("print(1)", "print", Category.BUILTIN_CALL),
        ("let print = f", "print", Category.NAME),
        ('"a\\n"', "\\n", Category.STRING_ESCAPE),
        ('"%{x}"', "%{", Category.INTERPOLATION),
        ('"${HOME}"', "${HOME}", Category.ENV_HOLE),
        ("ask $ hello", "hello", Category.VERBATIM),
        ("let x = ask $   ", "$   ", Category.VERBATIM),
        ('"""unfinished', '"""unfinished', Category.STRING),
        ("1.25", "1.25", Category.NUMBER),
        ("add(?, ?2)", "?2", Category.PLACEHOLDER),
        ("x + y", "+", Category.OPERATOR),
        ("f(x)", "(", Category.PUNCTUATION),
        ("let x = 1 # note", "# note", Category.COMMENT),
        ("let x = \u200bbad", "\u200b", Category.ERROR),
    ],
)
def test_construct_categories(source: str, word: str, category: Category) -> None:
    spans = lexical_spans(source)
    _partition(source, spans)
    start = source.index(word)
    assert any(a <= start < b and c == category for a, b, c in spans)


def test_nested_literal_modes_keep_strings_distinct_from_verbatim() -> None:
    source = 'ask $ hi %{"hello\\n"} bye'
    spans = lexical_spans(source)
    _partition(source, spans)
    for word, expected in (
        ("hi", Category.VERBATIM),
        ("hello", Category.STRING),
        ("\\n", Category.STRING_ESCAPE),
        ("bye", Category.VERBATIM),
    ):
        start = source.index(word)
        assert any(a <= start < b and category == expected for a, b, category in spans)


@pytest.mark.parametrize(
    "source",
    [
        '"""\nabc\n"""',
        '"""\n  abc%{x}def\n  ghi\n"""',
        '"""\n  abc%{"nested\\n%{x}tail"}def\n  ghi\\t\n"""',
        '"""\r\n  abc${HOME}def\r\n  ghi\r\n"""',
        "ask $\n  abc%{x}def\n  ghi\n",
    ],
)
def test_raw_literal_coverage_survives_dedent_and_nested_holes(source: str) -> None:
    spans = lexical_spans(source)
    _partition(source, spans)
    categories = [category for a, b, category in spans for _ in source[a:b]]
    for word in ("abc", "def", "ghi"):
        if word in source:
            start = source.index(word)
            expected = Category.VERBATIM if source.startswith("ask") else Category.STRING
            assert categories[start : start + len(word)] == [expected] * len(word)
    if not source.startswith("ask"):
        assert Category.TEXT not in categories[: source.rindex('"""') + 3]
    if "nested" in source:
        start = source.index("nested")
        assert categories[start : start + 6] == [Category.STRING] * 6
        start = source.index("\\n")
        assert categories[start : start + 2] == [Category.STRING_ESCAPE] * 2
    if "%{x}" in source:
        start = source.index("%{x}")
        assert categories[start : start + 4] == [
            Category.INTERPOLATION,
            Category.INTERPOLATION,
            Category.NAME,
            Category.INTERPOLATION,
        ]


def test_abandoned_nested_literal_escape_is_recovered() -> None:
    source = "\"%{'\\"
    spans = lexical_spans(source)
    _partition(source, spans)
    assert spans[-1] == (4, 5, Category.ERROR)


def test_interpolation_whitespace_is_not_literal_background() -> None:
    source = '"""\n a%{ x + "inner" }tail\n"""'
    spans = lexical_spans(source)
    categories = [category for a, b, category in spans for _ in source[a:b]]
    assert categories[source.index(" x")] == Category.TEXT
    assert categories[source.index("tail")] == Category.STRING


def test_known_names_and_buffer_declarations() -> None:
    source = "record Box\n  x: int\nlet b: Box = Box::[int](1)\nwrap(1)"
    spans = lexical_spans(source, frozenset({"Box"}), frozenset({"Box", "wrap"}))
    _partition(source, spans)
    names = [category for a, b, category in spans if source[a:b] == "Box"]
    assert names == [Category.TYPE, Category.TYPE, Category.CONSTRUCTOR]
    assert any(source[a:b] == "wrap" and c == Category.CONSTRUCTOR for a, b, c in spans)


@pytest.mark.parametrize(
    "source",
    [
        "",
        " \t\n",
        "let x = 1\r\nlet y = 2\r",
        '"a%{x}b%{y}"',
        '"${A}%{x}${B}"',
        '"""\n  a%{x}b\n  c\n"""',
        '"""\n  %{x}\n  %{y}\n"""',
        "ask $\n  a%{x}b\n  c\n",
        '"%{x',
        '"bad\\q"\nlet y = 1',
        "\ud800",
        "a/ b",
        "\x00",
        '@doc("x"\nenum E\n  | A()\n  | B()',
        'enum E\n  | @doc(join("a", "b")) Ok(v: int)',
        "scope a::b\n  let x = 1\nend a::b",
        "infixl |>\n1 + 2",
    ],
)
def test_partitions_including_nonmonotonic_template_tokens(source: str) -> None:
    _partition(source, lexical_spans(source))


def test_totality_on_short_malformed_sources() -> None:
    for chars in itertools.product(('"', "'", "$", "\\", "%", "{", "}", "\n", "\u200b"), repeat=3):
        source = "".join(chars)
        _partition(source, lexical_spans(source))


def test_corpus_partitions() -> None:
    for _name, source in lexical_corpus():
        _partition(source, lexical_spans(source))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('let x = "le', 8),
        ("let x = 'le", 8),
        ('"""a\n\nle', 0),
        ('# "ignored"\nle', None),
        ('# comment\r"le', None),
        ('x\r\n"le', 3),
        ('"bad\\\n#"\nle', None),
        ('"""\\q""" le', None),
        ('"closed" le', None),
        ('"escaped \\" le', 0),
        ('"bad\\q" le', None),
        ('ask $ "le', 6),
        ('"bad\n#"\nle', None),
        ('"bad\n# still literal\nle', 0),
        ('"%{\'unfinished}" le', None),
        ('"\\q\\\\" le', None),
        ('\\"le', 1),
        ('"%{x}unfinished', 0),
        ("ask $ le", None),
        ('"bad\nle', 0),
        ('"bad\\', 0),
        ("ask $ %{'''unfinished", 8),
        ("\"\"\"%{'''unfinished", 0),
        ("\"%{'x'}\"", None),
    ],
)
def test_recovery_based_open_string_check(source: str, expected: int | None) -> None:
    assert open_string_start(source) == expected


def test_positionless_synthetic_tokens_leave_the_source_intact() -> None:
    source = "let x = 1 + foo"
    expected = lexical_spans(source)
    tokens, errors = tokenize_recover(source)
    with patch(
        "agnette.agl.highlight.lexical.tokenize_recover",
        return_value=([Token("NAME", "ghost"), *tokens], errors),
    ):
        assert lexical_spans(source) == expected
    with patch(
        "agnette.agl.highlight.lexical.tokenize_recover", return_value=([Token("NAME", "x")], [])
    ):
        assert lexical_spans("x") == [(0, 1, Category.TEXT)]
    with patch(
        "agnette.agl.highlight.lexical.tokenize_recover",
        return_value=([Token("SYNTHETIC", "x", start_pos=0, end_pos=1)], []),
    ):
        assert lexical_spans("x") == [(0, 1, Category.TEXT)]
    with patch(
        "agnette.agl.highlight.lexical.tokenize_recover",
        return_value=([Token("TEMPLATE_START", "", start_pos=0, end_pos=0)], []),
    ):
        assert lexical_spans("x") == [(0, 1, Category.TEXT)]
    with patch(
        "agnette.agl.highlight.lexical.tokenize_recover", return_value=([Token("NAME", "x")], [])
    ):
        assert repl_spans("x") == []
