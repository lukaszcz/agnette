"""Scanner recovery contracts through the public lexer API.

Source coverage means an ordered interval projection: ignore synthetic layout
positions, clip diagnostic fragment overlaps to the next token's start, and
include unclaimed source slices (trivia and trimmed literal margins). Empty
intervals, including zero-width template closers, own no source characters.
Strict tokens deliberately retain their original spans, not a literal partition.
"""

from pathlib import Path

import pytest
from lark.lexer import Token

from agnette.agl.lexer import (
    IncompleteInputError,
    LexError,
    lex_comment_spans,
    tokenize,
    tokenize_recover,
)
from agnette.agl.lexer.tokens import ERROR
from agnette.util.text import normalize_newlines

REJECTED = [
    '"bad\\q"\nlater',  # escape conversion
    '"unfinished',  # string EOF
    '"unfinished\nlater',  # single-line string newline
    '"%{unfinished',  # interpolation EOF
    '"%{unfinished\nlater',  # interpolation newline
    '("%{) $ text}"\nlater',  # outer tracking precedes interpolation tracking
    "('%{) $ text}'\nlater",
    "))$ %{( $ text}}",  # buffered payload holes retain deferred-error precedence
    "$\n    first\n  bad\nlater",  # under-indented payload
    "$\nlater",  # empty block
    "\x01 later",  # unexpected character
    "before\n\ud800\nlater",  # source Unicode preflight
    "($ text)\nlater",  # deferred bracket diagnostic at completion
    "($ text \x01\nlater",  # deferred diagnostic wins over a later scan error
]


def positions(tokens: list[Token]) -> list[tuple[object, ...]]:
    return [
        (t.type, str(t), t.start_pos, t.end_pos, t.line, t.column, t.end_line, t.end_column)
        for t in tokens
    ]


def assert_source_coverage(source: str, tokens: list[Token]) -> None:
    """Project tokens and their unclaimed slices into an exact source partition."""
    source = normalize_newlines(source)
    real = [
        t
        for t in tokens
        if t.type not in {"_INDENT", "_DEDENT"}
        and not (t.type == "done" and source[t.start_pos : t.end_pos] != "done")
    ]
    # Scanner order is grammatical, not necessarily source-offset order.
    real.sort(key=lambda token: token.start_pos)
    cursor = 0
    slices: list[str] = []
    for index, token in enumerate(real):
        assert 0 <= token.start_pos <= token.end_pos <= len(source)
        start = max(cursor, token.start_pos)
        slices.append(source[cursor:start])
        end = (
            min(token.end_pos, real[index + 1].start_pos)
            if index + 1 < len(real)
            else token.end_pos
        )
        end = max(start, end)
        slices.append(source[start:end])
        cursor = end
    slices.append(source[cursor:])
    assert "".join(slices) == source


@pytest.mark.parametrize("source", REJECTED)
def test_scanner_error_recovery(source: str) -> None:
    with pytest.raises(LexError) as strict:
        list(tokenize(source))
    tokens, errors = tokenize_recover(source)
    assert str(errors[0]) == str(strict.value)
    assert errors[0].span == strict.value.span
    assert all(
        any(t.type == ERROR and t.start_pos <= error.span.start_offset <= t.end_pos for t in tokens)
        for error in errors
    )
    assert_source_coverage(source, tokens)
    if source.endswith("later"):
        assert any(t.type == "NAME" and str(t) == "later" for t in tokens)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "# only comment\n",
        "let x = 1\n    x\nnext",
        '"hello %{name} ${HOME}"',
        '"""\n    hello %{name}\n    world\n"""',
        "$\n    hello %{name}\nnext",
        "import foo/bar\nfoo/bar::name",
        "let x = 1\r\nnext\r",
        "do\n    work()",
        "($ text",  # an unclosed bracket belongs to the parser
        '))"%{( $ text}"}',  # buffering must not delay outer bracket tracking
        "))'%{( $ text}'}",
        '("""%{) $ text}"""\nlater',  # holes buffered by triple-template scanning
        '"%{(}" $ text)',  # buffered tokens must not be tracked again on replay
        '$ %{"""\nhi"""}\nlater',  # payload collection rewinds after multiline holes
        "$ %{'''\nhi'''}\nlater",
        '$\n    %{"""\nhi"""}\nlater',
        '$\n    %{"""\n    hi"""}\n    still payload\nlater',
        "$\n    %{\"\"\"%{'''\n    hi'''}\"\"\"}\n    still payload\nlater",
        "$\n    %{\"%{'''\n    hi'''}\"}\n    still payload\nlater",
        "\"%{'''\nhi'''}\"\nlater",  # single-template buffering
        "\"\"\"%{'''\nhi'''}\"\"\"\nlater",  # triple-template buffering
    ],
)
def test_strict_accepted_tokens_are_identical(source: str) -> None:
    strict = list(tokenize(source))
    tokens, errors = tokenize_recover(source)
    assert not errors
    assert positions(tokens) == positions(strict)
    assert_source_coverage(source, tokens)


