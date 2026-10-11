"""Portable grammar contracts, independent of the AgL grammar inventory."""

from dataclasses import FrozenInstanceError

import pytest

from agnette.agl.highlight.categories import Category, Span
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
from agnette.util.ident import IDENT_STOP


def _root(*rules: Rule, default: Category = Category.TEXT) -> Grammar:
    return Grammar((State("root", rules, default),))


def _pieces(source: str, spans: list[Span]) -> list[tuple[str, Category]]:
    cursor = 0
    for start, end, _ in spans:
        assert start == cursor
        assert end > start
        cursor = end
    assert cursor == len(source)
    return [(source[start:end], category) for start, end, category in spans]


def test_first_matching_rule_wins_and_adjacent_spans_coalesce() -> None:
    grammar = _root(Rule("ab", Category.KEYWORD), Rule("a", Category.NUMBER))
    source = "ababa?\n"
    assert _pieces(source, evaluate(grammar, source)) == [
        ("abab", Category.KEYWORD),
        ("a", Category.NUMBER),
        ("?\n", Category.TEXT),
    ]
    assert evaluate(grammar, "") == []


def test_nested_push_pop_switch_and_unclosed_states() -> None:
    grammar = Grammar(
        (
            State("root", (Rule('"', Category.PUNCTUATION, action=Push("string")),)),
            State(
                "string",
                (
                    Rule('"', Category.PUNCTUATION, action=Pop()),
                    Rule(r"\{", Category.INTERPOLATION, action=Push("code")),
                ),
                Category.STRING,
            ),
            State(
                "code",
                (
                    Rule(r"\}", Category.INTERPOLATION, action=Pop()),
                    Rule(r"\{", Category.INTERPOLATION, action=Push("code")),
                    Rule("!", Category.OPERATOR, action=Switch("other")),
                ),
                Category.NAME,
            ),
            State(
                "other",
                (Rule(r"\}", Category.INTERPOLATION, action=Pop()),),
                Category.NUMBER,
            ),
        )
    )
    source = '"a{x{y}z!2}b"?"tail'
    assert _pieces(source, evaluate(grammar, source)) == [
        ('"', Category.PUNCTUATION),
        ("a", Category.STRING),
        ("{", Category.INTERPOLATION),
        ("x", Category.NAME),
        ("{", Category.INTERPOLATION),
        ("y", Category.NAME),
        ("}", Category.INTERPOLATION),
        ("z", Category.NAME),
        ("!", Category.OPERATOR),
        ("2", Category.NUMBER),
        ("}", Category.INTERPOLATION),
        ("b", Category.STRING),
        ('"', Category.PUNCTUATION),
        ("?", Category.TEXT),
        ('"', Category.PUNCTUATION),
        ("tail", Category.STRING),
    ]


def test_captures_optional_empty_and_unclassified_match_regions() -> None:
    grammar = _root(
        Rule(
            r"(a)(?:-)(b)?()",
            Category.OPERATOR,
            captures=(
                Capture(3, Category.ERROR),
                Capture(2, Category.NUMBER),
                Capture(1, Category.NAME),
            ),
        )
    )
    source = "a-ba-!"
    assert _pieces(source, evaluate(grammar, source)) == [
        ("a", Category.NAME),
        ("-", Category.OPERATOR),
        ("b", Category.NUMBER),
        ("a", Category.NAME),
        ("-", Category.OPERATOR),
        ("!", Category.TEXT),
    ]


def test_fixed_width_lookbehind_explicit_unicode_classes_and_line_anchors() -> None:
    grammar = _root(
        Rule(r"(?<=:)[α-ω]+(?=;)", Category.NAME),
        Rule(r"^[A-Z]+$", Category.KEYWORD),
        Rule(r"\[\(\?\)\]", Category.PUNCTUATION),
    )
    source = ":αβ;\nABC\n[(?)]"
    assert _pieces(source, evaluate(grammar, source)) == [
        (":", Category.TEXT),
        ("αβ", Category.NAME),
        (";\n", Category.TEXT),
        ("ABC", Category.KEYWORD),
        ("\n", Category.TEXT),
        ("[(?)]", Category.PUNCTUATION),
    ]


