"""Pygments discovery, token vocabulary, and lossless AgL presentation."""

from unittest.mock import patch

import pytest
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.token import (
    Comment,
    Error,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Text,
)

from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.pygments_lexer import AglLexer


@pytest.mark.parametrize("semantic", [True, False])
@pytest.mark.parametrize(
    "source",
    [
        "",
        " \t\r\n",
        "let π = 1\r\nlet other = 1.25\r",
        '"bad\\q"\nlet later = 1\n',
        '"%{missing',
        '"""\n  before%{missing}after\n"""',
        "\x00\ud800\u200b",
        "ask $\n  hello%{missing}\n",
    ],
)
def test_original_text_and_offsets(source: str, semantic: bool) -> None:
    tokens = list(AglLexer(semantic=semantic).get_tokens_unprocessed(source))
    assert "".join(text for _, _, text in tokens) == source
    cursor = 0
    for offset, _, text in tokens:
        assert offset == cursor
        assert text
        cursor += len(text)


@pytest.mark.parametrize(
    ("source", "word", "token"),
    [
        ("let x = 1 # note", "# note", Comment.Single),
        ("let x = true", "let", Keyword),
        ("let x = true", "true", Keyword.Constant),
        ("true and false", "and", Operator.Word),
        ("record Box\n  x: int\n", "Box", Keyword.Type),
        ("let value = Some(1)\n", "Some", Name.Class),
        ("def f(x: int) = x\n", "f", Name.Function),
        ("print(1)\n", "print", Name.Builtin),
        ("import std/text\n", "std/text", Name.Namespace),
        ('@doc("note")\nlet x = 1\n', "doc", Name.Decorator),
        ("def f(x: int) = x\n", "x", Name.Variable),
        ("let value = 1\n", "value", Name.Variable),
        ("missing", "missing", Name),
        ('"hello"', "hello", String),
        ('"a\\n"', "\\n", String.Escape),
        ('"%{missing}"', "%{", String.Interpol),
        ("ask $ hello", "hello", String.Other),
        ('"%${HOME}"', "%${HOME}", String.Interpol),
        ("123", "123", Number.Integer),
        ("1.25", "1.25", Number.Float),
        ("x + y", "+", Operator),
        ("f(x)", "(", Punctuation),
        ("add(?, ?2)", "?2", Name.Variable),
        ("let x = \u200bbad", "\u200b", Error),
        (" \t", " ", Text),
    ],
)
def test_category_mapping(source: str, word: str, token: object) -> None:
    start = source.rindex(word)
    assert any(
        offset <= start < offset + len(text) and kind == token
        for offset, kind, text in AglLexer().get_tokens_unprocessed(source)
    )


@pytest.mark.parametrize("semantic", [True, False, "false"])
def test_context_is_hidden_and_seeds_names(semantic: bool | str) -> None:
    source = "let p: Point = Point(1)\n"
    lexer = AglLexer(context="record Point\n  x: int", semantic=semantic)
    tokens = list(lexer.get_tokens_unprocessed(source))
    assert "".join(text for _, _, text in tokens) == source
    assert [kind for _, kind, text in tokens if text == "Point"] == [
        Keyword.Type,
        Name.Class,
    ]


@pytest.mark.parametrize("semantic", [False, "false", 0])
def test_semantic_can_be_disabled(semantic: bool | str | int) -> None:
    source = "def f(x: int) = x\nlet value = f(1)\n"
    tokens = list(AglLexer(semantic=semantic).get_tokens_unprocessed(source))
    assert all(kind == Name for _, kind, text in tokens if text in {"f", "x", "value"})
    assert any(kind == Name.Function for _, kind, _ in AglLexer().get_tokens_unprocessed(source))


def test_adapter_fills_unclassified_gaps() -> None:
    with patch(
        "agnette.agl.highlight.pygments_lexer.semantic_spans",
        return_value=[(1, 2, Category.NAME)],
    ):
        assert list(AglLexer().get_tokens_unprocessed(" x ")) == [
            (0, Text, " "),
            (1, Name, "x"),
            (2, Text, " "),
        ]


def test_discovery_and_html_token_classes() -> None:
    lexer = get_lexer_by_name("agl")
    assert isinstance(lexer, AglLexer)
    assert lexer.name == "AgL"
    assert lexer.aliases == ["agl"]
    assert lexer.filenames == ["*.agl"]
    assert lexer.mimetypes == ["text/x-agl"]
    html = highlight('let x: int = true\n"%{x}"\n\u200b', lexer, HtmlFormatter())
    for css_class in ("k", "kt", "kc", "si", "err"):
        assert f'class="{css_class}"' in html
