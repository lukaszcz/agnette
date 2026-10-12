"""Character/style compatibility with the prompt lexer before extraction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agnette.agl.repl.console import AglCompleter, AglPromptLexer
from agnette.agl.repl.session import ReplSession
from tests._agl_corpus import lexical_corpus


def test_parent_corpus_character_styles() -> None:
    # Captured from 91132ba0, with qualified declaration styles corrected.
    expected = json.loads(Path(__file__).with_name("agl_repl_corpus_styles.json").read_text())
    differences = []
    for name, source in lexical_corpus():
        lines = AglPromptLexer._styled_lines(source)
        characters = [[(style, ch) for style, text in line for ch in text] for line in lines]
        signature = hashlib.sha256(json.dumps(characters, ensure_ascii=True).encode()).hexdigest()
        if signature != expected[name]:
            differences.append(name)
    assert not differences


@pytest.mark.parametrize(
    ("source", "complete"),
    [
        ('ask $ "le', False),
        ('"bad\n#"\nle', True),
        ('"bad\n# still literal\nle', False),
        ('# "ignored"\nle', True),
        ('"%{\'unfinished}" le', True),
    ],
)
def test_completion_keeps_raw_quote_policy(source: str, complete: bool) -> None:
    completer = AglCompleter(ReplSession())
    completions = [c.text for c in completer.get_completions(Document(source), CompleteEvent())]
    assert ("let" in completions) == complete


@pytest.mark.parametrize(
    ("source", "line", "expected"),
    [
        ('"""\nabc\n"""', 1, [("", "abc")]),
        ('"""abc\ndef\nghi"""', 1, [("", "def")]),
        ("ask $\n  abc\n  def\n", 2, [("", "  def")]),
    ],
)
def test_multiline_token_is_only_presented_on_its_starting_line(
    source: str, line: int, expected: list[tuple[str, str]]
) -> None:
    assert AglPromptLexer._styled_lines(source)[line] == expected
