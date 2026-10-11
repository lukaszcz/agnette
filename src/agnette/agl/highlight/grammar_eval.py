"""Reference interpreter for grammar conformance and generator tests."""

from __future__ import annotations

import re

from agnette.agl.highlight.categories import Category, Span
from agnette.agl.highlight.grammar import Grammar, Pop, Push, Rule, State, Switch


def _append(spans: list[Span], start: int, end: int, category: Category) -> None:
    if start == end:
        return
    if spans and spans[-1][2] == category:
        previous = spans[-1]
        spans[-1] = (previous[0], end, category)
    else:
        spans.append((start, end, category))


def _match_spans(spans: list[Span], rule: Rule, match: re.Match[str], offset: int) -> None:
    cursor = offset + match.start()
    captures = sorted(
        (offset + match.start(capture.group), offset + match.end(capture.group), capture.category)
        for capture in rule.captures
        if match.end(capture.group) > match.start(capture.group)
    )
    for start, end, category in captures:
        _append(spans, cursor, start, rule.category)
        _append(spans, start, end, category)
        cursor = end
    _append(spans, cursor, offset + match.end(), rule.category)


def _evaluate_line(
    states: dict[str, State], stack: list[str], spans: list[Span], line: str, offset: int
) -> None:
    position = 0
    visited: set[tuple[str, int]] = set()
    while True:
        state = states[stack[-1]]
        for index, rule in enumerate(state.rules):
            key = (state.name, index)
            unwinding = isinstance(rule.action, Pop) and len(stack) > 1
            if key in visited and not unwinding:
                continue
            match = rule._compiled.match(line, position)
            if match is None:
                continue
            if match.end() == position:
                if rule.action is None:
                    continue
                # A real pop decreases depth, so repeated recursive frames
                # can unwind. Push/switch revisits remain guarded against cycles.
                if not unwinding:
                    visited.add(key)
            else:
                visited.clear()
            _match_spans(spans, rule, match, offset)
            position = match.end()
            action = rule.action
            if isinstance(action, Push):
                stack.append(action.state)
            elif isinstance(action, Pop):
                if len(stack) > 1:
                    stack.pop()
            elif isinstance(action, Switch):
                stack[-1] = action.state
            break
        else:
            if position == len(line):
                break
            _append(spans, offset + position, offset + position + 1, state.default)
            position += 1
            visited.clear()


def evaluate(grammar: Grammar, source: str) -> list[Span]:
    """Partition source using line-local rules and a persistent state stack.

    LF, CRLF and CR delimit lines, are invisible to rules and receive TEXT even
    inside unclosed states. Other Unicode separators remain ordinary content.
    Offsets refer to the unchanged source. Empty lines and each line's end run
    zero-width transitions, including the final line at EOF.

    Zero-width push/switch and base-frame pop rules run once per (state, rule)
    at each offset. Other pops decrease depth and may repeat to unwind recursion.
    Nontransitioning empty matches are skipped.
    """
    states = {state.name: state for state in grammar.states}
    stack = [grammar.initial]
    spans: list[Span] = []
    offset = 0
    for newline in re.finditer(r"\r\n|\r|\n", source):
        _evaluate_line(states, stack, spans, source[offset : newline.start()], offset)
        _append(spans, newline.start(), newline.end(), Category.TEXT)
        offset = newline.end()
    _evaluate_line(states, stack, spans, source[offset:], offset)
    return spans
