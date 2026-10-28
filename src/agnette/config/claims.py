"""The claims index: which parameters address each configured leaf.

One index maps ``table -> leaf -> claims`` for every route that may read a
config table. Three results derive from it by scanning a :class:`ConfigSource`
for the leaves it actually sets:

* values: each claimant's unique leaf per specificity tier, layered across the source;
* ambiguity errors: a set leaf several distinct parameters claim, whatever the tiers;
* undeclared-key warnings: a set leaf no claim consumes (:meth:`ClaimsIndex.consumed`).

Claims live in one of two domains, config files or a package manifest's ``[config]``,
matching :attr:`ConfigSource.manifest`.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

from agnette.config.general import GeneralConfig, PathAnchor
from agnette.core.toml import TomlDict

__all__ = [
    "Claimant",
    "ClaimsIndex",
    "ConfigSource",
    "ConfiguredValue",
    "QualifiedConfigLookupError",
    "Table",
    "display_table_path",
    "resolve_leaf",
]

Table = tuple[str, ...]

_MISSING = object()


class ConfiguredValue(NamedTuple):
    """A configured value with the :class:`PathAnchor` of the layer that supplied it."""

    value: object
    anchor: PathAnchor | None


class QualifiedConfigLookupError(ValueError):
    """Raised when a qualified configuration table cannot be resolved uniquely."""


@dataclass(frozen=True)
class Claimant:
    """A parameter's claim on leaves through one route, compared by value.

    ``param`` identifies the parameter across its claimants, so claiming through
    several routes counts once; ``route`` tells those claimants apart. ``label``
    names the claimant in diagnostics only.
    """

    param: Hashable
    route: Hashable
    label: str = field(compare=False)


class Claim(NamedTuple):
    """One claimant's claim on a leaf of a table.

    ``claimant`` is ``None`` for a claim that only declares the leaf, feeding no
    value and never making it ambiguous. ``tier`` ranks the table's specificity
    for the claimant (0: its exact route). ``foreign`` marks an exact table that
    is another command's own, where a declare-only claim declares nothing; a
    claim that feeds a value still consumes its leaf.
    """

    claimant: Claimant | None
    tier: int
    foreign: bool


def _table_at(config: TomlDict, path: Table) -> dict[str, object] | None:
    """Return the table at *path* in *config*, or ``None`` when absent or not a table."""
    current: object = config
    for segment in path:
        if not isinstance(current, dict):
            return None
        current = current.get(segment, _MISSING)
        if current is _MISSING:
            return None
    return current if isinstance(current, dict) else None


def _table_path_replaced(config: TomlDict, path: Table) -> bool:
    """Return whether *config* explicitly makes *path* non-tabular."""
    current: object = config
    for segment in path:
        if not isinstance(current, dict) or segment not in current:
            return False
        current = current[segment]
        if not isinstance(current, dict):
            return True
    return False


def display_table_path(path: Table) -> str:
    """Render *path* as the dot-joined TOML table spelling a user would write.

    A path whose first segment is a slash-joined module route (the anchor
    spelling from :func:`agnette.config.qualified_keys.route_table_paths`) is
    rendered as a single quoted key (``["a/b"].c``) since a bare ``a/b.c`` is
    not valid TOML; every other path is plain dot-joined (``a.b``).
    """
    first, *rest = path
    if "/" in first:
        return ".".join((f'["{first}"]', *rest))
    return ".".join(path)


@dataclass(frozen=True)
class ConfigSource:
    """The config layers a claims index is read against, lowest first.

    ``anchors`` run parallel to ``layers``. A table at a leaf position is a nested
    route, not a value, when its path is in ``nested_routes``. ``manifest`` selects
    the index's manifest claims: a manifest source holds the package ``[config]``
    table under ``config``, so its table paths are ``config``-prefixed.
    """

    layers: tuple[TomlDict, ...]
    anchors: tuple[PathAnchor | None, ...]
    nested_routes: frozenset[Table]
    manifest: bool = False

    @classmethod
    def from_config(cls, config: GeneralConfig, nested_routes: frozenset[Table]) -> ConfigSource:
        """Return the normalized layers of *config*."""
        return cls(config.layers, config.anchors, nested_routes)

    @classmethod
    def from_manifest(
        cls,
        manifest_config: TomlDict,
        anchor: PathAnchor | None,
        nested_routes: frozenset[Table],
    ) -> ConfigSource:
        """Return a package manifest's ``[config]`` table as a single layer."""
        return cls(({"config": manifest_config},), (anchor,), nested_routes, manifest=True)

    def set_leaves(self, tables: Iterable[Table]) -> Iterator[tuple[str, Table]]:
        """Yield ``(leaf, table)`` for each key of *tables* in the layers that is no route."""
        paths = tuple(tables)
        for layer in self.layers:
            for path in paths:
                contents = _table_at(layer, path)
                if contents is None:
                    continue
                for name, value in contents.items():
                    if not self.is_route(value, path, name):
                        yield name, path

    def first_tables(self, tables: Iterable[Table]) -> dict[str, Table]:
        """Return each leaf *tables* set, mapped to the first table a layer scan meets it in."""
        found: dict[str, Table] = {}
        for leaf, table in self.set_leaves(tables):
            found.setdefault(leaf, table)
        return found

    def is_set(self, table: Table, leaf: str) -> bool:
        """Return whether any layer sets *leaf* as a value of *table*."""
        return any(name == leaf for name, _ in self.set_leaves((table,)))

    def is_route(self, value: object, path: Table, leaf: str) -> bool:
        """Return whether *value* at *leaf* of *path* is a nested route table, not a leaf value."""
        return isinstance(value, dict) and (*path, leaf) in self.nested_routes


