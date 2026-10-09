"""Operator-name spelling predicates.

Dependency-free leaf shared by the lexer and the scope passes.
"""

from __future__ import annotations

import unicodedata

# Operator names are Unicode punctuation/symbol runs, minus AgL's structural
# delimiters. Exact reserved spellings such as "=", "==", "->", and "|" are
# retyped by the lexer after maximal munch.
_OPERATOR_NAME_EXCLUDED_CHARS: frozenset[str] = frozenset(
    ("(", ")", "[", "]", "{", "}", ":", ",", ".", ";", '"', "'", "@", "#", "_")
)


def is_operator_name_char(ch: str) -> bool:
    """Return True when *ch* can participate in a standalone operator name."""
    if ch == "" or ch.isspace() or ch in _OPERATOR_NAME_EXCLUDED_CHARS:
        return False
    return unicodedata.category(ch)[0] in ("P", "S")


def is_operator_name(name: str) -> bool:
    """Return True when *name* is spelled entirely with operator-name characters."""
    return name != "" and all(is_operator_name_char(ch) for ch in name)
