"""Opt-in native execution: NODE_PATH supplies vscode-textmate and vscode-oniguruma."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Capture, Grammar, Pop, Push, Rule, State
from agnette.agl.highlight.grammar_eval import evaluate
from agnette.agl.highlight.targets.textmate import TEXTMATE_SCOPES, generate
from tests.test_agl_highlight_native_frames import FRAME_CASES

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not os.environ.get("NODE_PATH"),
    reason="requires node and NODE_PATH with vscode-textmate/vscode-oniguruma",
)


def test_native_textmate_frames_captures_and_recovery_batch() -> None:
    generic = Grammar(
        (
            State(
                "root",
                (
                    Rule(
                        "(a)(b)", Category.OPERATOR, (Capture(2, Category.KEYWORD),), Push("body")
                    ),
                    Rule("(c)(d)", Category.NAME, (Capture(1, Category.NUMBER),)),
                ),
                Category.ERROR,
            ),
            State(
                "body",
                (
                    Rule("(x)(z)", Category.STRING, (Capture(2, Category.ENV_HOLE),), Pop()),
                    Rule("$", action=Pop()),
                    Rule("!", Category.ATTRIBUTE, action=Push("inner")),
                ),
                Category.COMMENT,
            ),
            State("inner", (Rule("]", Category.PUNCTUATION, action=Pop()),), Category.VERBATIM),
        )
    )
    sources = [source for source, _, _ in FRAME_CASES] + [
        '"""%{ {1: "nested"} }tail""" let x=1',
        '"%{ {{{1\nlet x=1',
        '"""%{ {{{1\ntail""" let x=1',
        '"""bad\\q\ntail\\n %{2} ${HOME} tail""" let x=1',
        '"""%{ "bad\\q" }"""\ntail""" let x=1',
        '"""%{1',
        '"""%{1\nmore literal',
        "'''%{1\r\nmore literal''' let x=1",
        '"a%{ record Ωmega @ field | Some }b"',
        "$ text %{ @ field } tail\nlet z=3",
        "$\n  hi %{1\n  tail %{2}\nlet x=1",
        "$\n  hi\n\n  tail\nlet x=1",
        "𐐀name 😀 \u2028\nlet x=1",
    ]
    cases = [(AGL_GRAMMAR, source) for source in sources] + [
        (generic, "ab stuff !payload] xz cd?"),
        (generic, "ab stuff\ncd?"),
    ]
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("_textmate_native.cjs"))],
        input=json.dumps([{"grammar": json.loads(generate(g)), "source": s} for g, s in cases]),
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    outputs = json.loads(result.stdout)
    reverse = {scope: category for category, scope in TEXTMATE_SCOPES.items()}
    for (grammar, source), scopes in zip(cases, outputs, strict=True):
        actual = [
            next((reverse[s] for s in reversed(stack) if s in reverse), Category.TEXT)
            for stack in scopes
        ]
        expected = [c for a, b, c in evaluate(grammar, source) for _ in range(b - a)]
        assert actual == expected, source
