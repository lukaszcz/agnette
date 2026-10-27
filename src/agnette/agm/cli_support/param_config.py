"""Configuration routes shared by host-configurable module parameters."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from agnette.agl.ir.static_keys import StaticBindingKey
from agnette.agl.modules.ids import ModuleId
from agnette.agl.runtime.types import HostParamInfo, HostParamKind, ProgramDeclInfo
from agnette.agl.zones import ParamZone
from agnette.agm.cli_support.param_surface import ParamSurface, ParamSurfaceEntry
from agnette.agm.cli_support.program_options import native_raw_value, project_option
from agnette.config.general import GeneralConfig, undeclared_exec_keys
from agnette.config.qualified_keys import (
    QualifiedConfigKey,
    QualifiedConfigLookupError,
    configured_leaf_table_candidates,
    configured_leaf_tables,
    display_table_path,
    manifest_leaf_tables,
    resolve_manifest_values,
    resolve_qualified_values,
)
from agnette.core.toml import TomlDict

__all__ = [
    "ParamValueTiers",
    "report_undeclared_exec_keys",
    "resolve_module_param_values",
    "resolve_param_values",
]


#: A config table route: declaring module, scope, and registered command paths.
_RouteKey = tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, ...], ...]]


@dataclass(frozen=True)
class ParamValueTiers:
    """Supplied/program-route values (``upper``) above module-route values (``lower``).

    ``lower`` holds only keys absent from ``upper``, and within it a
    package manifest ``[config]`` value (when the selected program is
    package-owned) already outranks the plain module-route value for the
    same key. The full chain also ranks a selected program's own ``@config``
    entries between the two — ``PipelineDriver.preflight_arguments`` merges
    all three; there is no flattened two-tier view here.

    Every host parameter kind is keyed alike, engine settings included
    (their module-route tier is ``[exec]``, which the host reads itself).
    ``supplied`` names the ``upper`` keys the command line or environment
    supplied, as opposed to the program route.
    """

    upper: Mapping[StaticBindingKey, object]
    lower: Mapping[StaticBindingKey, object]
    supplied: frozenset[StaticBindingKey] = frozenset()


class _RouteReport(NamedTuple):
    """One config table route and the leaves it may supply."""

    module_segments: tuple[str, ...]
    scope_path: tuple[str, ...]
    command_paths: tuple[tuple[str, ...], ...]
    declared_leaves: frozenset[str]
    positional_only: frozenset[str]


def _tables_with_registered_descendants(
    command_paths: tuple[tuple[str, ...], ...],
    package_command_paths: frozenset[tuple[str, ...]],
) -> frozenset[tuple[str, ...]]:
    """Return which of *command_paths* is a proper prefix of another registered command.

    Such a table's own leaves may be inherited defaults meant for that
    descendant command (see the group-table inheritance in
    :func:`agnette.config.qualified_keys.resolve_qualified_values`), so a warning
    on this command must leave them alone — a sibling command may be the one
    that actually consumes them.
    """
    return frozenset(
        path
        for path in command_paths
        if any(
            len(other) > len(path) and other[: len(path)] == path for other in package_command_paths
        )
    )


def _warn_ignored_config_key(leaf: str, table_name: str, reason: str) -> None:
    """Print the warning that config key *leaf* in table *table_name* is ignored."""
    print(
        f"warning: config key '{leaf}' in the '{table_name}' configuration table "
        f"{reason} and will be ignored",
        file=sys.stderr,
    )


def report_undeclared_exec_keys(config: GeneralConfig) -> None:
    """Warn for each ``[exec]`` key that is no engine setting or execution option."""
    for key in undeclared_exec_keys(config.merged):
        _warn_ignored_config_key(key, "exec", "is not an engine setting or execution option")


def _report_undeclared_config_keys(
    config: GeneralConfig,
    routes: Sequence[_RouteReport],
    package_command_paths: frozenset[tuple[str, ...]],
) -> None:
    """Warn for configured route leaves that no host input consumes.

    Each route carries its own declaration set. Registered command tables are
    validated only against their program, never against a colliding module route.
    """
    command_tables = package_command_paths | frozenset(
        path for route in routes for path in route.command_paths
    )
    for route in routes:
        exempt_tables = (command_tables - frozenset(route.command_paths)) | (
            _tables_with_registered_descendants(route.command_paths, package_command_paths)
        )
        leaf_tables = configured_leaf_tables(
            config,
            route.module_segments,
            route.scope_path,
            route.command_paths,
            excluded_tables=exempt_tables,
        )
        for leaf in sorted(leaf_tables):
            table_path = leaf_tables[leaf]
            table_name = display_table_path(table_path)
            if leaf in route.positional_only:
                _warn_ignored_config_key(
                    leaf,
                    table_name,
                    "names a positional-only program argument, which can only be supplied "
                    "positionally,",
                )
                continue
            if leaf in route.declared_leaves:
                continue
            _warn_ignored_config_key(leaf, table_name, "is not a declared program argument")


def _report_unused_manifest_leaves(
    package_config: TomlDict,
    command_paths: tuple[tuple[str, ...], ...],
    consumed: frozenset[str],
    package_command_paths: frozenset[tuple[str, ...]],
) -> None:
    """Warn for manifest leaves in the selected command's own tables that nothing consumes.

    *consumed* holds every spelling that addresses a declared parameter, an engine
    setting included: the spellings reached parameters won on the surface, and
    all spellings of the unreached ones. Inherited group and root
    tables are skipped, since a sibling command may be the one that consumes
    them, as are tables a registered descendant command inherits from.
    """
    exempt_tables = _tables_with_registered_descendants(command_paths, package_command_paths)
    own_tables = {("config", *path) for path in command_paths} - {
        ("config", *path) for path in exempt_tables
    }
    leaf_tables = manifest_leaf_tables(package_config, command_paths)
    for leaf in sorted(leaf_tables):
        table_path = leaf_tables[leaf]
        if table_path not in own_tables or leaf in consumed:
            continue
        print(
            f"warning: manifest config key '{leaf}' in the '{display_table_path(table_path)}' "
            "table is not an engine setting or a module parameter of the program "
            "and will be ignored",
            file=sys.stderr,
        )


def resolve_param_values(
    config: GeneralConfig,
    program: ProgramDeclInfo,
    params: Mapping[StaticBindingKey, object],
    *,
    entry_segments: tuple[str, ...],
    command_paths: tuple[tuple[str, ...], ...],
    surface: ParamSurface,
    package_command_paths: frozenset[tuple[str, ...]] = frozenset(),
    package_config: TomlDict | None = None,
    declared_params: Sequence[HostParamInfo] | None = None,
) -> ParamValueTiers:
    """Resolve parameter config values beneath parsed CLI/environment values.

    Module routes are addressed by each binding's declaration module and
    scope. A selected program's qualified table overrides a parameter through
    any spelling it wins on *surface*: its bare name, or a qualified spelling
    when a nearer declaration claims that name. The two routes are resolved
    independently, so a program route wins even when its value comes from a
    less-specific config layer than the module route. The supplied and
    program-route values form :attr:`ParamValueTiers.upper`; the module-route
    values not already covered by that tier form :attr:`ParamValueTiers.lower`.

    *package_config* is the owning package's manifest ``[config]`` table
    (``None``/empty when the program is not package-owned). Its values
    address the same program-route spellings and are merged into ``lower``,
    above the plain module-route value, for every key not already resolved
    in ``upper`` — never overriding an ``upper`` entry.

    Configured leaves on those routes that no host input consumes are reported
    as warnings here, where the routes are known. *package_command_paths* is
    every command path the owning package registers (aliases expanded), used
    to exempt a command's own table from that warning when one of its leaves
    is really an inherited default for a registered descendant command.

    Engine settings are surface entries too, so their program-route and
    manifest values merge into the tiers like any parameter's; they have no
    declaration-module route, so they take no part in module-route resolution.

    Only the *surface* parameters (those the program reaches) take values.
    *declared_params* are the parameters the program's closure declares, reached
    or not; a configured key addressing one of them is not undeclared, so it
    draws no warning. ``None`` declares just the surface's.
    """
    supplied = frozenset(params)
    entries = surface.entries
    declared = _module_declared(
        (entry.param for entry in entries) if declared_params is None else declared_params
    )
    reached = frozenset(entry.param.key for entry in entries)
    module_params = _module_declared(entry.param for entry in entries)
    unreached_leaves = _declared_leaves(param for param in declared if param.key not in reached)
    module_routes = _module_routes(module_params)
    program_routes = _program_routes(program, entry_segments, command_paths, entries)
    _reject_configured_cross_route_ambiguities(config, module_routes, program_routes)
    module_values = resolve_module_param_values(config, module_params)

    _reject_configured_ambiguous_program_leaves(
        config, program, entry_segments, command_paths, surface, package_config
    )
    program_values = resolve_qualified_values(config, tuple(key for _entry, key in program_routes))
    upper = dict(params)
    _merge_route_values(upper, supplied, program_routes, program_values)
    lower = {key: value for key, value in module_values.items() if key not in upper}
    if package_config:
        manifest_values = resolve_manifest_values(
            package_config, tuple(key for _entry, key in program_routes)
        )
        _merge_route_values(lower, frozenset(upper), program_routes, manifest_values)
        _report_unused_manifest_leaves(
            package_config,
            command_paths,
            frozenset(
                spelling for _entry, key in program_routes for spelling in key.leaf_spellings()
            )
            | unreached_leaves,
            package_command_paths,
        )

    _report_undeclared_config_keys(
        config,
        _route_reports(
            program, entry_segments, command_paths, declared, unreached_leaves, program_routes
        ),
        package_command_paths,
    )
    return ParamValueTiers(upper=upper, lower=lower, supplied=supplied)


def _module_declared(params: Iterable[HostParamInfo]) -> tuple[HostParamInfo, ...]:
    """Return the *params* a declaration module routes: all but the engine settings."""
    return tuple(param for param in params if param.kind is not HostParamKind.ENGINE)


def _reject_configured_cross_route_ambiguities(
    config: GeneralConfig,
    module_routes: Sequence[tuple[HostParamInfo, QualifiedConfigKey]],
    program_routes: Sequence[tuple[ParamSurfaceEntry, QualifiedConfigKey]],
) -> None:
    """Reject a configured table leaf that resolves to distinct route kinds.

    The program side includes tables inherited from a command path's group
    prefixes, since a leaf set there is just as ambiguous against a module
    route as one set on the exact route.
    """
    if not program_routes:
        return
    # Every program route addresses the same table route, so its leaves are
    # read once; module routes repeat per declaration module and scope.
    _entry, shared_key = program_routes[0]
    program_leaves = configured_leaf_table_candidates(
        config,
        shared_key.module_segments,
        shared_key.scope_path,
        shared_key.command_paths,
    )
    module_leaves_by_route: dict[_RouteKey, dict[str, tuple[str, ...]]] = {}
    for module_param, module_key in module_routes:
        route: _RouteKey = (
            module_key.module_segments,
            module_key.scope_path,
            module_key.command_paths,
        )
        if route not in module_leaves_by_route:
            module_leaves_by_route[route] = configured_leaf_tables(
                config, module_key.module_segments, module_key.scope_path, module_key.command_paths
            )
        module_leaves = module_leaves_by_route[route]
        module_table = module_leaves.get(module_key.leaf)
        if module_table is None:
            continue
        peers = tuple(
            entry.param
            for entry, program_key in program_routes
            if module_param.key != entry.param.key
            and module_key.leaf == program_key.leaf
            and module_table in program_leaves.get(program_key.leaf, frozenset())
        )
        if peers:
            _reject_configured_ambiguity(module_leaves, {module_key.leaf: (module_param, *peers)})


def _reject_configured_ambiguity(
    leaf_tables: Mapping[str, tuple[str, ...]],
    candidates: Mapping[str, Sequence[HostParamInfo]],
) -> None:
    """Reject the first *candidates* leaf that one of *leaf_tables*' tables sets."""
    for leaf, claimants in candidates.items():
        table = leaf_tables.get(leaf)
        if table is None:
            continue
        names = ", ".join(claimant.declaration_path for claimant in claimants)
        raise QualifiedConfigLookupError(
            f"config key {display_table_path(table)}.{leaf} matches multiple parameters: {names}"
        )


