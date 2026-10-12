"""Optional independent execution through Pandoc's native KDE highlighter."""

import shutil
from pathlib import Path

import pytest

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Capture, Grammar, Rule, State, Switch, keyword_rule
from agnette.agl.highlight.targets.kde import generate
from tests.test_agl_highlight_native_frames import FRAME_CASES
from tools.highlight_engine_support import NEWLINE_CASES, CorpusCase
from tools.highlight_kde_support import run_corpus

pytestmark = pytest.mark.skipif(shutil.which("pandoc") is None, reason="requires pandoc")


@pytest.fixture(scope="session")
def builtin_definition(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate the immutable built-in grammar once per native-engine worker."""
    definition = tmp_path_factory.mktemp("kde") / "agl.xml"
    definition.write_text(generate(AGL_GRAMMAR), encoding="utf-8")
    return definition


def _assert_native(grammar: Grammar, source: str, tmp_path: Path) -> None:
    definition = tmp_path / "agl.xml"
    definition.write_text(generate(grammar), encoding="utf-8")
    run_corpus(definition, (CorpusCase("native", source),), grammar=grammar)


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
def test_native_builtin_states(source: str, builtin_definition: Path) -> None:
    run_corpus(builtin_definition, (CorpusCase("native", source),))


@pytest.mark.parametrize("case", NEWLINE_CASES, ids=lambda case: case.name)
def test_native_input_newlines(case: CorpusCase, builtin_definition: Path) -> None:
    run_corpus(builtin_definition, (case,))
