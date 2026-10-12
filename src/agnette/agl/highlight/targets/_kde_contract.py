"""Conservative native-stack contract; restrictions do not alter the generic model."""

import re

from agnette.agl.highlight.grammar import Grammar, Pop, Push, Rule, Switch, validate_pattern
from agnette.agl.highlight.targets._regex import Structure

_XML_FORBIDDEN = re.compile(r"[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _xml_characters(value: str) -> None:
    """Require XML 1.0 characters; entity escaping cannot encode forbidden literals."""
    if match := _XML_FORBIDDEN.search(value):
        raise ValueError(f"KDE XML 1.0 cannot represent U+{ord(match[0]):04X}")


def _reachable(edges: dict[str, set[str]], start: str) -> set[str]:
    found: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(edges[current] - found)
    return found


def validate(grammar: Grammar, structures: dict[Rule, Structure]) -> None:
    """Reject XML-forbidden literals and unsupported regex/stack behavior.

    Reachability ignores regex feasibility: accepting a transition graph must not
    depend on proving its patterns mutually exclusive. Pop-only cycles are safe
    because each native pop decreases stack depth.
    """
    switches = {state.name: set[str]() for state in grammar.states}
    for state in grammar.states:
        _xml_characters(state.name)
        for rule in state.rules:
            _xml_characters(rule.pattern)
            validate_pattern(rule.pattern, repeat_limit=65535)
            for word in rule.words:
                _xml_characters(word)
            facts = structures[rule]
            if facts.expanding_group_repeat:
                raise ValueError("KDE group repeat bounds must not exceed 1: PCRE2 expands groups")
            if facts.lookbehind:
                raise ValueError("KDE cannot preserve lookbehind after consumed text")
            if facts.start_anchor and not facts.leading_anchor_only:
                raise ValueError(
                    "KDE supports only a leading anchor without top-level alternatives"
                )
            if isinstance(rule.action, Switch):
                switches[state.name].add(rule.action.state)
    same_frame = {name: _reachable(switches, name) for name in switches}
    base = same_frame[grammar.initial]
    parents = {state.name: set[str]() for state in grammar.states}
    for state in grammar.states:
        for rule in state.rules:
            if isinstance(rule.action, Pop) and state.name in base:
                raise ValueError("KDE cannot preserve a base-frame pop")
            if isinstance(rule.action, Push):
                for name in same_frame[rule.action.state]:
                    parents[name].add(state.name)

    zero = {state.name: set[str]() for state in grammar.states}
    guarded: list[tuple[str, str]] = []
    for state in grammar.states:
        for rule in state.rules:
            if not structures[rule].nullable:
                continue
            if isinstance(rule.action, Pop):
                zero[state.name].update(parents[state.name])
            elif isinstance(rule.action, Push | Switch):
                zero[state.name].add(rule.action.state)
                guarded.append((state.name, rule.action.state))
    for origin, destination in guarded:
        if origin in _reachable(zero, destination):
            raise ValueError("KDE cannot preserve a zero-width transition cycle")
