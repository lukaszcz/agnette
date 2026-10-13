"""Tests for shared diagnostic helpers in ``agnette.agl.diagnostics``."""

from __future__ import annotations

from agnette.agl.diagnostics import (
    Diagnostic,
    RelatedDiagnostic,
    SourceSpan,
    dollar_spacing_hint,
    format_diagnostic,
    warning_diagnostic,
)
from agnette.agl.warnings import TAB_INDENTATION


class TestDollarSpacingHint:
    def test_name_with_stem_suggests_the_spaced_form(self) -> None:
        hint = dollar_spacing_hint("exec$")
        assert hint is not None
        assert "exec $" in hint

    def test_different_stem_is_reflected_in_the_hint(self) -> None:
        hint = dollar_spacing_hint("ask$")
        assert hint is not None
        assert "ask $" in hint

    def test_name_without_dollar_suffix_has_no_hint(self) -> None:
        assert dollar_spacing_hint("exec") is None

    def test_bare_dollar_has_no_hint(self) -> None:
        """A lone '$' is never lexable as a NAME, but the helper stays total."""
        assert dollar_spacing_hint("$") is None

    def test_empty_name_has_no_hint(self) -> None:
        assert dollar_spacing_hint("") is None


class TestNamedWarningFormatting:
    def test_warning_appends_its_name(self) -> None:
        d = Diagnostic(message="careful", line=3, column=2, severity="warning", warning_name="a-b")
        assert format_diagnostic(d, source_name="p.agl") == "p.agl:3:2: warning: careful [a-b]"

    def test_denied_warning_rendered_as_error_keeps_its_name(self) -> None:
        d = Diagnostic(message="careful", line=3, severity="error", warning_name="a-b")
        assert format_diagnostic(d, source_name=None) == "3: error: careful [a-b]"

    def test_unnamed_diagnostic_has_no_suffix(self) -> None:
        d = Diagnostic(message="careful", line=3, severity="warning")
        assert format_diagnostic(d, source_name=None) == "3: warning: careful"

    def test_name_precedes_related_notes(self) -> None:
        d = Diagnostic(
            message="careful",
            line=3,
            severity="warning",
            warning_name="a-b",
            related=(RelatedDiagnostic(message="see", line=1),),
        )
        assert format_diagnostic(d, source_name=None) == "3: warning: careful [a-b]\n  1: note: see"

    def test_warning_diagnostic_sets_severity_and_name_from_a_catalog_entry(self) -> None:
        span = SourceSpan(4, 1, 4, 5, 30, 34)
        d = warning_diagnostic(TAB_INDENTATION, "careful", span)
        assert (d.severity, d.warning_name, d.line, d.message) == (
            "warning",
            "tab-indentation",
            4,
            "careful",
        )
        assert format_diagnostic(d, source_name=None) == "4:1-4: warning: careful [tab-indentation]"