def resolve_module_param_values(
    config: GeneralConfig, params: Sequence[HostParamInfo]
) -> dict[StaticBindingKey, object]:
    """Resolve parameter values from their declaration-module routes only.

    This is the config surface used by incremental hosts, which initialize
    modules without selecting a program and therefore have no program route.
    """
    module_routes = _module_routes(params)
    _reject_configured_ambiguous_module_leaves(config, params)
    configured = resolve_qualified_values(config, tuple(key for _param, key in module_routes))
    values: dict[StaticBindingKey, object] = {}
    for param, key in module_routes:
        if key not in configured:
            continue
        projected = project_option(param.cli.name, param.type)
        values[param.key] = native_raw_value(projected, configured[key])
    return values


def _module_routes(
    params: Sequence[HostParamInfo],
) -> tuple[tuple[HostParamInfo, QualifiedConfigKey], ...]:
    """Return declaration-module routes for every non-anonymous binding."""
    return tuple(
        (
            param,
            QualifiedConfigKey(
                param.module.segments,
                param.route_path,
                param.cli.name,
            ),
        )
        for param in params
        if not param.module.is_entry
    )


def _reject_configured_ambiguous_module_leaves(
    config: GeneralConfig, params: Sequence[HostParamInfo]
) -> None:
    """Reject a configured leaf claimed by multiple bindings on one module route."""
    candidates_by_route: dict[
        tuple[tuple[str, ...], tuple[str, ...], str], list[HostParamInfo]
    ] = {}
    for param in params:
        if param.module.is_entry:
            continue
        route = (param.module.segments, param.route_path, param.cli.name)
        candidates_by_route.setdefault(route, []).append(param)
    for (module_segments, scope_path, leaf), candidates in candidates_by_route.items():
        if len(candidates) < 2:
            continue
        _reject_configured_ambiguity(
            configured_leaf_tables(config, module_segments, scope_path), {leaf: candidates}
        )


