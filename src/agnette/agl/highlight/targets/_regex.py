"""Structural facts for finite target lowering, independent of regex engine internals."""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import cast

from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Capture


@dataclass(slots=True)
class _Group:
    start: int = 0
    number: int | None = None
    assertion: bool = False
    atoms: list[bool] = field(default_factory=list)
    alternative: bool = False
    last_group: bool = False

    def add_atom(self, nullable: bool, *, group: bool = False) -> None:
        self.atoms.append(nullable)
        self.last_group = group

    def nullable(self) -> bool:
        return self.alternative or all(self.atoms)


@dataclass(frozen=True, slots=True)
class Structure:
    """Linear capture partitions and conservative zero-width/end dependencies."""

    parts: tuple[tuple[str, Category], ...]
    nullable: bool
    end_anchor: bool
    start_anchor: bool
    lookbehind: bool
    leading_anchor_only: bool
    expanding_group_repeat: bool


def _parse_structure(
    pattern: str, background: Category, captures: tuple[Capture, ...]
) -> Structure:
    """Parse already-validated model syntax; assertions have zero width."""
    selected = {capture.group: capture.category for capture in captures}
    extents: list[tuple[int, int, Category]] = []
    stack = [_Group()]
    group = 0
    position = 0
    class_start: int | None = None
    end_anchor = start_anchor = lookbehind = False
    misplaced_anchor = top_alternative = expanding_group_repeat = False
    while position < len(pattern):
        character = pattern[position]
        if character == "\\":
            if class_start is None:
                stack[-1].add_atom(False)
            position += 2
            continue
        if class_start is not None:
            if character == "]" and position != class_start:
                class_start = None
                stack[-1].add_atom(False)
        elif character == "[":
            class_start = position + 1 + pattern.startswith("^", position + 1)
        elif character == "(":
            number = None
            opener = "("
            if pattern.startswith("(?", position):
                opener = next(
                    prefix
                    for prefix in ("(?:", "(?=", "(?!", "(?<=", "(?<!")
                    if pattern.startswith(prefix, position)
                )
                lookbehind |= opener in {"(?<=", "(?<!"}
            else:
                group += 1
                number = group
                if number in selected and len(stack) > 1:
                    raise ValueError("Styled captures must be top-level partitions")
            stack.append(_Group(position, number, opener not in {"(", "(?:"}))
            position += len(opener) - 1
        elif character == ")":
            closed = stack.pop()
            stack[-1].add_atom(closed.assertion or closed.nullable(), group=True)
            if closed.number in selected:
                end = position + 1
                if end < len(pattern) and pattern[end] in "*+{":
                    raise ValueError("Styled captures cannot be repeated")
                if pattern.startswith("?", end):
                    end += 1 + pattern.startswith("?", end + 1)
                extents.append((closed.start, end, selected[closed.number]))
        elif character == "|":
            top_alternative |= len(stack) == 1
            if selected and len(stack) == 1:
                raise ValueError("Capture partitions cannot cross alternatives")
            stack[-1].alternative |= all(stack[-1].atoms)
            stack[-1].atoms.clear()
            stack[-1].last_group = False
        elif character in "*+?{":
            optional = character in "*?"
            if character == "{":
                end = pattern.index("}", position)
                bounds = pattern[position + 1 : end].split(",")
                optional = int(bounds[0]) == 0
                expanding_group_repeat |= stack[-1].last_group and any(
                    int(bound) > 1 for bound in bounds if bound
                )
                position = end
            stack[-1].atoms[-1] |= optional
            position += pattern.startswith("?", position + 1)
        else:
            end_anchor |= character == "$"
            start_anchor |= character == "^"
            misplaced_anchor |= character == "^" and position != 0
            stack[-1].add_atom(character in "^$")
        position += 1
    parts: list[tuple[str, Category]] = []
    cursor = 0
    for start, end, category in extents:
        if start > cursor:
            parts.append((pattern[cursor:start], background))
        parts.append((pattern[start:end], category))
        cursor = end
    if cursor < len(pattern):
        parts.append((pattern[cursor:], background))
    return Structure(
        tuple(parts),
        stack[0].nullable(),
        end_anchor,
        start_anchor,
        lookbehind,
        not misplaced_anchor and not top_alternative,
        expanding_group_repeat,
    )


_cached_structure = cast(
    Callable[[str, Category, tuple[Capture, ...]], Structure],
    lru_cache(maxsize=512)(_parse_structure),
)


def analyze(
    pattern: str, background: Category = Category.TEXT, captures: tuple[Capture, ...] = ()
) -> Structure:
    """Reuse immutable lowering facts; grammar/target validation remains caller-owned.

    All parse inputs form the cache key. Bound retention for arbitrary user grammars.
    """
    return _cached_structure(pattern, background, captures)
