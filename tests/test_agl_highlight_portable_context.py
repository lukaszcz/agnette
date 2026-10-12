"""Independent examples of the portable grammar's deliberately local contract."""

import pytest

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar_eval import evaluate
from agnette.agl.keywords import OPERATOR_SOFT_KEYWORDS, SOFT_KEYWORDS


def _at(source: str, spelling: str) -> list[Category]:
    characters = [category for a, b, category in evaluate(AGL_GRAMMAR, source) for _ in range(a, b)]
    start = source.index(spelling)
    return characters[start : start + len(spelling)]


@pytest.mark.parametrize(
    "source, spelling, category",
    [
        ("@arg-pos(1) def f() = 1", "@arg-pos", Category.ATTRIBUTE),
        ('@ doc("text") record Box', "doc", Category.ATTRIBUTE),
        ("@α-name", "@α-name", Category.ATTRIBUTE),
        ("import std/text", "std/text", Category.MODULE),
        ('std/text::split("x")', "std/text::", Category.MODULE),
        ("Option::Yes", "Option::", Category.MODULE),
        ("import tools/*::*", "tools/*::", Category.MODULE),
        ("import codecs/* as codec", "codecs/*", Category.MODULE),
        ("record Box", "Box", Category.TYPE),
        ("enum Choice = | Yes | No", "Choice", Category.TYPE),
        ("type Alias = int", "Alias", Category.TYPE),
        ("exception Problem\n  message: str", "Problem", Category.TYPE),
        ("enum Choice = | Yes | No", "Yes", Category.CONSTRUCTOR),
        ("enum Choice = | Yes | No", "No", Category.CONSTRUCTOR),
        ("enum Choice =\n  | Yes", "Yes", Category.CONSTRUCTOR),
        ('"%{ record Box }"', "Box", Category.TYPE),
        ("@doc(record Box) record Real", "Real", Category.TYPE),
    ],
)
def test_local_context_features(source: str, spelling: str, category: Category) -> None:
    assert _at(source, spelling) == [category] * len(spelling)


@pytest.mark.parametrize(
    "source, spelling, category",
    [
        # No cross-line declaration/attribute lookahead or attribute ownership.
        ("record\nBox", "Box", Category.NAME),
        ("record @doc Box", "Box", Category.NAME),
        ("@\ndoc", "doc", Category.NAME),
        ("@doc(record Inner)", "Inner", Category.TYPE),
        # No declaration inventory, enum owner, or path promotion history.
        ("let x = a | Name", "Name", Category.CONSTRUCTOR),
        ("let x = left/right", "left/right", Category.MODULE),
        ("std / text", "text", Category.TYPE),
        ("tools/*+name", "tools", Category.NAME),
        # Only identifier declaration/attribute/variant names, never hard words.
        ("record ++", "++", Category.NAME),
        ("record if", "if", Category.KEYWORD),
        ("@if", "if", Category.KEYWORD),
        ("| if", "if", Category.KEYWORD),
        ("|+Name", "|+Name", Category.NAME),
        ("infixl + at 5", "at", Category.NAME),
        ("record-id Box", "Box", Category.NAME),
        ("@import-extra", "import-extra", Category.ATTRIBUTE),
    ],
    ids=[
        "same-line-declarations",
        "no-attribute-transparency",
        "same-line-attributes",
        "argument-code-is-not-hidden",
        "no-enum-owner",
        "no-module-header-owner",
        "contiguous-paths-only",
        "wildcard-operator-boundary",
        "no-symbolic-declarations",
        "no-hardword-declarations",
        "no-hardword-attributes",
        "no-hardword-variants",
        "pipe-operator-boundary",
        "at-remains-a-name",
        "declaration-word-boundary",
        "attribute-name-boundary",
    ],
)
def test_named_context_boundaries(source: str, spelling: str, category: Category) -> None:
    assert _at(source, spelling) == [category] * len(spelling)


def test_declarations_do_not_seed_reference_roles() -> None:
    assert _at("record Box\nBox", "\nBox")[1:] == [Category.NAME] * 3


def test_softword_policy_is_lexical_inventory_not_layout_history() -> None:
    for word in SOFT_KEYWORDS:
        category = Category.OPERATOR_WORD if word in OPERATOR_SOFT_KEYWORDS else Category.KEYWORD
        for source in (word, f"f({word} = 1)", f"x.{word}", f"let {word} = 1"):
            assert _at(source, word) == [category] * len(word)
        assert _at(f"{word}-suffix", f"{word}-suffix") == [Category.NAME] * (len(word) + 7)


@pytest.mark.parametrize(
    "source, spelling, category",
    [
        ('"record Box @doc import std/text | Yes"', "Box", Category.STRING),
        ("# record Box @doc import std/text | Yes", "Box", Category.COMMENT),
        ("$ record Box @doc import std/text | Yes", "Box", Category.VERBATIM),
        ('@doc("text") record Box(value: int) = 12', "int", Category.TYPE),
        ('@doc("text") record Box(value: int) = 12', "12", Category.NUMBER),
        ("std/text::print(1)", "print", Category.BUILTIN_CALL),
        ("print::[int](1)", "print", Category.BUILTIN_CALL),
        ("print::[int](1)", "::", Category.OPERATOR),
        ("int:: [int]", "int", Category.TYPE),
        ("name::[int]", "name", Category.NAME),
    ],
)
def test_context_rules_leave_lexical_core_and_literal_hosts_intact(
    source: str, spelling: str, category: Category
) -> None:
    assert _at(source, spelling) == [category] * len(spelling)


def test_context_grammar_remains_compact_and_all_states_reachable() -> None:
    states = {state.name: state for state in AGL_GRAMMAR.states}
    reachable = {AGL_GRAMMAR.initial}
    pending = [AGL_GRAMMAR.initial]
    while pending:
        for rule in states[pending.pop()].rules:
            target = getattr(rule.action, "state", None)
            if target is not None and target not in reachable:
                reachable.add(target)
                pending.append(target)
    assert reachable == states.keys()
    assert len(reachable) <= 50
