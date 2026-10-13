"""Tests for the warning policy stage in ``agnette.agl.warning_policy``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from agnette.agl.diagnostics import Diagnostic, SourceSpan, warning_diagnostic
from agnette.agl.modules.roots import RootSet
from agnette.agl.scope.symbols import AllowRegion
from agnette.agl.syntax.spans import UNKNOWN_SOURCE, SourceId
from agnette.agl.warning_policy import (
    WarningOutcome,
    WarningPolicy,
    apply_program_policy,
    apply_warning_policy,
)
from agnette.agl.warnings import NOOP_PARSE_RETRIES, TAB_INDENTATION
from agnette.packages.manifest import load_manifest
from agnette.packages.model import PackageInfo

_MODULE = SourceId("/m/a.agl")
_OTHER = SourceId("/m/b.agl")


def _span(source: SourceId, start: tuple[int, int], end: tuple[int, int]) -> SourceSpan:
    return SourceSpan(*start, *end, 0, 0, source)


def _tab(line: int, column: int = 1, source: SourceId = _MODULE) -> Diagnostic:
    span = _span(source, (line, column), (line, column + 1))
    return warning_diagnostic(TAB_INDENTATION, "tab", span)


def _noop(line: int, column: int = 1, source: SourceId = _MODULE) -> Diagnostic:
    span = _span(source, (line, column), (line, column + 1))
    return warning_diagnostic(NOOP_PARSE_RETRIES, "noop", span)


def _allow(
    start: tuple[int, int], end: tuple[int, int], *names: str, source: SourceId = _MODULE
) -> AllowRegion:
    return AllowRegion(_span(source, start, end), frozenset(names))


def _known(
    *regions: AllowRegion, sources: Sequence[SourceId] = (_MODULE,)
) -> dict[str, list[AllowRegion]]:
    """The regions of the *sources* whose regions are known, each region under its own source."""
    known: dict[str, list[AllowRegion]] = {source.label: [] for source in sources}
    for region in regions:
        known.setdefault(region.span.source.label, []).append(region)
    return known


def _apply(
    warnings: Sequence[Diagnostic],
    regions: Mapping[str, Sequence[AllowRegion]],
    policy: WarningPolicy,
    *,
    scope_complete: bool = False,
    capped_sources: frozenset[str] = frozenset(),
) -> WarningOutcome:
    return apply_warning_policy(
        warnings,
        regions,
        policy,
        scope_complete=scope_complete,
        capped_sources=capped_sources,
    )


def _lines(diagnostics: tuple[Diagnostic, ...]) -> list[tuple[int, str | None]]:
    return [(d.line, d.warning_name) for d in diagnostics]


def test_levels_default_to_the_catalog_and_overrides_win() -> None:
    policy = WarningPolicy({"tab-indentation": "deny"})

    assert WarningPolicy().level("tab-indentation") == "warn"
    assert policy.level("tab-indentation") == "deny"
    assert policy.level("noop-parse-retries") == "warn"


def test_warn_level_reports_in_order_and_denies_nothing() -> None:
    outcome = _apply([_tab(3), _noop(1)], _known(), WarningPolicy())

    assert _lines(outcome.reported) == [(3, "tab-indentation"), (1, "noop-parse-retries")]
    assert outcome.denied == ()


def test_unnamed_diagnostics_are_not_policy_warnings() -> None:
    outcome = _apply([replace(_tab(1), warning_name=None), _noop(2)], _known(), WarningPolicy())

    assert _lines(outcome.reported) == [(2, "noop-parse-retries")]


def test_allow_level_drops_only_that_warning() -> None:
    policy = WarningPolicy({"tab-indentation": "allow"})

    outcome = _apply([_tab(1), _noop(2)], _known(), policy)

    assert _lines(outcome.reported) == [(2, "noop-parse-retries")]
    assert outcome.denied == ()


def test_deny_level_turns_a_warning_into_a_named_error() -> None:
    policy = WarningPolicy({"tab-indentation": "deny"})

    outcome = _apply([_tab(1), _noop(2)], _known(), policy)

    assert _lines(outcome.reported) == [(2, "noop-parse-retries")]
    (error,) = outcome.denied
    assert (error.severity, error.warning_name, error.line) == ("error", "tab-indentation", 1)
    assert error.message == "tab"


def test_region_hides_only_the_names_it_lists() -> None:
    region = _allow((2, 3), (5, 1), "tab-indentation")

    outcome = _apply([_tab(3), _noop(3), _tab(7)], _known(region), WarningPolicy())

    assert _lines(outcome.reported) == [(3, "noop-parse-retries"), (7, "tab-indentation")]


def test_region_is_matched_against_the_module_the_warning_is_anchored_in() -> None:
    region = _allow((1, 1), (9, 1), "tab-indentation")
    known = _known(region, sources=(_MODULE, _OTHER))

    outcome = _apply([_tab(3, source=_OTHER)], known, WarningPolicy())

    assert _lines(outcome.reported) == [(3, "tab-indentation")]


def test_region_contains_from_its_start_up_to_its_exclusive_end() -> None:
    region = _allow((4, 5), (6, 8), "tab-indentation")
    inside = [_tab(4, 5), _tab(5, 40), _tab(6, 7)]
    outside = [_tab(3, 9), _tab(4, 4), _tab(6, 8), _tab(7, 1)]

    outcome = _apply([*inside, *outside], _known(region), WarningPolicy())

    assert [(d.line, d.column) for d in outcome.reported] == [(3, 9), (4, 4), (6, 8), (7, 1)]


def test_a_warning_without_a_source_label_belongs_to_the_unknown_source() -> None:
    unlabelled = Diagnostic(
        message="tab", line=8, column=1, severity="warning", warning_name="tab-indentation"
    )
    unknown = AllowRegion(SourceSpan(8, 1, 9, 1, 0, 0), frozenset({"tab-indentation"}))

    hidden = _apply([unlabelled], {UNKNOWN_SOURCE.label: [unknown]}, WarningPolicy())
    kept = _apply([unlabelled], {UNKNOWN_SOURCE.label: []}, WarningPolicy())

    assert hidden.reported == ()
    assert kept.reported == (unlabelled,)


def test_a_region_hides_a_warning_before_its_level_applies() -> None:
    region = _allow((1, 1), (9, 1), "tab-indentation")

    outcome = _apply([_tab(3)], _known(region), WarningPolicy({"tab-indentation": "deny"}))

    assert outcome.reported == () and outcome.denied == ()


def test_a_warning_of_a_module_with_unknown_regions_is_withheld_at_every_level() -> None:
    policy = WarningPolicy({"tab-indentation": "deny", "noop-parse-retries": "warn"})

    outcome = _apply([_tab(1, source=_OTHER), _noop(2, source=_OTHER), _tab(3)], _known(), policy)

    assert outcome.reported == ()
    assert _lines(outcome.denied) == [(3, "tab-indentation")]


def test_capped_sources_are_dropped_even_when_denied() -> None:
    policy = WarningPolicy({"tab-indentation": "deny"})

    outcome = _apply(
        [_tab(1, source=_OTHER), _tab(2)],
        _known(sources=(_MODULE, _OTHER)),
        policy,
        capped_sources=frozenset({_OTHER.label}),
    )

    assert outcome.reported == ()
    assert [d.line for d in outcome.denied] == [2]


def test_a_source_without_regions_has_none_once_scope_resolved_the_whole_program() -> None:
    policy = WarningPolicy({"tab-indentation": "deny"})
    warnings = [_tab(1, source=_OTHER), _noop(2, source=_OTHER)]

    failed = _apply(warnings, _known(), policy, scope_complete=False)
    resolved = _apply(warnings, _known(), policy, scope_complete=True)

    assert failed.reported == () and failed.denied == ()
    assert _lines(resolved.reported) == [(2, "noop-parse-retries")]
    assert _lines(resolved.denied) == [(1, "tab-indentation")]


def _installed_package(root: Path) -> PackageInfo:
    (root / "src").mkdir(parents=True)
    (root / "package.toml").write_text('[package]\nname = "dep"\nversion = "1.0.0"\n')
    package = PackageInfo(root, load_manifest(root / "package.toml"))
    return replace(package, installed=True)


def test_program_policy_caps_the_sources_the_roots_call_installed(tmp_path: Path) -> None:
    package = _installed_package(tmp_path / "dep")
    roots = RootSet(roots=frozenset(), packages=(package,))
    installed = SourceId(str(package.module_root / "lib.agl"))
    policy = WarningPolicy({"tab-indentation": "deny"})

    for scope_complete in (True, False):
        outcome = apply_program_policy(
            [_tab(1, source=installed), _tab(2, source=_MODULE)],
            [],
            policy,
            scope_complete=scope_complete,
            roots=roots,
        )

        assert [d.line for d in outcome.denied] == ([2] if scope_complete else [])
        assert outcome.reported == ()


def test_program_policy_classifies_only_absolute_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synthetic label never names a file, wherever the working directory is."""
    package = _installed_package(tmp_path / "dep")
    roots = RootSet(roots=frozenset(), packages=(package,))
    monkeypatch.chdir(package.module_root)

    outcome = apply_program_policy(
        [_tab(1, source=SourceId("<stdlib-import>")), _tab(2, source=SourceId("main.agl"))],
        [],
        WarningPolicy(),
        scope_complete=True,
        roots=roots,
    )

    assert [d.line for d in outcome.reported] == [1, 2]


def test_program_policy_without_roots_caps_nothing() -> None:
    outcome = apply_program_policy([_tab(1)], [], WarningPolicy(), scope_complete=True, roots=None)

    assert [d.line for d in outcome.reported] == [1]
