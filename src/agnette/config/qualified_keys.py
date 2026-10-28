"""Config table routes of AgL declarations: the tables each is addressed through."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from agnette.command_catalog import RESERVED_CONFIG_SECTION_NAMES, SCHEMA_CONFIG_SECTION_NAMES
from agnette.config.claims import ConfigSource, ConfiguredValue, Table, resolve_leaf
from agnette.config.general import GeneralConfig, PathAnchor
from agnette.core.toml import TomlDict


@dataclass(frozen=True)
class ConfigRoute:
    """The config tables one declaration is addressed through.

    ``module_segments`` is the declaring module's slash path and ``scope_path``
    names the declaration within that module. ``command_paths`` carries the CLI
    paths a package registers for it, each of which addresses it as well as its
    module route does. ``section`` instead names a fixed top-level table
    (``[exec]``): a route with no module, read from that table alone.
    """

    module_segments: tuple[str, ...] = ()
    scope_path: tuple[str, ...] = ()
    command_paths: tuple[tuple[str, ...], ...] = ()
    section: str | None = None

    @classmethod
    def in_section(cls, section: str) -> ConfigRoute:
        """Return the route of the top-level *section* table."""
        return cls(section=section)

    def exact_table_paths(self) -> tuple[tuple[str, ...], ...]:
        """Return the table paths addressing this exact route, in read order.

        A section has the one table it names; see :func:`route_table_paths` for the rest.
        """
        if self.section is not None:
            return ((self.section,),)
        return route_table_paths(self.module_segments, self.scope_path, self.command_paths)

    def table_tiers(self) -> tuple[tuple[Table, ...], ...]:
        """Return the route's tables by specificity, most specific first.

        The exact tables, then the group tables the route inherits from, one tier per
        depth, deepest first.
        """
        return (
            self.exact_table_paths(),
            *_inherited_group_paths(_config_command_paths(self.command_paths)),
        )

    def table_paths(self) -> tuple[Table, ...]:
        """Return every table of :meth:`table_tiers`, flattened."""
        return tuple(path for tier in self.table_tiers() for path in tier)

    def display_leaf(self, leaf: str) -> str:
        """Return *leaf* as lookup diagnostics spell it.

        A section's leaf reads as its config key (``exec.timeout``), any other as its
        source-style address.
        """
        if self.section is not None:
            return f"{self.section}.{leaf}"
        return "::".join(("/".join(self.module_segments), *self.scope_path, leaf))


def known_route_tables(
    routes: Iterable[ConfigRoute], command_paths: Iterable[tuple[str, ...]] = ()
) -> frozenset[tuple[str, ...]]:
    """Return the table paths that are routes: those *routes* read, and *command_paths*.

    Covers each route's exact and inherited group tables and every table that
    leads to one, since such a table holds nested routes. A table at one of
    these paths is never a leaf value (:meth:`agnette.config.claims.ConfigSource.is_route`).
    """
    paths: set[tuple[str, ...]] = set(command_paths)
    for route in routes:
        paths.update(route.table_paths())
    return frozenset(path[:depth] for path in paths for depth in range(1, len(path) + 1))


def _inherited_group_paths(
    command_paths: tuple[tuple[str, ...], ...],
) -> tuple[tuple[tuple[str, ...], ...], ...]:
    """Return every proper, non-empty prefix of *command_paths*, tiered deepest first.

    Each element is one specificity tier's table paths — the prefixes at one
    depth, deduplicated in first-seen order; a prefix that is itself one of
    *command_paths* still counts.
    """
    by_depth: dict[int, list[tuple[str, ...]]] = {}
    seen: set[tuple[str, ...]] = set()
    for command_path in command_paths:
        for depth in range(1, len(command_path)):
            prefix = command_path[:depth]
            if prefix in seen:
                continue
            seen.add(prefix)
            by_depth.setdefault(depth, []).append(prefix)
    return tuple(tuple(by_depth[depth]) for depth in sorted(by_depth, reverse=True))


def manifest_table_tiers(
    command_paths: tuple[tuple[str, ...], ...],
) -> tuple[tuple[Table, ...], ...]:
    """Return one manifest route's tiers, most specific first, ``"config"``-prefixed.

    A program's own command paths (exact), then each inherited group tier
    deepest first, then the manifest root — no module-suffix or
    quoted-anchor routes, since a manifest names commands only.
    """
    exact = tuple(("config", *segment) for segment in command_paths)
    inherited = tuple(
        tuple(("config", *segment) for segment in tier)
        for tier in _inherited_group_paths(command_paths)
    )
    return (exact, *inherited, (("config",),))


def manifest_routes(
    command_paths: Iterable[tuple[str, ...]], command_tables: frozenset[tuple[str, ...]]
) -> frozenset[Table]:
    """Return the ``"config"``-prefixed table paths of a manifest's command tables.

    The manifest's route set: *command_tables* and *command_paths* (a program's
    own), plus every table leading to one. A table at one of these paths is a
    command table, never a leaf value.
    """
    commands = known_route_tables((), (*command_tables, *command_paths))
    return frozenset(("config", *path) for path in commands)


def resolve_route_leaf(
    config: GeneralConfig,
    route: ConfigRoute,
    leaf: str,
    nested_routes: frozenset[Table] = frozenset(),
) -> ConfiguredValue | None:
    """Resolve *leaf* of *route* from the file layers of *config* alone, tier by tier.

    A table at a leaf position is a nested route when it is a table of *route* or
    in *nested_routes*, else a native value.
    """
    source = ConfigSource.from_config(config, nested_routes | known_route_tables((route,)))
    return resolve_leaf(source, route.table_tiers(), leaf, route.display_leaf(leaf))


def resolve_manifest_leaf(
    manifest_config: TomlDict,
    route: ConfigRoute,
    leaf: str,
    anchor: PathAnchor | None = None,
    command_tables: frozenset[Table] = frozenset(),
) -> ConfiguredValue | None:
    """Resolve *leaf* of *route* from a package manifest's ``[config]`` table.

    Values carry *anchor*; a table is a nested route, not a value, when it is one of
    *command_tables* or a command path of *route*.
    """
    source = ConfigSource.from_manifest(
        manifest_config, anchor, manifest_routes(route.command_paths, command_tables)
    )
    return resolve_leaf(
        source, manifest_table_tiers(route.command_paths), leaf, route.display_leaf(leaf)
    )


def param_spellings_for(
    module_segments: tuple[str, ...],
    scope_path: tuple[str, ...],
    name: str,
    *,
    is_entry: bool,
) -> tuple[str, ...]:
    """Return every unique spelling addressing a ``@param`` binding, bare name first.

    The bare name, then one dotted spelling per non-anchor
    :func:`route_table_paths` entry (module-suffix or command-path routes
    only — anchor routes contain a slash and have no config-table use here),
    shortest route first. An anonymous entry module has no module prefix, so
    its binding has the bare name and, in a non-empty scope path, the
    scope-qualified spelling alone.
    Shared by CLI/config projection (:mod:`agnette.host_params.surface`)
    and package manifest ``[config]`` leaf validation.
    """
    spellings = [name]
    if is_entry:
        spellings.append(".".join((*scope_path, name)))
    else:
        spellings.extend(
            ".".join((*path, name))
            for path in route_table_paths(module_segments, scope_path)
            if all("/" not in segment for segment in path)
        )
    unique: dict[str, None] = {}
    for spelling in spellings:
        unique[spelling] = None
    return tuple(unique)


def full_spelling_collision(declarations: Iterable[tuple[str, str]]) -> str | None:
    """Describe the first full spelling that several *declarations* share, if any.

    *declarations* pair a named-module parameter's most qualified spelling
    (``declared_spellings[-1]``) with a label naming it. Such a spelling
    addresses every sharer in a config table and on the command line, so no
    host can tell them apart; the routes of the sharers collide
    (``a/refine`` against ``a::refine``).
    """
    labels: dict[str, list[str]] = {}
    for spelling, label in declarations:
        labels.setdefault(spelling, []).append(label)
    for spelling, sharers in labels.items():
        if len(sharers) > 1:
            return f"parameters {', '.join(sharers)} share the spelling '{spelling}'"
    return None


def route_table_paths(
    module_segments: tuple[str, ...],
    scope_path: tuple[str, ...] = (),
    command_paths: tuple[tuple[str, ...], ...] = (),
) -> tuple[tuple[str, ...], ...]:
    """Return the config table paths that address one route, in read order.

    Every module-suffix spelling, shortest first, then the exact quoted module
    anchor, then each *command path* the declaration is registered under, whose
    own segments are the whole table path: ``agm dv review`` reads
    ``[dv.review]``. AgM's top-level configuration sections are excluded,
    except that a single-segment loose-file module named after a *command* may
    address one of its nested declaration tables: a command section holds its
    own settings as leaf keys, so a table one level below it is free. A
    section keyed by AgM's own schema (:data:`SCHEMA_CONFIG_SECTION_NAMES`) is
    excluded at every depth, because its keys are AgM's own or user-chosen
    names (``[warnings]`` validates every key as a warning name), so a loose
    file named ``packages.agl`` -- or a command registered as ``agent review``
    -- has no config table for its programs at all. Value resolution and leaf
    enumeration both start from this exact-route list before either adds any
    inherited group-table paths of its own.
    """
    suffix_paths = [
        (*module_segments[-depth:], *scope_path) for depth in range(1, len(module_segments) + 1)
    ]
    anchor_path = ("/".join(module_segments), *scope_path)
    paths: list[tuple[str, ...]] = []
    seen_paths: set[tuple[str, ...]] = set()
    for path in (*suffix_paths, anchor_path):
        is_reserved_root = path[0] in RESERVED_CONFIG_SECTION_NAMES
        is_nested_loose_file_route = (
            len(module_segments) == 1
            and bool(scope_path)
            and path[0] not in SCHEMA_CONFIG_SECTION_NAMES
        )
        if (not is_reserved_root or is_nested_loose_file_route) and path not in seen_paths:
            seen_paths.add(path)
            paths.append(path)
    for command_path in _config_command_paths(command_paths):
        if command_path not in seen_paths:
            seen_paths.add(command_path)
            paths.append(command_path)
    return tuple(paths)


def _config_command_paths(
    command_paths: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], ...]:
    """*command_paths* that address config-file tables: none rooted at a schema section.

    Package validation rejects such a registration, but the activation index
    tolerates one from an earlier install (e.g. ``agent review``). Like a loose
    file named after a schema section, it has no config table, exact or
    inherited, so its tables never collide with AgM's own schema.
    """
    return tuple(path for path in command_paths if path[0] not in SCHEMA_CONFIG_SECTION_NAMES)