def test_surrogate_insertion_preserves_multiline_payload_order() -> None:
    source = '# \ud800\n$ %{"""\nhi"""}\nlater'
    strict = list(tokenize(source.replace("\ud800", " ")))
    tokens, errors = tokenize_recover(source)
    assert len(errors) == 1
    assert positions([t for t in tokens if t.type != ERROR]) == positions(strict)
    assert_source_coverage(source, tokens)


@pytest.mark.parametrize("source", ["\ud800", "before \ud800", "# \ud800"])
def test_surrogate_insertion_at_eof(source: str) -> None:
    strict = list(tokenize(source.replace("\ud800", " ")))
    tokens, errors = tokenize_recover(source)
    assert len(errors) == 1
    assert tokens[-1].type == ERROR
    assert positions(tokens[:-1]) == positions(strict)
    assert_source_coverage(source, tokens)


def test_accepted_source_corpus() -> None:
    root = Path(__file__).resolve().parents[1]
    for directory in [root / "tests/agl", root / "packages/stdlib/src"]:
        for path in directory.rglob("*.agl"):
            try:
                source = path.read_text()
                strict = list(tokenize(source))
            except (UnicodeDecodeError, LexError):
                continue
            tokens, errors = tokenize_recover(source)
            assert not errors, path
            assert positions(tokens) == positions(strict), path


@pytest.mark.parametrize("source", ['"""unfinished', "$", "$   "])
def test_incomplete_input_remains_strict(source: str) -> None:
    with pytest.raises(IncompleteInputError):
        tokenize_recover(source)


def test_unexpected_character_inside_interpolation_skips_only_it() -> None:
    source = '"%{\x01 name}"\nlater'
    tokens, errors = tokenize_recover(source)
    assert len(errors) == 1
    assert [(t.type, str(t)) for t in tokens if t.type in {ERROR, "NAME"}] == [
        (ERROR, "\x01"),
        ("NAME", "name"),
        ("NAME", "later"),
    ]
    assert_source_coverage(source, tokens)


@pytest.mark.parametrize("source", ['"bad\\\nlater', '"bad\\u12\nlater'])
def test_escape_errors_do_not_consume_following_line(source: str) -> None:
    tokens, errors = tokenize_recover(source)
    assert len(errors) == 1
    assert str(tokens[-1]) == "later"


@pytest.mark.parametrize("indent", ["\t\t", " \t", "\t ", "    "])
@pytest.mark.parametrize("quote", ['"', "'", '"""'])
@pytest.mark.parametrize("escape", ["\\", "\\u12"])
def test_escape_newline_recovery_preserves_indentation_columns(
    indent: str, quote: str, escape: str
) -> None:
    source = f"x\n{indent}{quote}{escape}\nlater"
    with pytest.raises(LexError) as strict:
        list(tokenize(source))
    tokens, errors = tokenize_recover(source)
    assert errors[0].span == strict.value.span
    error_token = next(t for t in tokens if t.type == ERROR)
    start_col = len(indent.expandtabs(4)) + 1
    end_col = start_col + len(quote + escape)
    assert (error_token.line, error_token.column) == (2, start_col)
    assert (error_token.end_line, error_token.end_column) == (2, end_col)
    newline = next(t for t in tokens if t.type == "_NEWLINE")
    assert (newline.line, newline.column) == (2, end_col)
    assert (tokens[-1].line, tokens[-1].column, str(tokens[-1])) == (3, 1, "later")
    assert_source_coverage(source, tokens)


def test_escape_newline_recovery_keeps_literal_tabs_unexpanded() -> None:
    source = 'x\n\t\t"""\n\t\t\\\nlater'
    tokens, errors = tokenize_recover(source)
    assert len(errors) == 1
    error_token = next(t for t in tokens if t.type == ERROR)
    assert (error_token.line, error_token.column) == (2, 9)
    assert (error_token.end_line, error_token.end_column) == (3, 4)
    assert (tokens[-1].line, tokens[-1].column, str(tokens[-1])) == (4, 1, "later")
    assert_source_coverage(source, tokens)


