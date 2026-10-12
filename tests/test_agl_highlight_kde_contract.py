"""KDE rejects generic semantics it cannot preserve; the model remains unrestricted."""

import xml.etree.ElementTree as ET

import pytest

from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import (
    Capture,
    Grammar,
    Pop,
    Push,
    Rule,
    State,
    Switch,
    keyword_rule,
)
from agnette.agl.highlight.grammar_eval import evaluate
from agnette.agl.highlight.targets.kde import generate


@pytest.mark.parametrize("bound", ["65536", "100000", "00065536"])
@pytest.mark.parametrize("quantifier", ["{{{}}}", "{{{},}}", "{{{},100000}}", "{{0,{}}}"])
@pytest.mark.parametrize("lazy", ["", "?"])
def test_repeat_bounds_above_pcre2_limit_are_target_only_errors(
    bound: str, quantifier: str, lazy: str
) -> None:
    pattern = "a" + quantifier.format(bound) + lazy
    grammar = Grammar((State("root", (Rule(pattern, Category.NAME),)),))
    evaluate(grammar, "a")
    with pytest.raises(ValueError):
        generate(grammar)


@pytest.mark.parametrize("body", ["(?:ab)", "(ab)", "(?:(?:ab))", r"(?:[\)])"])
@pytest.mark.parametrize("quantifier", ["{2}", "{10000}", "{65535}", "{0,2}", "{1,2}", "{2,}"])
@pytest.mark.parametrize("lazy", ["", "?"])
def test_expanding_group_repeats_are_target_only_errors(
    body: str, quantifier: str, lazy: str
) -> None:
    grammar = Grammar((State("root", (Rule(body + quantifier + lazy),)),))
    evaluate(grammar, "abab")
    with pytest.raises(ValueError):
        generate(grammar)


@pytest.mark.parametrize("body", ["(?:ab)", "(ab)", "(?:(?:ab))"])
@pytest.mark.parametrize("quantifier", ["{0}", "{1}", "{0,1}", "{1,1}", "{0,}", "{1,}"])
@pytest.mark.parametrize("lazy", ["", "?"])
def test_nonexpanding_group_repeats_remain_supported(body: str, quantifier: str, lazy: str) -> None:
    pattern = body + quantifier + lazy + "x"
    grammar = Grammar((State("root", (Rule(pattern),)),))
    regex = ET.fromstring(generate(grammar)).find(".//RegExpr")
    assert regex is not None
    assert regex.attrib["String"] == pattern


@pytest.mark.parametrize("pattern", [r"\){2}", r"[)]{2}", r"[])]{2}", r"[\)]{2}"])
def test_literal_group_closers_are_not_group_repeats(pattern: str) -> None:
    grammar = Grammar((State("root", (Rule(pattern),)),))
    regex = ET.fromstring(generate(grammar)).find(".//RegExpr")
    assert regex is not None
    assert regex.attrib["String"] == pattern


@pytest.mark.parametrize("bound", ["65535", "00065535"])
@pytest.mark.parametrize("quantifier", ["{{{}}}", "{{{},}}", "{{0,{}}}", "{{{0},{0}}}"])
@pytest.mark.parametrize("lazy", ["", "?"])
@pytest.mark.parametrize("atom", ["a", "[ab]"])
def test_pcre2_repeat_limit_is_inclusive(bound: str, quantifier: str, lazy: str, atom: str) -> None:
    pattern = atom + quantifier.format(bound) + lazy
    grammar = Grammar((State("root", (Rule(pattern),)),))
    xml = ET.fromstring(generate(grammar))
    regex = xml.find(".//RegExpr")
    assert regex is not None
    assert regex.attrib["String"] == pattern


@pytest.mark.parametrize(
    "pattern",
    [r"a\{65536\}", r"a\{0,100000\}", r"[a{100000}]", r"[]^{65536}]", r"[\]{65536}]"],
)
def test_literal_braces_do_not_have_repeat_bounds(pattern: str) -> None:
    grammar = Grammar((State("root", (Rule(pattern),)),))
    xml = ET.fromstring(generate(grammar))
    regex = xml.find(".//RegExpr")
    assert regex is not None
    assert regex.attrib["String"] == pattern


@pytest.mark.parametrize("pattern", [r"\\a{65536}", r"[{65536}]a{0,65536}", "(a{65536})"])
def test_real_quantifiers_after_literal_text_are_rejected(pattern: str) -> None:
    grammar = Grammar((State("root", (Rule(pattern),)),))
    with pytest.raises(ValueError):
        generate(grammar)


