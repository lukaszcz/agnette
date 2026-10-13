"""Conservative POSIX shell quote-state scan over a template's literal fragments.

:func:`hole_quote_states` reports, for each hole between consecutive fragments, whether it
sits unquoted, in ``'…'`` or in ``"…"``. A :attr:`HoleKind.WORD` hole stands for text that
leaves the quote state unchanged (it may be empty); an :attr:`HoleKind.OPAQUE` hole may
hold anything, so nothing after it is judged. The scan models only quotes and backslash
escapes: at the first construct whose effect on quoting it does not model exactly, it stops
judging and every later hole is :attr:`QuoteState.UNKNOWN`. Those constructs are command and
parameter expansions (``$(``, ``${``, a backtick), ``$'``, heredocs and here-strings
(``<<``), comments (``#`` at the start of a word), a backslash-newline outside ``'…'``, and
a backslash, ``$`` or unquoted ``<`` directly before a hole, which an empty hole could join
to the text after it.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum, auto

__all__ = ["HoleKind", "QuoteState", "hole_quote_states"]


class QuoteState(Enum):
    """The shell quoting context of a hole."""

    UNQUOTED = auto()
    SINGLE = auto()
    DOUBLE = auto()
    UNKNOWN = auto()


class HoleKind(Enum):
    """What the scan may assume about a hole's text."""

    WORD = auto()
    OPAQUE = auto()


#: Unquoted characters after which a new word starts: blanks and token-delimiting operators.
_WORD_BREAKS = frozenset(" \t\n;&|()<>")

#: The scan position before any text; ``#`` here starts a word.
_START = ""


class _Unmodeled(Exception):
    """The scan met a construct whose effect on quoting it does not model."""


def hole_quote_states(
    fragments: Sequence[str], kinds: Sequence[HoleKind]
) -> tuple[QuoteState, ...]:
    """Return the quote state of each hole between consecutive *fragments*.

    *fragments* are the decoded literal texts around ``n`` holes (``n + 1`` fragments);
    *kinds* gives the kind of each hole.
    """
    states: list[QuoteState] = []
    state = QuoteState.UNQUOTED
    previous: str | None = _START
    try:
        for index, fragment in enumerate(fragments):
            if index:
                states.append(state)
                if kinds[index - 1] is HoleKind.OPAQUE:
                    raise _Unmodeled
                previous = None
            state, previous = _scan(fragment, state, previous, index + 1 < len(fragments))
    except _Unmodeled:
        states.extend([QuoteState.UNKNOWN] * (len(fragments) - 1 - len(states)))
    return tuple(states)


def _scan(
    fragment: str, state: QuoteState, previous: str | None, hole_follows: bool
) -> tuple[QuoteState, str | None]:
    """Scan *fragment* from *state*; return the state and the last unquoted-context char.

    *previous* is the char before the fragment: :data:`_START` at the template start, ``None``
    right after a hole. Raises :class:`_Unmodeled` at an unmodelled construct.
    """
    position = 0
    while position < len(fragment):
        char = fragment[position]
        following = fragment[position + 1 : position + 2]
        at_end = not following and hole_follows
        if state is QuoteState.SINGLE:
            if char == "'":
                state = QuoteState.UNQUOTED
        elif char == "\\":
            if at_end or following == "\n":
                raise _Unmodeled
            position += 1
        elif char == "`" or (char == "$" and (at_end or following in ("(", "{", "'"))):
            raise _Unmodeled
        elif char == '"':
            state = QuoteState.UNQUOTED if state is QuoteState.DOUBLE else QuoteState.DOUBLE
        elif state is QuoteState.UNQUOTED:
            if char == "'":
                state = QuoteState.SINGLE
            elif (char == "<" and (following == "<" or at_end)) or (
                char == "#" and _starts_word(previous)
            ):
                raise _Unmodeled
        previous = char
        position += 1
    return state, previous


def _starts_word(previous: str | None) -> bool:
    """Whether an unquoted char after *previous* may begin a word (``None``: after a hole)."""
    return previous is None or previous == _START or previous in _WORD_BREAKS