def test_literal_class_delimiters_and_repeated_capture_order() -> None:
    grammar = _root(
        Rule(r"[]()]((x))", captures=(Capture(2, Category.NAME),)),
        Rule(r"[^]a](y)", captures=(Capture(1, Category.NUMBER),)),
        Rule(r"(?:(a)|(b))*", captures=(Capture(1, Category.NAME), Capture(2, Category.NUMBER))),
        Rule(r"[\[\]&\|~\-]+", Category.OPERATOR),
    )
    source = "]xzyaba[&|~-]"
    assert _pieces(source, evaluate(grammar, source)) == [
        ("]", Category.TEXT),
        ("x", Category.NAME),
        ("z", Category.TEXT),
        ("y", Category.NUMBER),
        ("a", Category.TEXT),
        ("b", Category.NUMBER),
        ("a", Category.NAME),
        ("[&|~-]", Category.OPERATOR),
    ]


def test_nondefault_initial_state_and_negative_fixed_width_lookbehind() -> None:
    grammar = Grammar((State("main", (Rule(r"(?<!:)[0-9]{2,3}?", Category.NUMBER),)),), "main")
    source = ":12 1234"
    assert _pieces(source, evaluate(grammar, source)) == [
        (":12 ", Category.TEXT),
        ("1234", Category.NUMBER),
    ]


def test_keyword_boundaries_are_identifier_stops_not_word_boundaries() -> None:
    grammar = _root(keyword_rule(("resource", "if", "if-long"), Category.KEYWORD))
    source = "resource-path resource( if-long if! xresource αresource resourceα"
    assert _pieces(source, evaluate(grammar, source)) == [
        ("resource-path ", Category.TEXT),
        ("resource", Category.KEYWORD),
        ("( ", Category.TEXT),
        ("if-long", Category.KEYWORD),
        (" if! xresource αresource resourceα", Category.TEXT),
    ]
    for stop in IDENT_STOP:
        text = f"{stop}resource{stop}"
        assert _pieces(text, evaluate(grammar, text)) == [
            (stop, Category.TEXT),
            ("resource", Category.KEYWORD),
            (stop, Category.TEXT),
        ]
    escaped = _root(keyword_rule(("foo?", "a+b", 'foo"bar'), Category.NAME))
    assert _pieces('foo? a+b foo"bar', evaluate(escaped, 'foo? a+b foo"bar')) == [
        ("foo?", Category.NAME),
        (" ", Category.TEXT),
        ("a+b", Category.NAME),
        (" ", Category.TEXT),
        ('foo"bar', Category.NAME),
    ]


def test_zero_width_transitions_terminate_and_preserve_partition() -> None:
    grammar = Grammar(
        (
            State("root", (Rule(r"(?=x)", action=Switch("next")),)),
            State("next", (Rule(r"(?=x)", action=Switch("root")),), Category.NAME),
        )
    )
    assert _pieces("xx", evaluate(grammar, "xx")) == [("xx", Category.TEXT)]
    nested = Grammar(
        (
            State("root", (Rule("", action=Push("inner")),)),
            State(
                "inner",
                (Rule(r"(?=!)", action=Pop()), Rule("", action=Push("inner"))),
                Category.STRING,
            ),
        )
    )
    assert _pieces("a!b", evaluate(nested, "a!b")) == [("a!b", Category.STRING)]
    assert _pieces(
        "x", evaluate(_root(Rule("", Category.ERROR), Rule("x", Category.NAME)), "x")
    ) == [("x", Category.NAME)]
    assert _pieces("x", evaluate(_root(Rule("x", Category.NAME, action=Pop())), "x")) == [
        ("x", Category.NAME)
    ]


@pytest.mark.parametrize("pattern", [r"a{2}", r"a{2,}", r"a{2,3}", r"a{2,3}?"])
def test_portable_braced_quantifiers_are_accepted(pattern: str) -> None:
    assert _pieces("aaa", evaluate(_root(Rule(pattern, Category.NAME)), "aaa"))[0] == (
        "aa" if pattern.endswith("?") or pattern == r"a{2}" else "aaa",
        Category.NAME,
    )


