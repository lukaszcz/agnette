"""Sublime Text syntax (YAML) for syntect-based tools such as bat.

Each state becomes a context whose ordered rules end in a one-character
fallback, so a context's leftmost match is the model's first rule matching at
the current position. Scopes follow TextMate. Lines include their LF; nullable
match/push/switch rules and base-frame pops or switches have no loop-free
native equivalent.
"""

from agnette.agl.highlight.grammar import Grammar, Pop, Push, Rule, Switch
from agnette.agl.highlight.targets._regex import analyze, validate_line
from agnette.agl.highlight.targets.textmate import TEXTMATE_SCOPES

_FALLBACK = r"[^\r\n]"


def _quote(text: str) -> str:
    """Spell an ASCII-only, double-quoted YAML 1.2 scalar."""
    characters: list[str] = []
    for character in text:
        point = ord(character)
        if character in '"\\':
            characters.append("\\" + character)
        elif 0x20 <= point < 0x7F:
            characters.append(character)
        elif point <= 0xFFFF:
            characters.append(f"\\u{point:04X}")
        else:
            characters.append(f"\\U{point:08X}")
    return '"' + "".join(characters) + '"'


def _entry(rule: Rule, contexts: dict[str, str]) -> list[str]:
    lines = [
        f"    - match: {_quote(rule.pattern)}",
        f"      scope: {TEXTMATE_SCOPES[rule.category]}",
    ]
    if rule.captures:
        lines.append("      captures:")
        lines.extend(
            f"        {capture.group}: {TEXTMATE_SCOPES[capture.category]}"
            for capture in rule.captures
        )
    action = rule.action
    if isinstance(action, Pop):
        lines.append("      pop: true")
    elif isinstance(action, Push):
        lines.append(f"      push: {contexts[action.state]}")
    elif isinstance(action, Switch):
        lines.append(f"      set: {contexts[action.state]}")
    return lines


def generate(grammar: Grammar) -> str:
    """Lower arbitrary states without AgL-specific recognition."""
    contexts = {
        state.name: "main" if state.name == grammar.initial else f"state{index}"
        for index, state in enumerate(grammar.states)
    }
    lines = [
        "%YAML 1.2",
        "---",
        "name: AgL",
        "file_extensions: [agl]",
        "scope: source.agl",
        "contexts:",
    ]
    for state in grammar.states:
        lines.append(f"  {contexts[state.name]}:")
        for rule in state.rules:
            validate_line(rule.pattern)
            base = state.name == grammar.initial
            if isinstance(rule.action, (Pop, Switch)) and base:
                raise ValueError("Sublime contexts cannot pop or replace the base frame")
            if not isinstance(rule.action, Pop) and analyze(rule.pattern).nullable:
                if rule.action is None and not rule.pattern:
                    continue  # Never styles or transitions.
                raise ValueError("Sublime requires consuming match/push/switch rules")
            lines.extend(_entry(rule, contexts))
        lines.append(f"    - match: {_quote(_FALLBACK)}")
        lines.append(f"      scope: {TEXTMATE_SCOPES[state.default]}")
    return "\n".join(lines) + "\n"
