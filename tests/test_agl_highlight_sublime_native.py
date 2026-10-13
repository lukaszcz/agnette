"""Native Sublime syntax execution through bat/syntect, run whenever bat is installed."""

from pathlib import Path

import pytest

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Capture, Grammar, Pop, Push, Rule, State, Switch
from agnette.agl.highlight.targets.sublime import generate
from tests.test_agl_highlight_native_frames import FRAME_CASES
from tools.highlight_engine_support import NEWLINE_CASES, CorpusCase
from tools.highlight_sublime_support import run_corpus
from tools.install_highlighting import bat_executable

pytestmark = pytest.mark.skipif(bat_executable() is None, reason="requires bat")


def _assert_native(grammar: Grammar, sources: list[str], tmp_path: Path) -> None:
    definition = tmp_path / "agl.sublime-syntax"
    definition.write_text(generate(grammar), encoding="utf-8")
    cases = tuple(CorpusCase(f"native-{index}", source) for index, source in enumerate(sources))
    run_corpus(definition, cases, grammar=grammar)


def test_native_sublime_builtin_frames_recovery_and_newlines(tmp_path: Path) -> None:
    sources = [
        "",
        "𐐀name 😀 ≤ λ\nlet x=1\n# Ω\n",
        *(source for source, _, _ in FRAME_CASES),
        '"""%{ {1: "nested"} }tail""" let x=1',
        '"%{ {{{1\nlet x=1',
        "$ text %{ @ field } tail\nlet z=3",
        "$\n  hi\n\n  tail\nlet x=1",
        *(case.source for case in NEWLINE_CASES),
    ]
    _assert_native(AGL_GRAMMAR, sources, tmp_path)


def test_native_sublime_generic_captures_transitions_and_quoting(tmp_path: Path) -> None:
    grammar = Grammar(
        (
            State(
                "root",
                (
                    Rule(
                        "(a)(b)", Category.OPERATOR, (Capture(2, Category.KEYWORD),), Push("body")
                    ),
                    Rule('"\\\\Ω😀\t', Category.ATTRIBUTE),
                ),
                Category.ERROR,
            ),
            State(
                "body",
                (
                    Rule("(x)(z)", Category.STRING, (Capture(2, Category.ENV_HOLE),), Pop()),
                    Rule("$", action=Pop()),
                    Rule("!", Category.ATTRIBUTE, action=Push("inner")),
                    Rule("s", Category.NAME, action=Switch("switched")),
                ),
                Category.COMMENT,
            ),
            State("inner", (Rule("]", Category.PUNCTUATION, action=Pop()),), Category.VERBATIM),
            State("switched", (Rule("q", Category.NUMBER, action=Pop()),), Category.TYPE),
        )
    )
    sources = [
        "ab stuff !payload] xz cd?",
        "ab stuff\ncd?",
        'w "\\Ω😀\tv',
        "ab s q cd s\nab s\ncd",
    ]
    _assert_native(grammar, sources, tmp_path)