def test_comments_after_errors_are_collected() -> None:
    source = '# before\n`\n"bad\n# after\n$ # payload\n# last'
    assert [source[start:end] for start, end in lex_comment_spans(source)] == [
        "# before",
        "# after",
        "# last",
    ]


@pytest.mark.parametrize("tail", ['"""unfinished', "$"])
def test_comment_prefix_survives_incomplete_input(tail: str) -> None:
    source = "# before\n" + tail
    assert lex_comment_spans(source) == [(0, 8)]


@pytest.mark.parametrize(
    "source",
    ["$\n    first\n  bad\n    still payload\n\nlater", "$\n    first\n   bad\n  worse\nlater"],
)
def test_bad_payload_margin_resynchronizes_at_block_dedent(source: str) -> None:
    tokens, errors = tokenize_recover(source)
    assert len(errors) == 1
    assert [(t.type, str(t)) for t in tokens if t.type == "NAME"] == [("NAME", "later")]
    assert_source_coverage(source, tokens)


def test_bad_payload_margin_at_eof() -> None:
    tokens, errors = tokenize_recover("$\n    first\n  bad")
    assert len(errors) == 1
    assert len(tokens) == 1
    assert tokens[0].type == ERROR


@pytest.mark.parametrize("source", ["$\nimport foo/bar", "$\n    text %{x\nimport foo/bar"])
def test_payload_recovery_preserves_next_module_header(source: str) -> None:
    tokens, errors = tokenize_recover(source)
    assert len(errors) == 1
    assert [(t.type, str(t)) for t in tokens[-2:]] == [("IMPORT", "import"), ("MODPATH", "foo/bar")]


def test_multiple_errors_keep_later_tokens() -> None:
    source = '\x01 \x01\n"bad\\q"\n"bad\nlater # comment'
    tokens, errors = tokenize_recover(source)
    assert len(errors) == 4
    assert sum(t.type == ERROR for t in tokens) == 4
    assert str(tokens[-1]) == "later"
    assert_source_coverage(source, tokens)


@pytest.mark.parametrize(
    "source", ['"""bad\\q\nlater', "$ text %{x\nlater", "$\n    text %{x\nlater"]
)
def test_nested_literal_errors_resynchronize(source: str) -> None:
    with pytest.raises(LexError) as strict:
        list(tokenize(source))
    tokens, errors = tokenize_recover(source)
    assert errors[0].span == strict.value.span
    assert str(tokens[-1]) == "later"
    assert_source_coverage(source, tokens)


@pytest.mark.parametrize(
    "source",
    [
        '$ %{"""\n\\q"""}\nlater',
        "$ %{'''\n\n\\uX'''}\nlater",
        "$ %{\"\"\"%{'''\n\\q'''}\"\"\"}\nlater",
        '$\n    %{"""\n\\q"""}\nlater',  # hole scans past the collected block
        '$\n    %{"""\n    \\q"""}\n    still payload\nlater',
        "$\n    %{\"\"\"%{'''\n\\q'''}\"\"\"}\nlater",
        '$ %{"""\n\\q',
        '$\n    %{"""\n\\q',
    ],
)
def test_multiline_payload_hole_error_covers_diagnostic(source: str) -> None:
    with pytest.raises(LexError) as strict:
        list(tokenize(source))
    tokens, errors = tokenize_recover(source)
    assert len(errors) == 1
    assert errors[0].span == strict.value.span
    assert str(errors[0]) == str(strict.value)
    error_token = next(t for t in tokens if t.type == ERROR)
    assert error_token.start_pos <= errors[0].span.start_offset
    assert error_token.end_pos >= errors[0].span.end_offset
    names = [str(t) for t in tokens if t.type == "NAME"]
    assert names == (["later"] if source.endswith("later") else [])
    assert_source_coverage(source, tokens)


@pytest.mark.parametrize(
    "source", ['"\ud800"\nlater', "# \ud800\nlater", "x\ud800\nlater", "\ud800\udfff\nlater"]
)
def test_surrogates_in_all_scanner_modes(source: str) -> None:
    with pytest.raises(LexError) as strict:
        list(tokenize(source))
    tokens, errors = tokenize_recover(source)
    assert errors[0].span == strict.value.span
    assert any(t.type == ERROR for t in tokens)
    assert str(tokens[-1]) == "later"
    assert_source_coverage(source, tokens)
