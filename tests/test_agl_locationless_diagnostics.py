"""A diagnostic about something no source declares has no location and is handled as such."""

from __future__ import annotations

import re

from agnette.agl.diagnostics import Diagnostic, format_diagnostic, format_diagnostic_location
from agnette.agl.repl.entry import relocate_diagnostic
from agnette.agl.scope.symbols import AllowRegion
from agnette.agl.syntax.spans import UNKNOWN_SOURCE, SourceSpan
from agnette.agl.warning_policy import WarningPolicy, apply_warning_policy

_TAB = "tab-indentation"


def test_a_diagnostic_without_a_line_is_formatted_without_a_location() -> None:
    diagnostic = Diagnostic(message="boom", source_label="/a/b.agl")

    for source_name in ("<agl>", None):
        output = format_diagnostic(diagnostic, source_name=source_name)
        assert format_diagnostic_location(diagnostic, source_name=source_name) == ""
        assert not re.search(r":\d+:", output)
        assert "boom" in output


def test_a_located_diagnostic_keeps_its_location() -> None:
    diagnostic = Diagnostic(message="boom", line=3, column=2)

    location = format_diagnostic_location(diagnostic)

    assert location
    assert format_diagnostic(diagnostic).startswith(location)


def test_relocation_leaves_a_diagnostic_without_a_line_in_place() -> None:
    diagnostic = Diagnostic(message="boom")

    def place(location: tuple[str | None, int, int | None]) -> tuple[str | None, int, int | None]:
        return "moved", location[1] + 10, location[2]

    assert relocate_diagnostic(diagnostic, place) == diagnostic


def test_an_allow_region_never_covers_a_warning_without_a_line() -> None:
    warning = Diagnostic(message="tab", severity="warning", warning_name=_TAB)
    everything = SourceSpan(1, 1, 99, 1, 0, 1)
    regions = {UNKNOWN_SOURCE.label: [AllowRegion(everything, frozenset({_TAB}))]}

    outcome = apply_warning_policy([warning], regions, WarningPolicy(), scope_complete=True)

    assert outcome.reported == (warning,)
