"""Markdown fence context, standard highlighting options, and document isolation."""

import re
from unittest.mock import patch
from xml.etree import ElementTree

import pytest
from markdown import Markdown
from pygments.token import Keyword, Name

from agnette.agl.highlight.markdown_ext import (
    ContextPreprocessor,
    _has_code_span_closer,
    formatter,
    validator,
)
from agnette.agl.highlight.pygments_lexer import AglLexer, document_context


def _markdown(custom: bool = True, relaxed_headers: bool = False) -> Markdown:
    fences = [{"name": "agl", "class": "agl", "format": formatter, "validator": validator}]
    return Markdown(
        extensions=(["agl_highlight"] if custom else []) + ["pymdownx.superfences", "attr_list"],
        extension_configs={
            "pymdownx.superfences": {
                "custom_fences": fences if custom else [],
                "relaxed_headers": relaxed_headers,
            }
        },
    )


def _structure(html: str) -> list[tuple[str, dict[str, str]]]:
    root = ElementTree.fromstring(f"<root>{html}</root>")
    return [(node.tag, node.attrib) for node in root.iter() if node.tag != "span"]


@pytest.mark.parametrize("delimiter", ["```", "~~~~"])
@pytest.mark.parametrize("label", ["agl", " agl", "\tagl"])
@pytest.mark.parametrize("prefix", ["", "    ", "> ", "> > "])
def test_context_reaches_lexer_in_containers(delimiter: str, label: str, prefix: str) -> None:
    lead = "- Example\n\n" if prefix == "    " else ""
    lines = [
        "<!-- agl-context: record Point -->",
        "<!-- agl-check: fragment -->",
        "<!-- agl-context:   x: int -->",
        f'{delimiter}{label} hl_lines="1" title="example.agl"',
        "let p: Point = Point(1)",
        delimiter,
    ]
    html = _markdown().convert(lead + "\n".join(prefix + line for line in lines))
    assert "agl-context" not in html
    assert "record" not in html
    assert "agl-check: fragment" in html
    assert '<span class="kt">Point</span>' in html
    assert '<span class="nc">Point</span>' in html
    assert '<span class="hll">' in html
    assert "example.agl" in html


@pytest.mark.parametrize(
    "header",
    [
        'agl hl_lines="1 2" title="example.agl" linenums="3 2 2"',
        'agl {#sample .extra hl_lines="1" title="example.agl" data-note="note"}',
        '{.agl #sample .extra hl_lines="1" title="example.agl" data-note="note"}',
        '{#sample .agl .extra hl_lines="1" title="example.agl"}',
        '.agl hl_lines="1" title="example.agl"',
    ],
)
def test_custom_fence_preserves_default_html_and_options(header: str) -> None:
    source = f"```{header}\nlet x: int = 1\nprint(x)\n```"
    assert _markdown().convert(source) == _markdown(custom=False).convert(source)
    with_context = _markdown().convert("<!-- agl-context: let hidden = 1 -->\n" + source)
    assert with_context == _markdown(custom=False).convert(source)
    assert _structure(with_context) == _structure(_markdown(custom=False).convert(source))


@pytest.mark.parametrize(
    "header",
    [
        "agl title=Example",
        "agl \N{NO-BREAK SPACE}",
        'agl hl_lines="not-a-number"',
        'agl linenums="invalid"',
        'agl unknown="value"',
        'agl title="unterminated',
        'agl {title="Example"} trailing',
        '{.python .agl title="Example"}',
        '{".agl"}',
        "{.agl=x}",
        "agl_context",
    ],
)
@pytest.mark.parametrize("prefix", ["", "> "])
def test_rejected_headers_preserve_authored_text_and_markers(header: str, prefix: str) -> None:
    lines = [
        prefix + "<!-- agl-context: record Point -->",
        prefix + "<!-- agl-check: fragment -->",
        prefix + "```" + header,
        prefix + "Point",
        prefix + "```",
    ]
    renderer = _markdown()
    assert ContextPreprocessor(renderer).run(lines) == lines
    source = "\n".join(lines)
    assert renderer.convert(source) == _markdown(custom=False).convert(source)
    assert "agl_context=" not in renderer.reset().convert(source)


