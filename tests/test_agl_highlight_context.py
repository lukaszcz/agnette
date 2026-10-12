"""Adjacent Markdown fence metadata shared by highlighting and snippet consumers."""

import pytest
from markdown.extensions.attr_list import get_attrs

from agnette.agl.highlight.context import fence_metadata


@pytest.mark.parametrize(
    ("markers", "check", "context"),
    [
        ([], None, ""),
        (["<!-- agl-context: record Point -->"], None, "record Point"),
        (
            ["<!-- agl-context: record Point -->", "<!-- agl-context:   x: int -->"],
            None,
            "record Point\n  x: int",
        ),
        (["<!-- agl-check: fragment -->"], "fragment", ""),
        (["<!-- agl-check: error -->"], "error", ""),
        (["<!-- agl-check: skip -->"], "skip", ""),
        (
            ["<!-- agl-check: error -->", "<!-- agl-context: let x = 1 -->"],
            "error",
            "let x = 1",
        ),
        (
            [
                "<!-- agl-context: record Point -->",
                "<!-- agl-check: fragment -->",
                "<!-- agl-context:   x: int -->",
            ],
            "fragment",
            "record Point\n  x: int",
        ),
        (
            ["<!-- agl-context: let x = 1 -->", "<!-- agl-check: fragment -->"],
            "fragment",
            "let x = 1",
        ),
        (["<!-- agl-context: -->"], None, ""),
        (["<!--agl-context:let x = 1-->"], None, "let x = 1"),
    ],
)
@pytest.mark.parametrize("prefix", ["", "    ", "\t", "> ", "  > >   "])
def test_adjacent_fence_metadata(
    markers: list[str], check: str | None, context: str, prefix: str
) -> None:
    lines = [prefix + line for line in [*markers, "```agl", "let p = Point(x = 1)", "```"]]
    metadata = fence_metadata(lines, len(markers))
    assert metadata is not None
    assert metadata.check == check
    assert metadata.context == context
    assert metadata.start == 0
    assert metadata.prefix == prefix
    assert metadata.delimiter == "```"


@pytest.mark.parametrize(
    "separator",
    ["", "prose", "<!-- unrelated -->", "<!-- agl-check: unknown -->", "<!-- agl-context: broken"],
)
def test_non_adjacent_markers_are_ignored(separator: str) -> None:
    lines = ["<!-- agl-context: let x = 1 -->", separator, "```agl"]
    metadata = fence_metadata(lines, 2)
    assert metadata is not None
    assert metadata.context == ""
    assert metadata.check is None
    assert metadata.start == 2


@pytest.mark.parametrize("fence", ["```python", "```agl-extra", "```", "not a fence"])
def test_non_agl_fences_are_ignored(fence: str) -> None:
    assert fence_metadata(["<!-- agl-context: let x = 1 -->", fence], 1) is None


@pytest.mark.parametrize("fence", ['~~~~agl title="Example"', "````agl { .example }"])
def test_fence_options_and_long_delimiters(fence: str) -> None:
    metadata = fence_metadata(["<!-- agl-context: let x = 1 -->", fence], 1)
    assert metadata is not None
    assert metadata.context == "let x = 1"
    assert metadata.delimiter == fence[:4]


@pytest.mark.parametrize("delimiter", ["```", "~~~", "````", "~~~~"])
@pytest.mark.parametrize("gap", ["", " ", "  ", "\t"])
def test_whitespace_before_fence_language(delimiter: str, gap: str) -> None:
    metadata = fence_metadata([f'> {delimiter}{gap}agl title="Example"'], 0)
    assert metadata is not None
    assert metadata.delimiter == delimiter
    assert metadata.prefix == "> "


@pytest.mark.parametrize(
    ("prefix", "line", "body"),
    [
        ("> ", ">", ""),
        ("> ", ">\t", ""),
        ("  > >   ", "  >>", ""),
        ("  > >   ", "> > ", ""),
        ("> > ", ">>let x = 1", "let x = 1"),
        ("> ", ">   x: int", "  x: int"),
        ("  > >   ", "  > >     x: int", "  x: int"),
        ("    ", "      x: int", "  x: int"),
        ("", ">", ">"),
        ("> ", "let x = 1", "let x = 1"),
        ("> > ", "> let x = 1", "> let x = 1"),
    ],
)
def test_fence_body_container_normalization(prefix: str, line: str, body: str) -> None:
    metadata = fence_metadata([prefix + "```agl"], 0)
    assert metadata is not None
    assert metadata.body_line(line) == body