@pytest.mark.parametrize("pattern", ["^a|b", "^a|^b", "(?:^a)", "(?=^a)a", "a^b"])
@pytest.mark.parametrize("captured", [False, True])
def test_unsupported_anchor_forms_are_target_only_errors(pattern: str, captured: bool) -> None:
    pattern = pattern + "(c)" if captured else pattern
    captures = (Capture(1, Category.NAME),) if captured else ()
    grammar = Grammar((State("root", (Rule(pattern, captures=captures),)),))
    evaluate(grammar, "zbc")
    with pytest.raises(ValueError):
        generate(grammar)


@pytest.mark.parametrize(
    "pattern",
    ["(?<=x)y", "(?<=x)(y)", "(?:a|(?<=x)y)", "(?<!x)y", "(?<!x)(y)", "(?:a|(?<!x)y)"],
)
def test_lookbehind_is_a_target_only_error(pattern: str) -> None:
    grammar = Grammar(
        (State("root", (Rule("x", Category.NUMBER), Rule(pattern, Category.KEYWORD))),)
    )
    expected = Category.TEXT if "(?<!" in pattern else Category.KEYWORD
    assert evaluate(grammar, "xy")[-1][2] == expected
    with pytest.raises(ValueError):
        generate(grammar)


def test_keyword_helper_left_boundary_is_a_target_only_error() -> None:
    grammar = Grammar((State("root", (keyword_rule(("if",), Category.KEYWORD),)),))
    assert evaluate(grammar, "if xif") == [
        (0, 2, Category.KEYWORD),
        (2, 6, Category.TEXT),
    ]
    with pytest.raises(ValueError):
        generate(grammar)


@pytest.mark.parametrize("pattern", ["", "(?=x)", "$", "x"])
def test_base_frame_pop_is_rejected(pattern: str) -> None:
    grammar = Grammar((State("root", (Rule(pattern, action=Pop()),)),))
    evaluate(grammar, "x")
    with pytest.raises(ValueError, match="KDE.*base-frame pop"):
        generate(grammar)


def test_switched_base_frame_pop_is_rejected() -> None:
    grammar = Grammar(
        (
            State("root", (Rule("a", action=Switch("next")),)),
            State("next", (Rule("b", action=Switch("last")),)),
            State("last", (Rule("c", action=Pop()),)),
        )
    )
    with pytest.raises(ValueError, match="KDE.*base-frame pop"):
        generate(grammar)


@pytest.mark.parametrize("pattern", ["", "(?=x)", "$", "^$"])
def test_zero_width_self_switch_cycles_are_rejected(pattern: str) -> None:
    grammar = Grammar((State("root", (Rule(pattern, action=Switch("root")),)),))
    evaluate(grammar, "x")
    with pytest.raises(ValueError, match="KDE.*zero-width.*cycle"):
        generate(grammar)


@pytest.mark.parametrize("pattern", ["", "$", "^$"])
def test_zero_width_switch_cycles_are_rejected(pattern: str) -> None:
    grammar = Grammar(
        (
            State("root", (Rule(pattern, action=Switch("next")),)),
            State("next", (Rule(pattern, action=Switch("root")),)),
        )
    )
    evaluate(grammar, "x")
    with pytest.raises(ValueError, match="KDE.*zero-width.*cycle"):
        generate(grammar)


def test_zero_width_push_pop_return_cycle_is_rejected() -> None:
    grammar = Grammar(
        (
            State("root", (Rule("(?=x)", action=Push("next")),)),
            State("next", (Rule("", action=Pop()),)),
        )
    )
    evaluate(grammar, "x")
    with pytest.raises(ValueError, match="KDE.*zero-width.*cycle"):
        generate(grammar)


def test_eol_switch_revisits_previously_entered_rule_is_rejected() -> None:
    grammar = Grammar(
        (
            State("root", (Rule("x", action=Push("inner")),)),
            State("inner", (Rule(">", action=Pop()), Rule("$", action=Switch("next")))),
            State("next", (Rule("", action=Push("inner")),)),
        )
    )
    evaluate(grammar, "x\ny")
    with pytest.raises(ValueError, match="KDE.*zero-width.*cycle"):
        generate(grammar)


@pytest.mark.parametrize("pattern", ["", "(?=x)", "$", "^$"])
def test_zero_width_recursive_push_is_rejected(pattern: str) -> None:
    grammar = Grammar(
        (
            State("root", (Rule("x", action=Push("inner")),)),
            State("inner", (Rule(">", action=Pop()), Rule(pattern, action=Push("inner")))),
        )
    )
    evaluate(grammar, "xx")
    with pytest.raises(ValueError, match="KDE.*zero-width.*cycle"):
        generate(grammar)
