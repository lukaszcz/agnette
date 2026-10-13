"""Tests for the static warning catalog in ``agnette.agl.warnings``."""

from __future__ import annotations

import pytest

from agnette.agl.warnings import (
    EXEMPTABLE_WARNINGS,
    NOOP_PARSE_RETRIES,
    SHELL_QUOTED_HOLE,
    TAB_INDENTATION,
    UNQUOTED_EXEC_HOLE,
    WARNING_CATALOG,
    WARNING_DEFAULT_LEVELS,
    StaticWarning,
)


@pytest.mark.parametrize(
    ("entry", "name", "exemptable"),
    [
        (TAB_INDENTATION, "tab-indentation", False),
        (NOOP_PARSE_RETRIES, "noop-parse-retries", False),
        (UNQUOTED_EXEC_HOLE, "unquoted-exec-hole", True),
        (SHELL_QUOTED_HOLE, "shell-quoted-hole", False),
    ],
)
def test_catalog_entries_carry_name_default_level_and_exemptability(
    entry: StaticWarning, name: str, exemptable: bool
) -> None:
    assert WARNING_CATALOG[name] is entry
    assert (entry.name, entry.default_level, entry.exemptable) == (name, "warn", exemptable)


def test_catalog_lists_exactly_the_four_warnings() -> None:
    assert set(WARNING_CATALOG) == {
        "tab-indentation",
        "noop-parse-retries",
        "unquoted-exec-hole",
        "shell-quoted-hole",
    }
    assert all(name == entry.name for name, entry in WARNING_CATALOG.items())


def test_derived_views_follow_the_entries() -> None:
    assert set(WARNING_DEFAULT_LEVELS.items()) == {(name, "warn") for name in WARNING_CATALOG}
    assert EXEMPTABLE_WARNINGS == {"unquoted-exec-hole"}


def test_an_unknown_name_is_not_in_the_catalog() -> None:
    assert "no-such-warning" not in WARNING_CATALOG