@pytest.mark.parametrize("header", ['{.agl title="broken}', "{.agl =ignored}"])
def test_uninjectable_attributes_preserve_markers(header: str) -> None:
    lines = ["<!-- agl-context: record Point -->", "```" + header, "Point", "```"]
    assert ContextPreprocessor(_markdown()).run(lines) == lines
    source = "\n".join(lines)
    assert _markdown().convert(source) == _markdown(custom=False).convert(source)


def test_rejected_unclosed_fence_does_not_hide_next_context() -> None:
    source = (
        '````agl unknown="value"\n'
        "<!-- agl-context: record Point -->\n"
        "<!-- agl-context:   x: int -->\n"
        "```agl\nlet p: Point = Point(1)\n```"
    )
    html = _markdown().convert(source)
    assert "agl-context" not in html
    assert '<span class="kt">Point</span>' in html
    assert '<span class="nc">Point</span>' in html


@pytest.mark.parametrize("header", ["agl title=Example", 'agl hl_lines="not-a-number"'])
def test_relaxed_header_validation_does_not_discard_context(header: str) -> None:
    lines = ["<!-- agl-context: record Point -->", "```" + header, "Point", "```"]
    renderer = _markdown(relaxed_headers=True)
    assert ContextPreprocessor(renderer).run(lines) == lines
    source = "\n".join(lines)
    assert renderer.convert(source) == _markdown(custom=False, relaxed_headers=True).convert(source)


@pytest.mark.parametrize(
    "prose",
    [
        "Use `<!--` to open a comment.",
        "Use `<!--\ninside the code span\n` as text.",
        "Code `` a ` <!-- ` b `` end.",
        "Escaped \\` tick, then `<!--` in code.",
        "Write \\<!-- literally.",
    ],
)
def test_literal_comment_delimiters_do_not_disable_context(prose: str) -> None:
    source = (
        prose
        + "\n\n<!-- agl-context: record Point -->\n<!-- agl-context:   x: int -->\n"
        + "```agl\nlet p: Point = Point(1)\n```"
    )
    html = _markdown().convert(source)
    assert "agl-context" not in html
    assert '<span class="kt">Point</span>' in html


def test_inline_code_closer_scan_does_not_slice_remaining_lines() -> None:
    class SliceCheckingLines(list[str]):
        def __getitem__(self, index: int | slice) -> str | list[str]:
            if isinstance(index, slice):
                pytest.fail("inline-code scan copied a slice of the document")
            return super().__getitem__(index)

    lines = SliceCheckingLines(["`code`", *["tail"] * 1000])
    assert _has_code_span_closer(lines, 0, 1, 1)


def test_unmatched_inline_code_delimiter_does_not_hide_markdown_comments() -> None:
    source = (
        "Use ` as a literal.\n\n"
        "<!-- agl-context: record Point -->\n<!-- agl-context:   x: int -->\n"
        "```agl\nlet p: Point = Point(1)\n```"
    )
    html = _markdown().convert(source)
    assert "agl-context" not in html
    assert '<span class="kt">Point</span>' in html


@pytest.mark.parametrize(
    "block",
    [
        "    <div>\n    <!-- literal in an indented code block\n\n",
        "<script>\n<!-- literal <div> in raw HTML\n</script>\n\n",
        "<div>\n<section>\n</span>\n<hr>\n<!-- literal in raw HTML\n</div>\n\n",
        "<span>inline HTML <!-- note --></span>\n\n",
    ],
)
def test_comment_delimiters_in_markdown_blocks_do_not_hide_context(block: str) -> None:
    source = (
        block
        + "<!-- agl-context: record Point -->\n<!-- agl-context:   x: int -->\n"
        + "```agl\nlet p: Point = Point(1)\n```"
    )
    html = _markdown().convert(source)
    assert "agl-context" not in html
    assert '<span class="kt">Point</span>' in html


def test_context_is_local_to_fence_and_document() -> None:
    renderer = _markdown()
    fence = "```agl\nlet p: Point = Point(1)\n```"
    contextual = "<!-- agl-context: record Point -->\n<!-- agl-context:   x: int -->\n" + fence
    html = renderer.convert(contextual + "\n\n" + fence)
    assert html.count('<span class="kt">Point</span>') == 1
    assert html.count('<span class="n">Point</span>') == 2
    assert '<span class="kt">Point</span>' not in renderer.reset().convert(fence)
    assert any(
        kind == Name and text == "Point"
        for _, kind, text in AglLexer().get_tokens_unprocessed("Point")
    )


