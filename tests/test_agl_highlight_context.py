"""Adjacent Markdown fence metadata shared by highlighting and snippet consumers."""

import pytest

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


def test_nearest_check_marker_wins() -> None:
    metadata = fence_metadata(
        ["<!-- agl-check: error -->", "<!-- agl-check: fragment -->", "```agl"], 2
    )
    assert metadata is not None
    assert metadata.check == "fragment"