class ClaimsIndex:
    """``table -> leaf -> [Claim]`` over every claimed route of one resolution."""

    def __init__(self) -> None:
        self._files: dict[Table, dict[str, list[Claim]]] = {}
        self._manifest: dict[Table, dict[str, list[Claim]]] = {}

    def claim(
        self,
        claimant: Claimant | None,
        tiers: Sequence[Iterable[Table]],
        leaves: Iterable[str],
        *,
        foreign: frozenset[Table] = frozenset(),
        manifest: bool = False,
    ) -> None:
        """Record *claimant*'s claim on *leaves* of each table, tiered most specific first.

        A ``None`` *claimant* only declares the leaves.

        *foreign* are tables owned by another command; they are flagged where
        they are the claimant's exact (tier 0) tables. *manifest* selects the domain.
        """
        tables = self._tables(manifest)
        spellings = tuple(leaves)
        for tier, paths in enumerate(tiers):
            for path in paths:
                by_leaf = tables.setdefault(path, {})
                for leaf in spellings:
                    by_leaf.setdefault(leaf, []).append(
                        Claim(claimant, tier, tier == 0 and path in foreign)
                    )

    def consumed(self, source: ConfigSource, table: Table, leaf: str) -> bool:
        """Return whether some claim consumes *leaf* of *table* (undeclared-key warnings)."""
        claims = self._tables(source.manifest).get(table, {}).get(leaf, ())
        return any(claim.claimant is not None or not claim.foreign for claim in claims)

    def reject_ambiguities(self, source: ConfigSource) -> None:
        """Reject a leaf *source* sets that several distinct parameters claim.

        Whatever the tiers of the claims: an inherited table's leaf is as ambiguous as an exact one.
        """
        tables = self._tables(source.manifest)
        for table in sorted(tables):
            for leaf, claims in tables[table].items():
                claimants = [c.claimant for c in claims if c.claimant is not None]
                if len({c.param for c in claimants}) < 2 or not source.is_set(table, leaf):
                    continue
                labels = list({c.label: None for c in claimants})
                raise QualifiedConfigLookupError(
                    f"config key {display_table_path(table)}.{leaf} matches multiple parameters: "
                    f"{', '.join(labels)}"
                )

    def resolve(self, source: ConfigSource) -> dict[Claimant, ConfiguredValue]:
        """Resolve the value of each claimant, tier by tier.

        A claimant's tiers are resolved most specific first, each independently
        across all layers, even one a higher tier already resolved, so a conflict
        in a shadowed tier still errors; a higher tier's value wins whichever
        layer supplied it. Within a tier a later layer overrides an earlier one,
        several spellings in one layer conflict, and a layer that makes a
        claimant's table non-tabular drops the earlier value.
        """
        reads: dict[int, dict[Claimant, list[Table]]] = {}
        for table, by_leaf in self._tables(source.manifest).items():
            for claims in by_leaf.values():
                for claim in claims:
                    if claim.claimant is None:
                        continue
                    tables = reads.setdefault(claim.tier, {}).setdefault(claim.claimant, [])
                    if table not in tables:
                        tables.append(table)
        resolved: dict[Claimant, ConfiguredValue] = {}
        for tier in sorted(reads):
            for claimant, value in self._resolve_tier(source, tier, reads[tier]).items():
                resolved.setdefault(claimant, value)
        return resolved

    def _resolve_tier(
        self, source: ConfigSource, tier: int, reads: dict[Claimant, list[Table]]
    ) -> dict[Claimant, ConfiguredValue]:
        """Resolve the claimants of *reads* (their tables at *tier*) across every layer."""
        tables = self._tables(source.manifest)
        resolved: dict[Claimant, ConfiguredValue] = {}
        for layer, anchor in zip(source.layers, source.anchors, strict=True):
            for claimant, paths in reads.items():
                if any(_table_path_replaced(layer, path) for path in paths):
                    resolved.pop(claimant, None)
            hits: dict[Claimant, list[tuple[Table, str, object]]] = {}
            for table, by_leaf in tables.items():
                contents = _table_at(layer, table)
                if contents is None:
                    continue
                for leaf, claims in by_leaf.items():
                    if leaf not in contents or source.is_route(contents[leaf], table, leaf):
                        continue
                    for claim in claims:
                        if claim.tier == tier and claim.claimant in reads:
                            hits.setdefault(claim.claimant, []).append(
                                (table, leaf, contents[leaf])
                            )
            for claimant, found in hits.items():
                if len(found) > 1:
                    spellings = ", ".join(
                        f"{display_table_path(path)}.{leaf}" for path, leaf, _ in found
                    )
                    raise QualifiedConfigLookupError(
                        f"config key {claimant.label} is set by conflicting tables: {spellings}"
                    )
                resolved[claimant] = ConfiguredValue(found[0][2], anchor)
        return resolved

    def _tables(self, manifest: bool) -> dict[Table, dict[str, list[Claim]]]:
        return self._manifest if manifest else self._files


def resolve_leaf(
    source: ConfigSource, tiers: Sequence[Iterable[Table]], leaf: str, label: str
) -> ConfiguredValue | None:
    """Resolve *leaf* over one route's *tiers* against *source*, in its file or manifest domain.

    *label* names the leaf in a conflict error.
    """
    index = ClaimsIndex()
    claimant = Claimant(leaf, source.manifest, label)
    index.claim(claimant, tiers, (leaf,), manifest=source.manifest)
    return index.resolve(source).get(claimant)