@pytest.mark.parametrize("quantifier", ["{100000}", "{100000,}", "{0,100000}", "{100000,100000}"])
@pytest.mark.parametrize("lazy", ["", "?"])
def test_portable_repeat_limit_is_inclusive(quantifier: str, lazy: str) -> None:
    source = "a" * 100000
    grammar = _root(Rule(f"a{quantifier}{lazy}b", Category.NAME))
    assert _pieces(source + "b", evaluate(grammar, source + "b")) == [(source + "b", Category.NAME)]


@pytest.mark.parametrize("bound", ["100001", "4294967296", "9" * 5000])
@pytest.mark.parametrize("quantifier", ["{{{}}}", "{{{},}}", "{{{},100000}}", "{{0,{}}}"])
@pytest.mark.parametrize("lazy", ["", "?"])
def test_nonportable_repeat_bounds_raise_value_error(
    bound: str, quantifier: str, lazy: str
) -> None:
    with pytest.raises(ValueError):
        Rule("a" + quantifier.format(bound) + lazy)


@pytest.mark.parametrize(
    "pattern, source",
    [
        (r"(?<=(a))b", "ab"),
        (r"(?<=(?<=a))b", "ab"),
        (r"(?<!(?<=a))b", "b"),
        (r"(?<!(?<!a))b", "ab"),
        (r"(?:(?=b)b)*", "b"),
        (r"((?=b))*b", "b"),
        (r"(?:^b|b)*", "b"),
        (r"(?:^$)*b", "b"),
    ],
)
def test_portable_assertion_nesting_and_repetition(pattern: str, source: str) -> None:
    grammar = _root(Rule(pattern, Category.NAME))
    assert _pieces(source, evaluate(grammar, source)) == (
        [("a", Category.TEXT), ("b", Category.NAME)]
        if source == "ab"
        else [(source, Category.NAME)]
    )


@pytest.mark.parametrize("behind", ["(?<=", "(?<!"])
@pytest.mark.parametrize("ahead", ["(?=a)", "(?!a)", "(?:(?=a))", "(?:(?!a))"])
def test_lookahead_inside_lookbehind_is_rejected(behind: str, ahead: str) -> None:
    with pytest.raises(ValueError):
        Rule(behind + ahead + ")a")


@pytest.mark.parametrize(
    "pattern",
    [
        r"(?<!(a))b",
        r"(?<!(?:(a)))b",
        r"(?<!(?<=(a)))b",
        r"(?<!(?<!(a)))b",
        r"(?<=(?<!a))b",
        r"(?<=(?:(?<!a)))b",
        r"(?<!(?<=(?<!a)))b",
    ],
)
def test_nonportable_lookbehind_contents_are_rejected(pattern: str) -> None:
    with pytest.raises(ValueError):
        Rule(pattern)


@pytest.mark.parametrize(
    "assertion",
    [
        "(?=a)",
        "(?!a)",
        "(?<=a)",
        "(?<!a)",
        "(?:(?=a))",
        "(?:(?:(?!a)))",
        "(?:^)",
        "(?:$)",
        "(?:^|a)",
        "(?:a|$)",
        "(?:|^)",
    ],
)
@pytest.mark.parametrize("quantifier", ["*", "+", "?", "{0}", "{1,}", "{0,2}"])
@pytest.mark.parametrize("lazy", ["", "?"])
def test_repeated_assertions_are_rejected(assertion: str, quantifier: str, lazy: str) -> None:
    with pytest.raises(ValueError):
        Rule(assertion + quantifier + lazy)


def test_lookbehind_restrictions_do_not_leak_to_sibling_groups() -> None:
    grammar = _root(Rule(r"(?<!a)(?=b)(b)", captures=(Capture(1, Category.NAME),)))
    assert _pieces("b", evaluate(grammar, "b")) == [("b", Category.NAME)]


