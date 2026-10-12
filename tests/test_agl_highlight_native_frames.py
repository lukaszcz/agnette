"""Shared native frames retain multiline owners, not rejected children."""

import pytest

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Switch
from agnette.agl.highlight.grammar_eval import evaluate

SUCCESSIVE_FRAME_CASES = (
    ('"""%{1\ntail %{2\nmore\nend""" let after=3', "more", Category.STRING),
    ("'''%{1\ntail %{let n=2 + '%{3}'} %{4\nmore\nend''' let after=3", "more", Category.STRING),
    ('"""bad\\q\ntail %{2\nmore %{3\nend""" let after=3', "more", Category.STRING),
    ("$ \n  hi %{1\n  tail %{2\n  more\nlet after=3", "more", Category.VERBATIM),
    ("$\t\n\thi %{1\n\ttail %{2\n\tmore\nlet after=3", "more", Category.VERBATIM),
)

FRAME_CASES = (
    ('"%{ {1: 2} }tail" let x=1', "let", Category.KEYWORD),
    ('"""%{1\ntail""" let x=1', "tail", Category.STRING),
    ('"""bad\\q consumed"""\ntail\\n %{2} ${HOME}\n""" let x=1', "tail", Category.STRING),
    ('"bad\\q\nlet x=1', "let", Category.KEYWORD),
    ("$\n  hi %{1\n  tail\nlet x=1", "tail", Category.VERBATIM),
    *SUCCESSIVE_FRAME_CASES,
)


def test_builtin_uses_eleven_push_pop_states() -> None:
    assert len(AGL_GRAMMAR.states) == 11
    assert not any(
        isinstance(rule.action, Switch) for state in AGL_GRAMMAR.states for rule in state.rules
    )


@pytest.mark.parametrize("source, word, category", FRAME_CASES)
def test_multiline_owner_survives_child_recovery(
    source: str, word: str, category: Category
) -> None:
    colors = [c for a, b, c in evaluate(AGL_GRAMMAR, source) for _ in range(b - a)]
    start = source.index(word)
    assert colors[start : start + len(word)] == [category] * len(word)
    final = source.rfind("let")
    if final >= 0:
        assert colors[final : final + 3] == [Category.KEYWORD] * 3