@pytest.mark.parametrize(("preceding", "block"), [("    x", "pre"), ("- item", "ul")])
def test_removing_context_preserves_markdown_block_boundaries(preceding: str, block: str) -> None:
    source = f"{preceding}\n<!-- agl-context: record Point -->\n```agl\nlet p = Point()\n```"
    html = _markdown().convert(source)
    root = ElementTree.fromstring(f"<root>{html}</root>")

    assert [node.tag for node in root] == [block, "div"]
    assert '<span class="nc">Point</span>' in html
    assert "agl_context=" not in html


def test_context_reaches_lexer_with_crlf_newlines() -> None:
    source = (
        "<!-- agl-context: record Point -->\r\n"
        "<!-- agl-context:   x: int -->\r\n"
        "```agl\r\nlet p: Point = Point(1)\r\n```"
    )
    html = _markdown().convert(source)
    assert "agl-context" not in html
    assert '<span class="kt">Point</span>' in html
    assert '<span class="nc">Point</span>' in html


@pytest.mark.parametrize(
    "source",
    [
        "```text\n<!-- agl-context: record Point -->\n```agl\nPoint\n```\n```",
        "<!-- outer\n<!-- agl-context: record Point -->\n```agl\nPoint\n```\n-->\n-->",
        "<!-- agl-context: record Point -->\n\n```agl\nPoint\n```",
        "<!-- agl-context: record Point -->\n```python\nPoint\n```",
    ],
)
def test_unrelated_or_protected_markers_remain_untouched(source: str) -> None:
    assert _markdown().convert(source) == _markdown(custom=False).convert(source)


def test_markers_inside_agl_body_are_displayed_not_consumed() -> None:
    source = '```agl\n"""\n<!-- agl-context: record Point -->\n~~~agl\nPoint\n~~~\n"""\n```'
    assert _markdown().convert(source) == _markdown(custom=False).convert(source)
    assert "agl-context" in _markdown().convert(source)


@pytest.mark.parametrize("option", ['agl_context="%%%"', 'agl_context="_w=="', "agl_context"])
def test_invalid_context_option_does_not_raise(option: str) -> None:
    assert "Point" in _markdown().convert(f"```agl {option}\nPoint\n```")


def test_validator_rejects_boolean_context_option() -> None:
    renderer = _markdown()
    assert not validator("agl", {"agl_context": True}, {}, {}, renderer)


def test_check_only_metadata_and_empty_context() -> None:
    source = "<!-- agl-context: -->\n<!-- agl-check: fragment -->\n```agl\nmissing\n```"
    html = _markdown().convert(source)
    assert "agl-context" not in html
    assert "agl-check" in html
    assert 'class="highlight"' in html


def test_context_payload_preserves_unicode_and_quotes() -> None:
    html = _markdown().convert(
        "<!-- agl-context: record Δ -->\n<!-- agl-context:   x: int -->\n"
        '<!-- agl-context: let note = "hello" -->\n```agl\nlet p: Δ = Δ(1)\n```'
    )
    assert '<span class="kt">Δ</span>' in html
    assert "hello" not in html
    assert "agl_context" not in html


@pytest.mark.parametrize("fail", [False, True])
def test_formatter_restores_enclosing_context_even_on_failure(fail: bool) -> None:
    renderer = _markdown()
    renderer.convert("```agl\n0\n```")
    token = document_context.set("record Outer\n  x: int")
    try:
        assert AglLexer(context="").context.source == ""
        if fail:
            with (
                patch.object(
                    renderer.preprocessors["fenced_code_block"],
                    "highlight",
                    side_effect=RuntimeError,
                ),
                pytest.raises(RuntimeError),
            ):
                formatter("Outer", "agl", "agl", {}, renderer)
        else:
            html = formatter("Outer", "agl", "agl", {}, renderer, classes=[], id_value="", attrs={})
            assert '<span class="n">Outer</span>' in html
        assert any(
            kind == Keyword.Type and text == "Outer"
            for _, kind, text in AglLexer().get_tokens_unprocessed("let x: Outer = missing")
        )
    finally:
        document_context.reset(token)
    assert document_context.get() == ""