def test_metadata_stays_in_its_markdown_container() -> None:
    metadata = fence_metadata(["<!-- agl-context: let x = 1 -->", "> ```agl"], 1)
    assert metadata is not None
    assert metadata.context == ""


@pytest.mark.parametrize("fence", ["```{.python .agl}", "```{}", '```{".agl"}', "```{.agl=x}"])
def test_non_agl_or_malformed_attributes_are_ignored(fence: str) -> None:
    assert fence_metadata([fence], 0) is None


@pytest.mark.parametrize(
    ("prefix", "line"),
    [("    ", "Outside prose"), (">   ", "> - Next item"), ("> ", "Outside prose")],
)
def test_container_boundaries(prefix: str, line: str) -> None:
    metadata = fence_metadata([prefix + "```agl"], 0)
    assert metadata is not None
    assert metadata.container.content(line) is None
    assert metadata.body_line(line) == line


@pytest.mark.parametrize(
    ("start", "continuation"),
    [("1. ", "  "), ("- > ", "    > "), ("- ", "  > ")],
)
def test_list_metadata_stays_in_its_container(start: str, continuation: str) -> None:
    metadata = fence_metadata(
        [start + "<!-- agl-context: let x = 1 -->", continuation + "```agl"], 1
    )
    assert metadata is not None
    assert metadata.context == ""


def test_metadata_does_not_cross_new_list_item() -> None:
    metadata = fence_metadata(["- <!-- agl-context: let x = 1 -->", "- ```agl"], 1)
    assert metadata is not None
    assert metadata.context == ""
    assert metadata.prefix == "  "


@pytest.mark.parametrize("closing", ["```", "````", "```  "])
def test_longer_closing_fence(closing: str) -> None:
    metadata = fence_metadata(["```agl"], 0)
    assert metadata is not None
    assert metadata.closes(closing)
    assert not metadata.closes("~~~")


def test_nearest_check_marker_wins() -> None:
    metadata = fence_metadata(
        ["<!-- agl-check: error -->", "<!-- agl-check: fragment -->", "```agl"], 2
    )
    assert metadata is not None
    assert metadata.check == "fragment"


@pytest.mark.parametrize(
    "header",
    [
        "{.agl}",
        '{#sample .agl .extra title="Example"}',
        ".agl",
        "{.agl title=Bob's}",
        "{.agl title=Example\\}",
        '{.agl title="broken}',
        "{#sample\t.agl\ttitle=Example}",
        "{.agl=ignored .agl}",
        "{.agl } ignored}",
    ],
)
def test_attribute_language_metadata(header: str) -> None:
    metadata = fence_metadata(["<!-- agl-context: let x = 1 -->", "```" + header], 1)
    assert metadata is not None
    assert metadata.context == "let x = 1"


@pytest.mark.parametrize(
    ("start", "continuation"),
    [
        ("- ", "  "),
        ("- ", "    "),
        ("1. ", "   "),
        ("12) ", "    "),
        ("> - ", ">   "),
        ("> - ", ">     "),
        ("- > ", "  > "),
        ("-   > ", "    > "),
    ],
)
def test_metadata_starts_list_container(start: str, continuation: str) -> None:
    metadata = fence_metadata(
        [
            start + "<!-- agl-context: record Point -->",
            continuation + "<!-- agl-context:   x: int -->",
            continuation + "```agl",
        ],
        2,
    )
    assert metadata is not None
    assert metadata.start == 0
    assert metadata.context == "record Point\n  x: int"


@pytest.mark.parametrize(
    "attributes",
    [
        ".agl",
        "#sample .agl .extra",
        "\t#sample\t.agl\t",
        ".python .agl",
        "title=Bob's .agl",
        r"title=Example\ .agl",
        'title="two words" .agl',
        "title='two words' .agl",
        '.agl title="broken',
        'title="broken .agl',
        '".agl"',
        ".agl=ignored .python",
        ".agl=ignored .agl",
        "key=.agl",
        "key= .agl",
        ".agl =ignored",
        "=ignored .agl",
        ".agl } .python",
        ".python } .agl",
        "",
    ],
)
def test_attribute_language_matches_markdown_lexical_semantics(attributes: str) -> None:
    parsed = get_attrs(attributes.replace("\t", "    "))
    language = next((value for key, value in parsed if key == "."), "")
    metadata = fence_metadata(["```{" + attributes + "}"], 0)
    assert (metadata is not None) == (language == "agl")
