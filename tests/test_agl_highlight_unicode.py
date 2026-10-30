"""Exact Unicode vocabularies, independent of portable grammar approximation."""

import importlib
import re
import sys
import unicodedata

import pytest

from agnette.agl.highlight import agl_grammar
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Rule
from agnette.agl.highlight.grammar_eval import evaluate
from agnette.agl.highlight.unicode_snapshot import UNICODE_SNAPSHOT
from agnette.agl.highlight.unicode_vocabulary import (
    scan_unicode_classes,
    unicode_character_classes,
)
from agnette.agl.operator_names import is_operator_name_char
from agnette.util.ident import is_identifier_start


def test_snapshot_is_an_immutable_version_owned_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    version, patterns = UNICODE_SNAPSHOT
    assert isinstance(UNICODE_SNAPSHOT, tuple)
    assert isinstance(patterns, tuple)
    assert len(patterns) == 2
    assert all(isinstance(pattern, str) for pattern in patterns)
    assert isinstance(version, str) and version
    monkeypatch.setattr(unicodedata, "unidata_version", version)
    assert unicode_character_classes() is patterns
    for pattern in patterns:
        Rule(pattern)  # Snapshot data still obeys ordinary portable validation.


@pytest.mark.skipif(
    unicodedata.unidata_version != UNICODE_SNAPSHOT[0], reason="snapshot has a different UCD owner"
)
@pytest.mark.parametrize("first", range(0, sys.maxunicode + 1, 0x10000))
def test_snapshot_matches_every_unicode_point(first: int) -> None:
    assert unicodedata.unidata_version == UNICODE_SNAPSHOT[0]
    patterns = unicode_character_classes()
    assert patterns is UNICODE_SNAPSHOT[1]
    characters = "".join(map(chr, range(first, min(first + 0x10000, sys.maxunicode + 1))))
    expected_start: set[int] = set()
    expected_operator: set[int] = set()
    for offset, character in enumerate(characters):
        if is_identifier_start(character):
            expected_start.add(offset)
        if is_operator_name_char(character):
            expected_operator.add(offset)
    for pattern, expected in zip(patterns, (expected_start, expected_operator), strict=True):
        actual = {match.start() for match in re.compile(pattern).finditer(characters)}
        assert actual == expected


def test_live_scan_matches_the_current_vocabulary() -> None:
    assert scan_unicode_classes() == unicode_character_classes()


def test_unknown_unicode_version_uses_live_platform_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_category = unicodedata.category
    witness = "\U0010ffff"
    assert not is_operator_name_char(witness)

    def changed_category(character: str) -> str:
        return "Sm" if character == witness else original_category(character)

    # Change platform data, not vocabulary internals: stale snapshot reuse is visible.
    with monkeypatch.context() as patch:
        patch.setattr(unicodedata, "unidata_version", "unknown-test-version")
        patch.setattr(unicodedata, "category", changed_category)
        try:
            importlib.reload(agl_grammar)
            assert is_operator_name_char(witness)
            assert evaluate(agl_grammar.AGL_GRAMMAR, witness) == [(0, 1, Category.NAME)]
            assert evaluate(agl_grammar.AGL_GRAMMAR, "Ⅻ") == [(0, 1, Category.ERROR)]
            assert evaluate(agl_grammar.AGL_GRAMMAR, "漢") == [(0, 1, Category.NAME)]
        finally:
            # Restore the shared grammar even if an assertion fails.
            patch.undo()
            importlib.reload(agl_grammar)
    assert evaluate(agl_grammar.AGL_GRAMMAR, witness) == [(0, 1, Category.ERROR)]


@pytest.mark.parametrize(
    "character, start, operator",
    [
        ("_", True, False),
        ("漢", True, False),  # Lo is alphabetic; Nl is not Python isalpha().
        ("Ⅻ", False, False),
        ("\U00020000", True, False),
        ("\U0001d400", True, False),
        ("\U0001f600", False, True),
        ("+", False, True),
        ("(", False, False),
        ("@", False, False),
        ("'", False, False),
        ("\u3000", False, False),
        ("\U0010ffff", False, False),
        ("\ud800", False, False),
    ],
)
def test_unicode_category_boundaries(character: str, start: bool, operator: bool) -> None:
    patterns = unicode_character_classes()
    assert bool(re.fullmatch(patterns[0], character)) == start
    assert bool(re.fullmatch(patterns[1], character)) == operator
    assert is_identifier_start(character) == start
    assert is_operator_name_char(character) == operator
