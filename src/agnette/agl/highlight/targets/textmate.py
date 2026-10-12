"""TextMate JSON using native begin/end frames, captures and category fallbacks.

Pop rules must precede non-pop rules: TextMate tests ends before child patterns.
Switch, base-frame pops and nullable pushes have no equivalent native frame.
"""

import json
import re

from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Grammar, Pop, Push, Rule, Switch
from agnette.agl.highlight.targets._regex import analyze

TEXTMATE_SCOPES: dict[Category, str] = {
    Category.TEXT: "meta.text.agl",
    Category.COMMENT: "comment.line.number-sign.agl",
    Category.KEYWORD: "keyword.control.agl",
    Category.KEYWORD_CONSTANT: "constant.language.agl",
    Category.OPERATOR_WORD: "keyword.operator.word.agl",
    Category.TYPE: "entity.name.type.agl",
    Category.CONSTRUCTOR: "entity.name.function.constructor.agl",
    Category.FUNCTION: "entity.name.function.agl",
    Category.BUILTIN_CALL: "support.function.builtin.agl",
    Category.MODULE: "entity.name.namespace.agl",
    Category.ATTRIBUTE: "entity.other.attribute-name.agl",
    Category.PARAMETER: "variable.parameter.agl",
    Category.LOCAL: "variable.other.local.agl",
    Category.NAME: "variable.other.agl",
    Category.STRING: "string.quoted.agl",
    Category.STRING_ESCAPE: "constant.character.escape.agl",
    Category.INTERPOLATION: "punctuation.section.interpolation.agl",
    Category.VERBATIM: "string.unquoted.verbatim.agl",
    Category.ENV_HOLE: "variable.other.environment.agl",
    Category.NUMBER: "constant.numeric.agl",
    Category.OPERATOR: "keyword.operator.agl",
    Category.PUNCTUATION: "punctuation.agl",
    Category.PLACEHOLDER: "variable.language.placeholder.agl",
    Category.ERROR: "invalid.illegal.agl",
}


def _captures(rule: Rule, offset: int = 0) -> dict[str, dict[str, str]]:
    return {
        str(offset): {"name": TEXTMATE_SCOPES[rule.category]},
        **{
            str(offset + capture.group): {"name": TEXTMATE_SCOPES[capture.category]}
            for capture in rule.captures
        },
    }


def _validate_line(pattern: str) -> None:
    """Exclude access to TextMate's synthetic LF, absent from model lines."""
    position = 0
    while position < len(pattern):
        character = pattern[position]
        if character == "[":
            end = position + 1 + pattern.startswith("^", position + 1)
            end += pattern.startswith("]", end)
            while pattern[end] != "]":
                end += 2 if pattern[end] == "\\" else 1
            if re.fullmatch(pattern[position : end + 1], "\n") is not None:
                raise ValueError("TextMate character classes must exclude LF")
            position = end + 1
        else:
            width = 2 if character == "\\" else 1
            if pattern[position : position + width] in {r"\n", "\n"}:
                raise ValueError("TextMate patterns cannot inspect LF")
            position += width


def generate(grammar: Grammar) -> str:
    """Lower arbitrary native-framed states without AgL-specific recognition."""
    names = {state.name: f"state{index}" for index, state in enumerate(grammar.states)}
    ends: dict[str, tuple[str, dict[str, dict[str, str]]]] = {}
    for state in grammar.states:
        patterns: list[str] = []
        captures: dict[str, dict[str, str]] = {}
        groups = 0
        nonpop = False
        for rule in state.rules:
            _validate_line(rule.pattern)
            if isinstance(rule.action, Switch):
                raise ValueError("TextMate does not support same-frame Switch")
            if (
                (
                    rule.pattern
                    and not isinstance(rule.action, Pop)
                    and analyze(rule.pattern).nullable
                )
                or isinstance(rule.action, Push)
                and not rule.pattern
            ):
                raise ValueError("TextMate requires consuming Push/match rules")
            if isinstance(rule.action, Pop):
                if state.name == grammar.initial or nonpop:
                    raise ValueError("TextMate requires non-base Pop rules before child patterns")
                patterns.append(f"({rule.pattern})")
                captures.update(_captures(rule, groups + 1))
                groups += rule._compiled.groups + 1
            else:
                nonpop = True
        ends[state.name] = ("|".join(patterns), captures)
    states = {state.name: state for state in grammar.states}
    repository: dict[str, dict[str, list[dict[str, object]]]] = {}
    for state in grammar.states:
        lowered: list[dict[str, object]] = []
        for rule in state.rules:
            if isinstance(rule.action, Pop):
                continue
            if isinstance(rule.action, Push):
                target = states[rule.action.state]
                end, captures = ends[target.name]
                lowered.append(
                    {
                        "begin": rule.pattern,
                        "beginCaptures": _captures(rule),
                        "end": end,
                        "endCaptures": captures,
                        "contentName": TEXTMATE_SCOPES[target.default],
                        "patterns": [{"include": "#" + names[target.name]}],
                    }
                )
            elif rule.pattern:
                lowered.append({"match": rule.pattern, "captures": _captures(rule)})
        # A contentName alone inherits into nested matches; an explicit fallback
        # gives unmatched characters their own deepest category scope.
        lowered.append({"match": r"[^\r\n]", "name": TEXTMATE_SCOPES[state.default]})
        repository[names[state.name]] = {"patterns": lowered}
    document: dict[str, object] = {
        "name": "AgL",
        "scopeName": "source.agl",
        "fileTypes": ["agl"],
        "patterns": [{"include": "#" + names[grammar.initial]}],
        "repository": repository,
    }
    return json.dumps(document, ensure_ascii=True, indent=2) + "\n"