def test_literal_braces_require_escaping_outside_character_classes() -> None:
    source = "{,}{}"
    grammar = _root(Rule(r"\{,\}", Category.NAME), Rule(r"[{}]+", Category.OPERATOR))
    assert _pieces(source, evaluate(grammar, source)) == [
        ("{,}", Category.NAME),
        ("{}", Category.OPERATOR),
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("pattern", [r"a\n(b)", r"(?<=a\n)(b)", r"a(?=\nb)", r"a[^x]+b"])
def test_rules_and_captures_cannot_see_other_lines(pattern: str, newline: str) -> None:
    captures = (Capture(1, Category.NAME),) if "(b)" in pattern else ()
    grammar = _root(Rule(pattern, Category.ERROR, captures=captures))
    source = f"a{newline}b"
    assert _pieces(source, evaluate(grammar, source)) == [(source, Category.TEXT)]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_line_local_anchors_lookarounds_and_newline_trivia(newline: str) -> None:
    grammar = _root(
        Rule(r"^b(?<!a)$", Category.NAME),
        Rule(r"^a(?!\r?\nb)$", Category.KEYWORD),
        Rule(r"[\r\n]+", Category.ERROR),
    )
    source = f"a{newline}b{newline}"
    assert _pieces(source, evaluate(grammar, source)) == [
        ("a", Category.KEYWORD),
        (newline, Category.TEXT),
        ("b", Category.NAME),
        (newline, Category.TEXT),
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_state_survives_line_boundaries_but_newlines_are_trivia(newline: str) -> None:
    grammar = Grammar(
        (
            State("root", (Rule('"', Category.PUNCTUATION, action=Push("string")),)),
            State("string", (Rule('"', Category.PUNCTUATION, action=Pop()),), Category.STRING),
        )
    )
    source = f'"a{newline}{newline}b"c'
    assert _pieces(source, evaluate(grammar, source)) == [
        ('"', Category.PUNCTUATION),
        ("a", Category.STRING),
        (newline + newline, Category.TEXT),
        ("b", Category.STRING),
        ('"', Category.PUNCTUATION),
        ("c", Category.TEXT),
    ]


def test_zero_width_transitions_run_at_line_end_and_on_empty_lines() -> None:
    grammar = Grammar(
        (
            State("root", (Rule("x", Category.KEYWORD, action=Push("line")),)),
            State("line", (Rule("$", action=Pop()),), Category.NAME),
        )
    )
    assert _pieces("xy\nz", evaluate(grammar, "xy\nz")) == [
        ("x", Category.KEYWORD),
        ("y", Category.NAME),
        ("\nz", Category.TEXT),
    ]
    empty_line = Grammar(
        (
            State("root", (Rule("^$", action=Switch("body")),)),
            State("body", (), Category.NAME),
        )
    )
    assert _pieces("\nx", evaluate(empty_line, "\nx")) == [
        ("\n", Category.TEXT),
        ("x", Category.NAME),
    ]


def test_line_local_capture_offsets_remain_original_source_offsets() -> None:
    grammar = _root(
        Rule(
            r"^([a-z]+):([0-9]+)$",
            Category.PUNCTUATION,
            (Capture(1, Category.NAME), Capture(2, Category.NUMBER)),
        )
    )
    source = "a:12\r\nb:3"
    assert _pieces(source, evaluate(grammar, source)) == [
        ("a", Category.NAME),
        (":", Category.PUNCTUATION),
        ("12", Category.NUMBER),
        ("\r\n", Category.TEXT),
        ("b", Category.NAME),
        (":", Category.PUNCTUATION),
        ("3", Category.NUMBER),
    ]


def test_chained_line_end_pops_and_switch_preserve_stack_semantics() -> None:
    grammar = Grammar(
        (
            State("root", (Rule("a", Category.KEYWORD, action=Push("outer")),)),
            State(
                "outer",
                (Rule("$", action=Pop()), Rule("b", Category.NAME, action=Push("inner"))),
                Category.STRING,
            ),
            State(
                "inner",
                (Rule("$", action=Pop()), Rule("c", Category.NAME, action=Switch("last"))),
            ),
            State("last", (Rule("$", action=Pop()),), Category.NUMBER),
        )
    )
    source = "abcd\ne"
    assert _pieces(source, evaluate(grammar, source)) == [
        ("a", Category.KEYWORD),
        ("bc", Category.NAME),
        ("d", Category.NUMBER),
        ("\ne", Category.TEXT),
    ]


def test_unicode_separators_remain_line_content() -> None:
    source = "a\u2028b\u0085c"
    assert _pieces(source, evaluate(_root(Rule(".+", Category.NAME)), source)) == [
        (source, Category.NAME)
    ]


def test_model_is_immutable_and_hashable() -> None:
    rule = Rule("x", Category.NAME)
    state = State("root", (rule,))
    grammar = Grammar((state,))
    assert len({grammar, Grammar((state,))}) == 1
    for model, attribute, value in (
        (rule, "pattern", "y"),
        (state, "name", "next"),
        (grammar, "initial", "next"),
        (Capture(1, Category.NAME), "group", 2),
        (Push("root"), "state", "next"),
        (Switch("root"), "state", "next"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(model, attribute, value)


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a)\1",
        r"\g<1>",
        r"(?P<name>a)",
        r"(?P=name)",
        r"(?(1)a|b)",
        r"(?<=a+)b",
        r"(?<!a|bc)x",
        r"\bword\b",
        r"\w+",
        r"\d",
        r"\s",
        r"\p{L}",
        r"\u0061",
        r"\x61",
        r"\Aabc",
        r"\Z",
        r"(?i:abc)",
        r"(?>a)",
        r"a++",
        r"a*+",
        r"a?+",
        r"a{1,2}+",
        r"a{,}",
        r"a{,2}",
        r"a{1",
        r"a{1, 2}",
        r"a{١,٢}",
        r"a{word}",
        r"a}",
        r"a{2,1}",
        r"a{1}{2}",
        r"[[:alpha:]]",
        r"[a&&b]",
        r"[a||b]",
        r"[a~~b]",
        "[",
        "\\",
    ],
)
def test_nonportable_or_invalid_regex_is_rejected(pattern: str) -> None:
    with pytest.raises(ValueError):
        Rule(pattern)


@pytest.mark.parametrize(
    "captures",
    [
        (Capture(0, Category.NAME),),
        (Capture(2, Category.NAME),),
        (Capture(1, Category.NAME), Capture(1, Category.NUMBER)),
    ],
)
def test_invalid_capture_mapping_is_rejected(captures: tuple[Capture, ...]) -> None:
    with pytest.raises(ValueError):
        Rule("(x)", captures=captures)


@pytest.mark.parametrize(
    "pattern", ["((x))", "[]()]((x))", "(?=(x))x", "(?<=(x))y", "(?!(x))y", "(?<!(x))y"]
)
def test_overlapping_or_lookaround_captures_are_rejected(pattern: str) -> None:
    captures = (
        (Capture(1, Category.NAME), Capture(2, Category.NUMBER))
        if pattern in {"((x))", "[]()]((x))"}
        else (Capture(1, Category.NAME),)
    )
    with pytest.raises(ValueError):
        Rule(pattern, captures=captures)


@pytest.mark.parametrize(
    "states, initial",
    [
        ((), "root"),
        ((State("root", ()), State("root", ())), "root"),
        ((State("", ()),), ""),
        ((State("root", ()),), "missing"),
        ((State("root", (Rule("x", action=Push("missing")),)),), "root"),
        ((State("root", (Rule("x", action=Switch("missing")),)),), "root"),
        ((State("root", (Rule("x", action=Push("inner")),)), State("inner", ())), "root"),
        (
            (
                State("root", (Rule("x", action=Push("inner")),)),
                State("inner", (Rule("x"), Rule("y", action=Pop()))),
            ),
            "root",
        ),
    ],
)
def test_invalid_states_are_rejected(states: tuple[State, ...], initial: str) -> None:
    with pytest.raises(ValueError):
        Grammar(states, initial)


@pytest.mark.parametrize("words", [(), ("",), ("a b",), ("1bad",)])
def test_keyword_helper_rejects_empty_or_nonidentifier_inventory(words: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        keyword_rule(words, Category.KEYWORD)


def test_keyword_helper_can_transition_and_ignores_duplicate_words() -> None:
    grammar = Grammar(
        (
            State("root", (keyword_rule(("if", "if"), Category.KEYWORD, action=Switch("body")),)),
            State("body", (), Category.NAME),
        )
    )
    assert _pieces("if x", evaluate(grammar, "if x")) == [
        ("if", Category.KEYWORD),
        (" x", Category.NAME),
    ]