def test_nested_quotes_preserve_body_indentation_and_blank_lines() -> None:
    source = (
        "> > <!-- agl-context: record Point -->\n"
        "> > <!-- agl-context:   x: int -->\n"
        "> > ~~~ agl\n"
        ">> let p: Point = Point(1)\n"
        ">>\n"
        "> > if true then\n"
        "> >   print(p)\n"
        "> > ~~~"
    )
    html = _markdown().convert(source)
    assert '<span class="kt">Point</span>' in html
    assert "agl-context" not in html
    assert "  <span" in html


def test_default_fence_structure_is_highlighted_without_markers() -> None:
    html = _markdown().convert("```agl\nlet x: int = 1\n```")
    assert re.search(r'<div class="highlight"><pre>.*<code>', html)
    assert '<span class="kt">int</span>' in html


@pytest.mark.parametrize("context", ["", "> <!-- agl-context: let hidden = 1 -->\n"])
@pytest.mark.parametrize("prefix", ["> ", "> > ", "> - "])
def test_unclosed_quote_fence_stops_at_container_boundary(context: str, prefix: str) -> None:
    source = context + prefix + "```agl\n" + prefix + "0\n"
    unrelated = ["", "Unquoted prose.", "", "```agl", "let x = 1", "```", "Tail prose."]
    lines = source.splitlines() + unrelated
    processed = ContextPreprocessor(_markdown()).run(lines)
    assert processed[-len(unrelated) :] == unrelated
    html = _markdown().convert("\n".join(lines))
    assert "Unquoted prose." in html
    assert '<span class="k">let</span>' in html


@pytest.mark.parametrize(
    "header",
    [
        "{.agl}",
        '{#sample .agl hl_lines="1" title="Point.agl"}',
        "{.agl title=Bob's}",
        r"{.agl title=Example\}",
        "{#sample\t.agl\ttitle=Example}",
        "{.agl=ignored .agl}",
        '{.agl hl_lines="not-a-number"}',
        r'{.agl title="Example\"}',
        "agl {title=Bob's}",
        r"agl {title=Example\}",
    ],
)
def test_attribute_language_receives_context(header: str) -> None:
    source = (
        "<!-- agl-context: record Point -->\n<!-- agl-context:   x: int -->\n"
        + "```"
        + header
        + "\nlet p: Point = Point(1)\n```"
    )
    html = _markdown().convert(source)
    assert "agl-context" not in html
    assert '<span class="kt">Point</span>' in html
    assert '<span class="nc">Point</span>' in html


@pytest.mark.parametrize(
    ("start", "continuation", "tags"),
    [
        ("- ", "  ", ["ul", "li"]),
        ("- ", "    ", ["ul", "li"]),
        ("> - ", ">     ", ["blockquote", "ul", "li"]),
        ("1. ", "   ", ["ol", "li"]),
        ("12. ", "    ", ["ol", "li"]),
        ("> - ", ">   ", ["blockquote", "ul", "li"]),
        ("> > 1. ", "> >    ", ["blockquote", "blockquote", "ol", "li"]),
        ("- > ", "  > ", ["ul", "li", "blockquote"]),
    ],
)
def test_metadata_on_list_item_preserves_container(
    start: str, continuation: str, tags: list[str]
) -> None:
    lines = [
        "Intro prose.",
        "",
        start + "<!-- agl-context: record Point -->",
        continuation + "<!-- agl-context:   x: int -->",
        continuation + "```{.agl}",
        continuation + "let p: Point = Point(1)",
        continuation + "```",
        "",
        "Tail prose.",
    ]
    processed = ContextPreprocessor(_markdown()).run(lines)
    assert processed[:2] == lines[:2]
    assert processed[-2:] == lines[-2:]
    assert start in processed
    html = _markdown().convert("\n".join(lines))
    assert "agl-context" not in html
    assert '<span class="kt">Point</span>' in html
    root = ElementTree.fromstring(f"<root>{html}</root>")
    node = root
    for tag in tags:
        child = node.find(tag)
        assert child is not None
        node = child
    assert node.find("div") is not None
