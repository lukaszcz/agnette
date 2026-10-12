"""Pure TextMate framing, captures and explicit unmatched-category scopes."""

import json

import pytest

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Capture, Grammar, Pop, Push, Rule, State, Switch
from agnette.agl.highlight.targets import TARGETS
from agnette.agl.highlight.targets.textmate import TEXTMATE_SCOPES


def test_textmate_builtin_is_deterministic_and_compact() -> None:
    output = TARGETS["textmate"](AGL_GRAMMAR)
    assert output == TARGETS["textmate"](AGL_GRAMMAR)
    grammar = json.loads(output)
    assert grammar["scopeName"] == "source.agl"
    assert grammar["fileTypes"] == ["agl"]
    assert len(grammar["repository"]) == 11
    assert set(TEXTMATE_SCOPES) == set(Category)
    assert len(set(TEXTMATE_SCOPES.values())) == len(Category)
    assert grammar["patterns"] == [{"include": "#state0"}]
    for state, rules in zip(AGL_GRAMMAR.states, grammar["repository"].values(), strict=True):
        assert rules["patterns"][-1] == {
            "match": "[^\\r\\n]",
            "name": TEXTMATE_SCOPES[state.default],
        }
        for rule in rules["patterns"]:
            if "begin" in rule:
                assert rule["contentName"] in TEXTMATE_SCOPES.values()
                assert rule["end"]
                assert len(rule["patterns"]) == 1


def test_textmate_required_category_scopes() -> None:
    assert TEXTMATE_SCOPES[Category.BUILTIN_CALL] == "support.function.builtin.agl"
    assert TEXTMATE_SCOPES[Category.VERBATIM] == "string.unquoted.verbatim.agl"


@pytest.mark.parametrize(
    "grammar",
    [
        AGL_GRAMMAR,
        Grammar(
            (
                State(
                    "root",
                    (
                        Rule(
                            "(x)" * len(Category),
                            captures=tuple(Capture(i, c) for i, c in enumerate(Category, 1)),
                            action=Push("body"),
                        ),
                    ),
                ),
                State("body", (Rule("z", Category.STRING, action=Pop()),)),
            )
        ),
    ],
)
def test_textmate_recursive_structure_resolves_includes_and_categories(grammar: Grammar) -> None:
    document = json.loads(TARGETS["textmate"](grammar))
    reverse = {scope: category for category, scope in TEXTMATE_SCOPES.items()}
    seen: set[Category] = set()

    def visit(value: object, *, root: bool = False) -> None:
        if isinstance(value, dict):
            for field, child in value.items():
                if field == "include":
                    assert isinstance(child, str) and child.startswith("#")
                    assert child[1:] in document["repository"]
                elif field in {"name", "contentName"} and not root:
                    assert isinstance(child, str)
                    seen.add(reverse[child])
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document, root=True)
    assert seen


def test_textmate_generic_captures_and_end_alternatives() -> None:
    grammar = Grammar(
        (
            State(
                "root",
                (
                    Rule(""),
                    Rule(
                        "(a)(b)", Category.OPERATOR, (Capture(2, Category.KEYWORD),), Push("body")
                    ),
                ),
            ),
            State(
                "body",
                (
                    Rule("(x)", Category.STRING, (Capture(1, Category.ENV_HOLE),), Pop()),
                    Rule("$", action=Pop()),
                    Rule("(y)", Category.NUMBER, (Capture(1, Category.NAME),)),
                ),
                Category.COMMENT,
            ),
        )
    )
    repository = json.loads(TARGETS["textmate"](grammar))["repository"]
    entry = repository["state0"]["patterns"][0]
    assert entry["beginCaptures"] == {
        "0": {"name": TEXTMATE_SCOPES[Category.OPERATOR]},
        "2": {"name": TEXTMATE_SCOPES[Category.KEYWORD]},
    }
    assert entry["end"] == "((x))|($)"
    assert entry["endCaptures"] == {
        "1": {"name": TEXTMATE_SCOPES[Category.STRING]},
        "2": {"name": TEXTMATE_SCOPES[Category.ENV_HOLE]},
        "3": {"name": TEXTMATE_SCOPES[Category.TEXT]},
    }
    assert entry["contentName"] == TEXTMATE_SCOPES[Category.COMMENT]
    assert (
        repository["state1"]["patterns"][0]["captures"]["1"]["name"]
        == TEXTMATE_SCOPES[Category.NAME]
    )


@pytest.mark.parametrize(
    "states",
    [
        (State("root", (Rule("x", action=Switch("root")),)),),
        (State("root", (Rule("$", action=Pop()),)),),
        (State("root", (Rule("x*"),)),),
        (State("root", (Rule(r"x(?=\n)"),)),),
        (State("root", (Rule("\n"),)),),
        (State("root", (Rule(r"[\n]"),)),),
        (State("root", (Rule(r"[^x]+"),)),),
        (
            State("root", (Rule("(?=x)", action=Push("body")),)),
            State("body", (Rule("$", action=Pop()),)),
        ),
        (
            State("root", (Rule("", action=Push("body")),)),
            State("body", (Rule("$", action=Pop()),)),
        ),
        (
            State("root", (Rule("x", action=Push("body")),)),
            State("body", (Rule("z", action=Pop()), Rule("x"), Rule("y", action=Pop()))),
        ),
    ],
)
def test_textmate_rejects_non_native_transitions(states: tuple[State, ...]) -> None:
    with pytest.raises(ValueError):
        TARGETS["textmate"](Grammar(states))
