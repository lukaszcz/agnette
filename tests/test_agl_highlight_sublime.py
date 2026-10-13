"""Pure Sublime syntax lowering: ordered contexts, transitions and YAML quoting."""

import codecs

import pytest

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Capture, Grammar, Pop, Push, Rule, State, Switch
from agnette.agl.highlight.targets import TARGETS
from agnette.agl.highlight.targets.textmate import TEXTMATE_SCOPES

_FALLBACK = r"[^\r\n]"

type _Entry = dict[str, str | dict[str, str]]


def _contexts(document: str) -> dict[str, list[_Entry]]:
    """Read back the generator's fixed block layout, decoding quoted scalars."""
    lines = document.splitlines()
    assert lines[:6] == [
        "%YAML 1.2",
        "---",
        "name: AgL",
        "file_extensions: [agl]",
        "scope: source.agl",
        "contexts:",
    ]
    contexts: dict[str, list[_Entry]] = {}
    entry: _Entry = {}
    for line in lines[6:]:
        assert line.isascii()
        indent = len(line) - len(line.lstrip(" "))
        key, _, value = line.strip().removeprefix("- ").partition(": ")
        if value.startswith('"'):
            value = codecs.decode(value[1:-1], "unicode_escape")
        if indent == 2:
            contexts[key.removesuffix(":")] = []
        elif indent == 4:
            entry = {key: value}
            contexts[next(reversed(contexts))].append(entry)
        elif indent == 6:
            entry[key.removesuffix(":")] = value if value else {}
        else:
            captures = entry["captures"]
            assert indent == 8 and isinstance(captures, dict)
            captures[key] = value
    return contexts


def test_sublime_builtin_is_deterministic_and_preserves_rule_order() -> None:
    output = TARGETS["sublime"](AGL_GRAMMAR)
    assert output == TARGETS["sublime"](AGL_GRAMMAR)
    contexts = _contexts(output)
    assert list(contexts)[0] == "main"
    assert len(contexts) == len(AGL_GRAMMAR.states)
    for state, entries in zip(AGL_GRAMMAR.states, contexts.values(), strict=True):
        assert [entry["match"] for entry in entries] == [
            *(rule.pattern for rule in state.rules),
            _FALLBACK,
        ]
        assert entries[-1] == {"match": _FALLBACK, "scope": TEXTMATE_SCOPES[state.default]}


def test_sublime_lowers_scopes_captures_and_transitions() -> None:
    grammar = Grammar(
        (
            State(
                "body",
                (
                    Rule("z", Category.STRING, action=Pop()),
                    Rule("s", Category.NAME, action=Switch("other")),
                ),
                Category.COMMENT,
            ),
            State(
                "root",
                (
                    Rule(""),
                    Rule(
                        "(a)(b)", Category.OPERATOR, (Capture(2, Category.KEYWORD),), Push("body")
                    ),
                ),
            ),
            State("other", (Rule("$", action=Pop()), Rule("y", Category.NUMBER))),
        ),
        initial="root",
    )
    contexts = _contexts(TARGETS["sublime"](grammar))
    assert list(contexts) == ["state0", "main", "state2"]
    assert contexts["main"][0] == {
        "match": "(a)(b)",
        "scope": TEXTMATE_SCOPES[Category.OPERATOR],
        "captures": {"2": TEXTMATE_SCOPES[Category.KEYWORD]},
        "push": "state0",
    }
    assert contexts["state0"] == [
        {"match": "z", "scope": TEXTMATE_SCOPES[Category.STRING], "pop": "true"},
        {"match": "s", "scope": TEXTMATE_SCOPES[Category.NAME], "set": "state2"},
        {"match": _FALLBACK, "scope": TEXTMATE_SCOPES[Category.COMMENT]},
    ]
    assert contexts["state2"][0] == {
        "match": "$",
        "scope": TEXTMATE_SCOPES[Category.TEXT],
        "pop": "true",
    }


@pytest.mark.parametrize("pattern", ['"', "\\\\", "\t", "Ω", "😀", "\x7f", '["\\\\]+'])
def test_sublime_quotes_patterns_as_ascii_yaml(pattern: str) -> None:
    grammar = Grammar((State("root", (Rule(pattern, Category.KEYWORD),)),))
    assert _contexts(TARGETS["sublime"](grammar))["main"][0]["match"] == pattern


@pytest.mark.parametrize(
    "states",
    [
        (State("root", (Rule("$", action=Pop()),)),),
        (State("root", (Rule("x", action=Switch("root")),)),),
        (State("root", (Rule("x*"),)),),
        (
            State("root", (Rule("x", action=Push("body")),)),
            State("body", (Rule("$", action=Pop()), Rule("(?=y)", action=Switch("body")))),
        ),
        (State("root", (Rule(r"x(?=\n)"),)),),
        (State("root", (Rule(r"[^x]+"),)),),
        (
            State("root", (Rule("(?=x)", action=Push("body")),)),
            State("body", (Rule("$", action=Pop()),)),
        ),
    ],
)
def test_sublime_rejects_non_native_transitions(states: tuple[State, ...]) -> None:
    with pytest.raises(ValueError):
        TARGETS["sublime"](Grammar(states))
