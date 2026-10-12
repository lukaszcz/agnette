"""Optional independent execution through Pandoc's native KDE highlighter."""

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Capture, Grammar, Rule, State, Switch, keyword_rule
from agnette.agl.highlight.grammar_eval import evaluate
from agnette.agl.highlight.targets.kde import KDE_STYLES, generate
from tests.test_agl_highlight_native_frames import FRAME_CASES

pytestmark = pytest.mark.skipif(shutil.which("pandoc") is None, reason="requires pandoc")

_CLASSES = dict(
    zip(
        (
            "dsNormal",
            "dsKeyword",
            "dsConstant",
            "dsDataType",
            "dsFunction",
            "dsBuiltIn",
            "dsImport",
            "dsAttribute",
            "dsVariable",
            "dsString",
            "dsSpecialChar",
            "dsVerbatimString",
            "dsDecVal",
            "dsOperator",
            "dsComment",
            "dsError",
        ),
        (
            "",
            "kw",
            "cn",
            "dt",
            "fu",
            "bu",
            "im",
            "at",
            "va",
            "st",
            "sc",
            "vs",
            "dv",
            "op",
            "co",
            "er",
        ),
        strict=True,
    )
)


class _Tokens(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.styles: list[str] = []
        self.text: list[str] = []
        self.colors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "code":
            self.active = True
        if self.active and tag == "span":
            self.styles.append(dict(attrs).get("class") or "")

    def handle_endtag(self, tag: str) -> None:
        if self.active and tag == "span":
            self.styles.pop()
        if tag == "code":
            self.active = False

    def handle_data(self, data: str) -> None:
        if self.active:
            self.text.append(data)
            style = next((item for item in reversed(self.styles) if item), "")
            self.colors.extend([style] * len(data))


def _assert_native(grammar: Grammar, source: str, tmp_path: Path) -> None:
    definition = tmp_path / "agl.xml"
    definition.write_text(generate(grammar), encoding="utf-8")
    result = subprocess.run(
        ["pandoc", "--preserve-tabs", "--syntax-definition", str(definition), "-t", "html"],
        input="```agl\n" + source + "\n```\n",
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    parsed = _Tokens()
    parsed.feed(result.stdout)
    expected = [
        _CLASSES[KDE_STYLES[category]]
        for start, end, category in evaluate(grammar, source)
        for _ in range(end - start)
    ]
    assert "".join(parsed.text) == source
    assert parsed.colors == expected


@pytest.mark.parametrize(
    "rule, source",
    [
        (Rule("x", Category.KEYWORD, action=Switch("body"), words=("xx",)), "xx tail"),
        (Rule("x+", Category.KEYWORD, action=Switch("body"), words=("x",)), "xxx tail"),
        (
            Rule("x", Category.KEYWORD, action=Switch("body"), words=("x\ry", "x\r\ny")),
            "xx tail",
        ),
        (
            keyword_rule(("xx",), Category.KEYWORD, action=Switch("body"), token_start=True),
            "xx tail",
        ),
        (
            keyword_rule(("x?", "x"), Category.KEYWORD, action=Switch("body"), token_start=True),
            "x? tail",
        ),
        (
            Rule("(x)", Category.TEXT, (Capture(1, Category.KEYWORD),), Switch("body"), ("xx",)),
            "xx tail",
        ),
    ],
)
def test_native_keyword_extent_before_switch(rule: Rule, source: str, tmp_path: Path) -> None:
    grammar = Grammar((State("root", (rule,)), State("body", (), Category.NUMBER)))
    _assert_native(grammar, source, tmp_path)


@pytest.mark.parametrize("pattern", ["a{65535}", "[ab]{65535}", "(?:ab){0,1}", "(ab){1}"])
def test_native_supported_repeats(pattern: str, tmp_path: Path) -> None:
    grammar = Grammar((State("root", (Rule(pattern, Category.KEYWORD),)),))
    _assert_native(grammar, "ab", tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        *(source for source, _, _ in FRAME_CASES),
        "$\n  hi\n\n  tail\nlet x=1",
        "@ field record Thing | Some\nexception Problem\ntype Alias enum Choice",
        "@\trecord @true record true | enum @\nrecord\nThing",
        "resource-path resource-dir( resource( as? a+b xrecord record-id",
        '"a%{ record Thing @ field | Some }b"',
        '"unfinished\nlet x = 2',
        '"""%{ {{{x\nlet y=2',
        "$ text %{ @ field } tail\nlet z=3",
        "$\n  body\nnext",
        '"bad\\q\nlet x=3',
        '"""abc\ndef"""',
        "'''abc\ndef'''",
        "$text %{ (record Thing) }",
        "record Ωmega @ attr | Variant\n# comment",
    ],
)
def test_native_builtin_states(source: str, tmp_path: Path) -> None:
    _assert_native(AGL_GRAMMAR, source, tmp_path)
