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


def test_native_textmate_source_offsets_and_roundtrip(tmp_path: Path) -> None:
    sources = [
        "",
        "𐐀name 😀 \u2028\u2029\v\f\r\nlet x=1\r# Ω\n",
        '"""😀%{1}\r\n\r\ntail"""\rlet x=2',
        "\r\n\r\n\r\n",
    ]
    grammar = tmp_path / "AgL.tmLanguage.json"
    grammar.write_text(generate(AGL_GRAMMAR))
    helper = Path(__file__).resolve().parents[1] / "config/textmate/tests/tokenize.cjs"
    result = subprocess.run(
        ["node", str(helper), str(grammar)],
        input=json.dumps(sources),
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    reverse = {scope: category for category, scope in TEXTMATE_SCOPES.items()}
    for source, tokens in zip(sources, json.loads(result.stdout), strict=True):
        assert "".join(token["text"] for token in tokens) == source
        actual = []
        offset = 0
        for token in tokens:
            assert token["start"] == offset
            offset = token["end"]
            assert source[token["start"] : offset] == token["text"]
            category = next(
                (reverse[s] for s in reversed(token["scopes"]) if s in reverse), Category.TEXT
            )
            actual.extend([category] * len(token["text"]))
        assert offset == len(source)
        expected = [c for a, b, c in evaluate(AGL_GRAMMAR, source) for _ in range(b - a)]
        assert actual == expected


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
