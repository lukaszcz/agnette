"""The per-invocation policy that decides which static warnings reach the user.

The static passes report every warning they find, and caches keep those raw
warnings. A host then applies a :class:`WarningPolicy` to one invocation's
warnings with :func:`apply_warning_policy`, in order:

1. drop warnings anchored in capped sources (installed dependencies); a host
   passes the capped set once it knows each module's origin, and until then
   the set is empty and the step drops nothing;
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
from typing import TYPE_CHECKING, NamedTuple, assert_never

from agnette.agl.diagnostics import Diagnostic
from agnette.agl.syntax.spans import UNKNOWN_SOURCE
from agnette.agl.warnings import WARNING_DEFAULT_LEVELS, WarningLevel

if TYPE_CHECKING:
    from agnette.agl.scope.program import ResolvedModule
    from agnette.agl.scope.symbols import AllowRegion

__all__ = [
    "NamedWarning",
    "WarningOutcome",
    "WarningPolicy",
    "apply_program_policy",
    "apply_warning_policy",
    "named_warnings",
    "regions_by_source",
]


@dataclass(frozen=True, slots=True)
class WarningPolicy:
    """Warning levels: ``overrides`` by name over the catalog defaults."""

    overrides: Mapping[str, WarningLevel] = field(default_factory=dict)

    def level(self, name: str) -> WarningLevel:
        return self.overrides.get(name, WARNING_DEFAULT_LEVELS[name])


class NamedWarning(NamedTuple):
    """A static warning with its catalog name."""

    name: str
    diagnostic: Diagnostic


def named_warnings(diagnostics: Iterable[Diagnostic]) -> list[NamedWarning]:
    """Pair each warning among *diagnostics* with its catalog name.

    Every static warning is built by :func:`~agnette.agl.diagnostics.warning_diagnostic`,
    so none is unnamed.
    """
    return [
        NamedWarning(diagnostic.warning_name, diagnostic)
        for diagnostic in diagnostics
        if diagnostic.warning_name is not None
    ]


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


def apply_program_policy(
    warnings: Iterable[Diagnostic],
    modules: Iterable[ResolvedModule],
    policy: WarningPolicy,
    *,
    scope_complete: bool,
    capped_sources: frozenset[str] = frozenset(),
) -> WarningOutcome:
    """Apply *policy* to the *warnings* of one program invocation.

    *modules* are every module scope resolved, also when a later module or pass
    failed. *scope_complete* is whether scope resolved the whole program; if not,
    the warnings of any other module are withheld. *capped_sources* are the labels
    of the sources whose warnings the dependency cap drops.
    """
    return apply_warning_policy(
        named_warnings(warnings),
        regions_by_source(modules),
        policy,
        scope_complete=scope_complete,
        capped_sources=capped_sources,
    )


def apply_warning_policy(
    warnings: Iterable[NamedWarning],
    regions: Mapping[str, Sequence[AllowRegion]],
    policy: WarningPolicy,
    *,
    scope_complete: bool,
    capped_sources: frozenset[str] = frozenset(),
) -> WarningOutcome:
    """Apply the policy steps to *warnings*, keeping their order.

    *regions* maps the source label of each module whose regions are known to
    them. A warning anchored in any other source is withheld, unless
    *scope_complete* (scope resolved the whole program): then that source has no
    regions. *capped_sources* are the source labels whose warnings are dropped
    outright, whatever their level.
    """
    reported: list[Diagnostic] = []
    denied: list[Diagnostic] = []
    for name, warning in warnings:
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
