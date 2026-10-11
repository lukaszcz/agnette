"""Immutable, target-neutral regex states for generated highlighting grammars.

The portable subset uses explicit character sets, ordinary/noncapturing groups,
lookarounds (fixed-width behind), alternation, greedy/lazy repetition and line
anchors. Lookbehind excludes lookahead; positive behind also excludes negative
behind, and negative behind excludes captures. Assertions cannot be repeated.
Braced repetition requires {n}, {n,} or {n,m} with ASCII bounds <=100000; literal
braces must be escaped outside classes. Engine-specific escapes, flags, groups
and possessive repetition are excluded. Captures selected for styling must be
disjoint and outside lookarounds. Rules see one line without its newline; only
the state stack survives line boundaries.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from agnette.agl.highlight.categories import Category
from agnette.util.ident import IDENT_STOP, is_identifier


@dataclass(frozen=True, slots=True)
class Push:
    """Enter a state, retaining the current state as its parent."""

    state: str


@dataclass(frozen=True, slots=True)
class Pop:
    """Remove one frame, retaining the base frame."""


@dataclass(frozen=True, slots=True)
class Switch:
    """Replace the current state without changing its parent."""

    state: str


type Action = Push | Pop | Switch


@dataclass(frozen=True, slots=True)
class Capture:
    """Style one numbered capture group."""

    group: int
    category: Category


@dataclass(slots=True)
class _RegexGroup:
    """Track captures, assertion ancestry and noncapturing repeat targets."""

    kind: str = ""
    number: int | None = None
    atoms: list[bool] = field(default_factory=list)
    repeatable: bool = True

    def finish_branch(self) -> None:
        # Oniguruma rejects an assertion alone, but permits concatenations.
        self.repeatable &= len(self.atoms) != 1 or self.atoms[0]
        self.atoms.clear()


def _compile(pattern: str, selected: set[int]) -> re.Pattern[str]:
    """Validate syntax shared with KDE/Oniguruma before compiling for Python."""
    position = 0
    in_class = False
    class_start = 0
    group_number = 0
    groups = [_RegexGroup()]
    while position < len(pattern):
        character = pattern[position]
        if character == "\\":
            position += 1
            if position == len(pattern) or (
                pattern[position].isalnum() and pattern[position] not in "nrtf"
            ):
                raise ValueError("Use explicit character sets, not engine-specific escapes")
            if not in_class:
                groups[-1].atoms.append(True)
        elif character == "[":
            if in_class:
                raise ValueError("Nested/POSIX character classes are not portable")
            in_class = True
            class_start = position + 1 + pattern.startswith("^", position + 1)
        elif character == "]":
            if not in_class or position != class_start:
                in_class = False
                groups[-1].atoms.append(True)
        elif in_class:
            if character in "&~|-" and pattern.startswith(character, position + 1):
                raise ValueError("Character-class set operations are not portable")
        else:
            if character == "(":
                number: int | None = None
                kind = "("
                if pattern.startswith("(?", position):
                    kind = next(
                        (
                            prefix
                            for prefix in ("(?:", "(?=", "(?!", "(?<=", "(?<!")
                            if pattern.startswith(prefix, position)
                        ),
                        "",
                    )
                    if not kind:
                        raise ValueError("Unsupported group construct")
                    if kind in {"(?=", "(?!"} and any(
                        group.kind in {"(?<=", "(?<!"} for group in groups
                    ):
                        raise ValueError("Lookahead inside lookbehind is not portable")
                    if kind == "(?<!" and any(group.kind == "(?<=" for group in groups):
                        raise ValueError("Negative lookbehind inside positive is not portable")
                else:
                    group_number += 1
                    number = group_number
                    if any(group.kind == "(?<!" for group in groups):
                        raise ValueError("Captures inside negative lookbehind are not portable")
                    if number in selected and any(
                        group.number in selected or group.kind not in {"", "(", "(?:"}
                        for group in groups
                    ):
                        raise ValueError("Styled captures must be disjoint and consuming")
                groups.append(_RegexGroup(kind, number))
                position += len(kind) - 1
            elif character == ")" and len(groups) > 1:
                group = groups.pop()
                group.finish_branch()
                groups[-1].atoms.append(
                    group.kind == "(" or (group.kind == "(?:" and group.repeatable)
                )
            elif character == "|":
                groups[-1].finish_branch()
            elif character == "}":
                raise ValueError("Escape literal braces")
            elif character in "*+?{":
                if character == "{":
                    quantifier = re.match(r"\{[0-9]+(?:,[0-9]*)?\}", pattern[position:])
                    if quantifier is None:
                        raise ValueError("Use {n}, {n,} or {n,m}; escape literal braces")
                    for digits in quantifier.group()[1:-1].split(","):
                        bound = digits.lstrip("0")
                        if len(bound) > 6 or (len(bound) == 6 and bound > "100000"):
                            raise ValueError("Portable repeat bounds must not exceed 100000")
                    position += quantifier.end() - 1
                if groups[-1].atoms and not groups[-1].atoms[-1]:
                    raise ValueError("Assertions cannot be repeated")
                if pattern.startswith("+", position + 1):
                    raise ValueError("Possessive repetition is not in the portable subset")
            else:
                groups[-1].atoms.append(character not in "^$")
        position += 1
    try:
        # Python also checks that lookbehind has fixed width.
        return re.compile(pattern, re.MULTILINE)
    except re.error as error:
        raise ValueError("Invalid or nonportable regex") from error


@dataclass(frozen=True, slots=True)
class Rule:
    """An ordered regex, a match background, optional captures and a transition."""

    pattern: str
    category: Category = Category.TEXT
    captures: tuple[Capture, ...] = ()
    action: Action | None = None
    _compiled: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        selected = {capture.group for capture in self.captures}
        compiled = _compile(self.pattern, selected)
        if len(selected) != len(self.captures) or any(
            group < 1 or group > compiled.groups for group in selected
        ):
            raise ValueError("Capture groups must exist and be unique")
        object.__setattr__(self, "_compiled", compiled)


@dataclass(frozen=True, slots=True)
class State:
    """Named ordered rules; unmatched characters receive the default category."""

    name: str
    rules: tuple[Rule, ...]
    default: Category = Category.TEXT


@dataclass(frozen=True, slots=True)
class Grammar:
    """Named states with validated destinations, independent of target lowering.

    Pop removes one frame, Switch replaces one frame, and Push retains its parent.
    Built-in states use these finite operations directly; line-end recovery chains
    single-frame pops rather than requiring a stack reset.
    """

    states: tuple[State, ...]
    initial: str = "root"

    def __post_init__(self) -> None:
        by_name = {state.name: state for state in self.states}
        if "" in by_name or len(by_name) != len(self.states) or self.initial not in by_name:
            raise ValueError("States must have unique nonempty names and contain the initial state")
        for state in self.states:
            for rule in state.rules:
                action = rule.action
                if isinstance(action, (Push, Switch)):
                    if action.state not in by_name:
                        raise ValueError("Unknown transition state")
                    target = by_name[action.state]
                    if isinstance(action, Push) and (
                        not target.rules or not isinstance(target.rules[0].action, Pop)
                    ):
                        raise ValueError("A pushed state's pop rule must come first")


def character_class(characters: Iterable[str]) -> str:
    """Spell an explicit, range-compressed Unicode set in the portable subset."""
    points = sorted({ord(character) for character in characters})
    if not points:
        raise ValueError("Character classes must be nonempty")
    ranges: list[str] = []
    escapes = {"\n": r"\n", "\r": r"\r", "\t": r"\t", "\f": r"\f"}

    def escaped(point: int) -> str:
        character = chr(point)
        return escapes.get(character, re.escape(character))

    start = previous = points[0]
    for point in (*points[1:], -1):
        if point == previous + 1:
            previous = point
            continue
        ranges.append(
            escaped(start) if start == previous else f"{escaped(start)}-{escaped(previous)}"
        )
        start = previous = point
    return "[" + "".join(ranges) + "]"


def keyword_rule(
    words: Iterable[str],
    category: Category,
    *,
    action: Action | None = None,
    token_start: bool = False,
) -> Rule:
    """Use identifier stops; omit the left boundary only at caller-owned token starts."""
    inventory = sorted(set(words))
    inventory.sort(key=len, reverse=True)
    if not inventory or any(not is_identifier(word) for word in inventory):
        raise ValueError("Keyword inventories must contain complete identifiers")
    stops = character_class(IDENT_STOP)[1:-1]
    alternatives = "|".join(re.escape(word) for word in inventory)
    before = "" if token_start else f"(?<![^{stops}])"
    return Rule(f"{before}(?:{alternatives})(?![^{stops}])", category, action=action)
