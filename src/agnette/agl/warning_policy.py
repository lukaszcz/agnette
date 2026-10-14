"""The per-invocation policy that decides which static warnings reach the user.

The static passes report every warning they find, and caches keep those raw
warnings. A host then applies a :class:`WarningPolicy` to one invocation's
warnings with :func:`apply_warning_policy`, in order:

1. drop warnings anchored in capped sources: modules of installed
   dependencies (store packages, the store or shipped standard library), whose
   warnings the user cannot act on, whatever their level;
2. drop warnings inside an ``@allow`` region that names them;
3. apply the configured level: ``allow`` drops, ``warn`` reports, ``deny``
   turns the warning into an error that keeps its catalog name.

A warning is matched against the regions of the module it is anchored in, by
the source label its diagnostic and each module's regions carry. A warning of
a module whose regions are unknown is withheld: scope never resolved it because
a pass failed first, so its ``@allow`` cannot be honoured and the warning is
neither reported nor denied. When scope resolved the whole program, a source
without regions simply has none.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, assert_never

from agnette.agl.diagnostics import Diagnostic
from agnette.agl.syntax.spans import UNKNOWN_SOURCE
from agnette.agl.warnings import WARNING_CATALOG, WarningLevel

if TYPE_CHECKING:
    from agnette.agl.modules.roots import RootSet
    from agnette.agl.scope.program import ResolvedModule
    from agnette.agl.scope.symbols import AllowRegion

__all__ = [
    "WarningOutcome",
    "WarningPolicy",
    "apply_program_policy",
    "apply_warning_policy",
    "installed_sources",
    "regions_by_source",
]


@dataclass(frozen=True, slots=True)
class WarningPolicy:
    """Warning levels: ``overrides`` by name over the catalog defaults."""

    overrides: Mapping[str, WarningLevel] = field(default_factory=dict)

    def level(self, name: str) -> WarningLevel:
        return self.overrides.get(name, WARNING_CATALOG[name].default_level)


class WarningOutcome(NamedTuple):
    """The policy's verdict: warnings to report, and denied ones as errors."""

    reported: tuple[Diagnostic, ...]
    denied: tuple[Diagnostic, ...]


def regions_by_source(modules: Iterable[ResolvedModule]) -> dict[str, list[AllowRegion]]:
    """Map the source label of each of *modules* to the ``@allow`` regions it declares.

    A module without a region still has an entry: its regions are known to be none.
    """
    by_source: dict[str, list[AllowRegion]] = {}
    for module in modules:
        label = module.source.span.source.label
        by_source.setdefault(label, []).extend(module.resolved.attributes.warning_allows)
    return by_source


def installed_sources(warnings: Iterable[Diagnostic], roots: RootSet | None) -> frozenset[str]:
    """The source labels of *warnings* anchored in installed modules.

    A file module's label is its canonical absolute path, so *roots* tells its
    origin; a synthetic label such as ``<code>`` is never installed. This reads
    only the invocation's roots, never the modules, so it also holds for a
    module a failing pass never reached. *roots* is ``None`` before a host has
    any, when no module is loaded.
    """
    if roots is None:
        return frozenset()
    labels = {w.source_label for w in warnings if w.source_label is not None}
    return frozenset(
        label
        for label in labels
        if (path := Path(label)).is_absolute() and roots.is_installed(path)
    )


def apply_program_policy(
    warnings: Iterable[Diagnostic],
    modules: Iterable[ResolvedModule],
    policy: WarningPolicy,
    *,
    scope_complete: bool,
    roots: RootSet | None,
) -> WarningOutcome:
    """Apply *policy* to the *warnings* of one program invocation.

    *modules* are every module scope resolved, also when a later module or pass
    failed. *scope_complete* is whether scope resolved the whole program; if not,
    the warnings of any other module are withheld. *roots* are the invocation's
    module roots, whose installed modules the dependency cap drops.
    """
    warnings = tuple(warnings)
    return apply_warning_policy(
        warnings,
        regions_by_source(modules),
        policy,
        scope_complete=scope_complete,
        capped_sources=installed_sources(warnings, roots),
    )


def apply_warning_policy(
    warnings: Iterable[Diagnostic],
    regions: Mapping[str, Sequence[AllowRegion]],
    policy: WarningPolicy,
    *,
    scope_complete: bool,
    capped_sources: frozenset[str] = frozenset(),
) -> WarningOutcome:
    """Apply the policy steps to the named *warnings*, keeping their order.

    *regions* maps the source label of each module whose regions are known to
    them. A warning anchored in any other source is withheld, unless
    *scope_complete* (scope resolved the whole program): then that source has no
    regions. *capped_sources* are the source labels whose warnings are dropped
    outright, whatever their level.
    """
    reported: list[Diagnostic] = []
    denied: list[Diagnostic] = []
    for warning in warnings:
        name = warning.warning_name
        if name is None:
            continue
        label = UNKNOWN_SOURCE.label if warning.source_label is None else warning.source_label
        module_regions = regions.get(label, () if scope_complete else None)
        if (
            label in capped_sources
            or module_regions is None
            or _allowed_by_region(warning, name, module_regions)
        ):
            continue
        level = policy.level(name)
        match level:
            case "allow":
                pass
            case "warn":
                reported.append(warning)
            case "deny":
                denied.append(replace(warning, severity="error"))
            case _ as unreachable:  # pragma: no cover
                assert_never(unreachable)
    return WarningOutcome(tuple(reported), tuple(denied))


def _allowed_by_region(warning: Diagnostic, name: str, regions: Iterable[AllowRegion]) -> bool:
    """Whether a region naming *name* contains the warning, from its start to its exclusive end."""
    position = (warning.line, warning.column or 1)
    return any(
        name in region.names
        and (region.span.start_line, region.span.start_col)
        <= position
        < (region.span.end_line, region.span.end_col)
        for region in regions
    )