def _program_routes(
    program: ProgramDeclInfo,
    entry_segments: tuple[str, ...],
    command_paths: tuple[tuple[str, ...], ...],
    entries: Sequence[ParamSurfaceEntry],
) -> tuple[tuple[ParamSurfaceEntry, QualifiedConfigKey], ...]:
    """Return selected-program routes for resolving module-parameter spellings.

    Every spelling a parameter wins on the CLI surface addresses it in the
    selected program's table too, so a parameter whose bare name is claimed by
    a nearer declaration stays configurable per program through a qualified
    spelling. Engine settings route like module parameters. :class:`ParamSurface`
    is the sole authority on which spellings those are.
    """
    if not entry_segments:
        return ()
    program_path = (*program.scope_path, program.name)
    routes: list[tuple[ParamSurfaceEntry, QualifiedConfigKey]] = []
    for entry in entries:
        if not entry.spellings:
            continue
        leaf, *aliases = entry.spellings
        routes.append(
            (
                entry,
                QualifiedConfigKey(
                    entry_segments, program_path, leaf, command_paths, tuple(aliases)
                ),
            )
        )
    return tuple(routes)


def _merge_route_values(
    values: dict[StaticBindingKey, object],
    supplied: frozenset[StaticBindingKey],
    routes: Iterable[tuple[ParamSurfaceEntry, QualifiedConfigKey]],
    configured: Mapping[QualifiedConfigKey, object],
) -> None:
    """Project configured native values into *values*, skipping keys already in *supplied*.

    Shared by the program-route merge into ``upper`` (*supplied* = CLI/
    environment keys) and the manifest merge into ``lower`` (*supplied* =
    every key already resolved in ``upper``, so a manifest value never
    overrides one): both are "does a higher tier already own this key".
    """
    for entry, key in routes:
        if entry.param.key in supplied or key not in configured:
            continue
        projected = project_option(entry.param.cli.name, entry.param.type)
        values[entry.param.key] = native_raw_value(projected, configured[key])


def _reject_configured_ambiguous_program_leaves(
    config: GeneralConfig,
    program: ProgramDeclInfo,
    entry_segments: tuple[str, ...],
    command_paths: tuple[tuple[str, ...], ...],
    surface: ParamSurface,
    package_config: TomlDict | None = None,
) -> None:
    """Reject a configured program-table leaf claimed by peer module parameters.

    Every spelling a program table may use is checked, bare or qualified: a
    spelling several parameters claim resolves to none of them, so *using* it
    names the candidates rather than silently picking one. *package_config*
    extends the same check over the owning package's manifest command, group,
    and root tables. Config group tables are covered too, since an inherited
    leaf is just as ambiguous as one set on the exact route.
    """
    if not entry_segments:
        return
    program_path = (*program.scope_path, program.name)
    candidates = configured_leaf_table_candidates(
        config, entry_segments, program_path, command_paths
    )
    configured = {leaf: min(tables) for leaf, tables in candidates.items()}
    if package_config:
        configured = {**manifest_leaf_tables(package_config, command_paths), **configured}
    _reject_configured_ambiguity(configured, surface.ambiguous)


def _route_reports(
    program: ProgramDeclInfo,
    entry_segments: tuple[str, ...],
    command_paths: tuple[tuple[str, ...], ...],
    declared_params: Sequence[HostParamInfo],
    unreached_leaves: frozenset[str],
    program_routes: Sequence[tuple[ParamSurfaceEntry, QualifiedConfigKey]],
) -> list[_RouteReport]:
    """Describe module and selected-program config routes for warning reporting.

    The program route declares the spellings reached parameters won plus
    *unreached_leaves*, never a spelling a reached parameter lost.
    """
    by_module_scope: dict[tuple[ModuleId, tuple[str, ...]], list[HostParamInfo]] = {}
    for param in declared_params:
        if not param.module.is_entry:
            by_module_scope.setdefault((param.module, param.route_path), []).append(param)

    reports: list[_RouteReport] = []
    ordered_modules = _report_modules(program, declared_params)
    for module in ordered_modules:
        if module.is_entry:
            continue
        root_params = by_module_scope.get((module, ()), [])
        reports.append(
            _RouteReport(
                module.segments,
                (),
                (),
                frozenset(param.cli.name for param in root_params),
                frozenset(),
            )
        )
        scope_paths = tuple(
            scope_path
            for candidate, scope_path in by_module_scope
            if candidate == module and scope_path
        )
        for scope_path in scope_paths:
            declared = frozenset(param.cli.name for param in by_module_scope[(module, scope_path)])
            reports.append(_RouteReport(module.segments, scope_path, (), declared, frozenset()))

    if entry_segments:
        declared = frozenset(
            {
                *(parameter.cli.name for parameter in program.parameters),
                *(spelling for _entry, key in program_routes for spelling in key.leaf_spellings()),
                *unreached_leaves,
            }
        )
        positional_only = frozenset(
            parameter.cli.name
            for parameter in program.parameters
            if parameter.kind is ParamZone.POSITIONAL_ONLY
        )
        reports.append(
            _RouteReport(
                entry_segments,
                (*program.scope_path, program.name),
                command_paths,
                declared,
                positional_only,
            )
        )
    return reports


def _declared_leaves(declared: Iterable[HostParamInfo]) -> frozenset[str]:
    """Return every spelling that can address one of *declared*, claimed or not."""
    return frozenset(spelling for param in declared for spelling in param.declared_spellings)


def _report_modules(
    program: ProgramDeclInfo, declared: Sequence[HostParamInfo]
) -> tuple[ModuleId, ...]:
    """Return closure modules first, retaining any other declaring module afterward."""
    modules: list[ModuleId] = list(program.closure)
    for param in declared:
        if param.module not in modules:
            modules.append(param.module)
    return tuple(modules)
