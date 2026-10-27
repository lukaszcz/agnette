"""Configuration routes shared by host-configurable parameters."""

from __future__ import annotations

import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple, NoReturn

from agnette.agl.ir.static_keys import (
    FunctionParamKey,
    HostParamKey,
    host_param_label,
)
from agnette.agl.modules.ids import ModuleId
from agnette.agl.runtime.arguments import InheritedParamValue
from agnette.agl.runtime.types import (
    HostParamInfo,
    HostParamKind,
    ProgramDeclInfo,
    ProgramParamInfo,
)
from agnette.agl.zones import NAME_ADDRESSABLE_ZONES, ParamZone
from agnette.config.general import (
    EXEC_CONFIG_SECTION,
    GeneralConfig,
    PathAnchor,
    anchor_path_value,
    undeclared_exec_keys,
)
from agnette.config.qualified_keys import (
    ConfigRoute,
    ConfiguredValue,
    QualifiedConfigKey,
    QualifiedConfigLookupError,
    configured_leaf_table_candidates,
    configured_leaf_tables,
    display_table_path,
    full_spelling_collision,
    known_route_tables,
    manifest_leaf_tables,
    resolve_manifest_values,
    resolve_qualified_values,
)
from agnette.core.toml import TomlDict
from agnette.host_params.inherit import (
    InheritRequest,
    InheritScope,
    resolve_inherited_values,
)
from agnette.host_params.projection import (
    is_optional_default,
    native_raw_value,
    project_option,
)
from agnette.host_params.surface import ParamSurface, ParamSurfaceEntry

__all__ = [
    "ParamValueTiers",
    "colliding_declarations",
    "declaration_route_tables",
    "report_colliding_declarations",
    "report_undeclared_exec_keys",
    "resolve_declaration_route_values",
    "resolve_param_values",
]


@dataclass(frozen=True)
class ParamValueTiers:
    """Supplied/program-route values (``upper``) above declaration-route values (``lower``).

    ``lower`` holds only keys absent from ``upper``, and within it a
    package manifest ``[config]`` value (when the selected program is
    package-owned) already outranks the plain declaration-route value for the
    same key. The full chain also ranks a selected program's own ``@config``
    entries between the two — ``PipelineDriver.preflight_arguments`` merges
    all three; there is no flattened two-tier view here. ``inherited`` holds
    the ``@inherit`` values, ranked between ``@config`` and ``upper``; it
    never names a key ``upper`` or ``supplied`` holds.

    Every host parameter kind is keyed alike by :data:`HostParamKey`: module
    parameters and engine settings by binding key (an engine setting's
    declaration route is the ``[exec]`` table), function parameters and the
    program's own arguments by :class:`FunctionParamKey` (an argument has no
    declaration route beyond its program route).
    ``supplied`` names the keys the command line or environment supplied, as
    opposed to the program route; a program argument bound positionally is
    supplied but holds no value in either tier.
    """

    upper: Mapping[HostParamKey, object]
    lower: Mapping[HostParamKey, object]
    supplied: frozenset[HostParamKey] = frozenset()
    inherited: Mapping[HostParamKey, InheritedParamValue] = field(default_factory=dict)


class _RouteReport(NamedTuple):
    """One config table route and the leaves it may supply."""

    route: ConfigRoute
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


_NOT_A_PARAMETER = "is not a parameter of the program"
_UNDECLARED = "is not a declared parameter of its route"
_POSITIONAL_ONLY = (
    "names a positional-only program argument, which can only be supplied positionally"
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
        _warn_ignored_config_key(
            key, EXEC_CONFIG_SECTION, "is not an engine setting or execution option"
        )


def _report_undeclared_config_keys(
    config: GeneralConfig,
    reports: Sequence[_RouteReport],
    package_command_paths: frozenset[tuple[str, ...]],
    nested_routes: frozenset[tuple[str, ...]],
) -> None:
    """Warn for configured route leaves that no host input consumes.

    Routes may read one table (a program route that is also a function route,
    a group table a program inherits), so a leaf is consumed when any route
    reading its table declares it. Registered command tables are validated
    only against their program, never against a colliding declaration route.
    """
    command_tables = package_command_paths | frozenset(
        path for report in reports for path in report.route.command_paths
    )
    foreign_commands = [
        command_tables - frozenset(report.route.command_paths) for report in reports
    ]
    declared_by_table: dict[tuple[str, ...], set[str]] = {}
    for report, foreign in zip(reports, foreign_commands, strict=True):
        read_tables = (
            *(path for path in report.route.exact_table_paths() if path not in foreign),
            *(path for tier in report.route.inherited_table_tiers() for path in tier),
        )
        for table_path in read_tables:
            declared_by_table.setdefault(table_path, set()).update(report.declared_leaves)
    for report, foreign in zip(reports, foreign_commands, strict=True):
        exempt_tables = foreign | _tables_with_registered_descendants(
            report.route.command_paths, package_command_paths
        )
        leaf_tables = configured_leaf_tables(
            config,
            report.route,
            nested_routes=nested_routes,
            excluded_tables=exempt_tables,
        )
        for leaf in sorted(leaf_tables):
            table_path = leaf_tables[leaf]
            table_name = display_table_path(table_path)
            if leaf in declared_by_table.get(table_path, ()):
                continue
            if leaf in report.positional_only:
                _warn_ignored_config_key(leaf, table_name, _POSITIONAL_ONLY)
                continue
            _warn_ignored_config_key(leaf, table_name, _UNDECLARED)


def _report_unused_manifest_leaves(
    package_config: TomlDict,
    command_paths: tuple[tuple[str, ...], ...],
    consumed: frozenset[str],
    positional_only: frozenset[str],
    package_command_paths: frozenset[tuple[str, ...]],
) -> None:
    """Warn for manifest leaves in the selected command's own tables that nothing consumes.

    *consumed* holds every spelling that addresses a declared parameter, a program
    argument or engine setting included: the spellings reached parameters won on
    the surface, and all spellings of the unreached ones. *positional_only* holds
    the program's positional-only argument names, which get their own reason.
    Inherited group and root tables are skipped, since a sibling command may be
    the one that consumes them, as are tables a registered descendant command
    inherits from.
    """
    exempt_tables = _tables_with_registered_descendants(command_paths, package_command_paths)
    own_tables = {("config", *path) for path in command_paths} - {
        ("config", *path) for path in exempt_tables
    }
    leaf_tables = manifest_leaf_tables(package_config, command_paths, package_command_paths)
    for leaf in sorted(leaf_tables):
        table_path = leaf_tables[leaf]
        if table_path not in own_tables or leaf in consumed:
            continue
        reason = _POSITIONAL_ONLY if leaf in positional_only else _NOT_A_PARAMETER
        print(
            f"warning: manifest config key '{leaf}' in the '{display_table_path(table_path)}' "
            f"table {reason} and will be ignored",
            file=sys.stderr,
        )


def resolve_param_values(
    config: GeneralConfig,
    program: ProgramDeclInfo,
    params: Mapping[HostParamKey, object],
    *,
    entry_segments: tuple[str, ...],
    command_paths: tuple[tuple[str, ...], ...],
    surface: ParamSurface,
    declared_params: Sequence[HostParamInfo],
    package_command_paths: frozenset[tuple[str, ...]] = frozenset(),
    package_config: TomlDict | None = None,
    declared_programs: Sequence[ProgramDeclInfo] = (),
    positional: frozenset[FunctionParamKey] = frozenset(),
    manifest_anchor: PathAnchor | None = None,
    inherit_scope: InheritScope | None = None,
) -> ParamValueTiers:
    """Resolve parameter config values beneath parsed CLI/environment values.

    The one resolver of every host parameter kind: *params* are the CLI and
    environment values, *positional* the program arguments a positional token
    bound (a CLI value, so configured values skip it). Below them it ranks the
    program route (exact, then group tables), the manifest ``[config]``
    (command, group, root) and the declaration route; the selected program's
    ``@config`` entries rank between the first and the second, merged by
    ``PipelineDriver.preflight_arguments``. Declaration routes are addressed by each
    parameter's declaration module and scope, a function parameter's by its
    function. A selected program's qualified table overrides a parameter through
    any spelling it wins on *surface*: its bare name, or a qualified spelling when
    a nearer declaration claims that name. The two routes are resolved
    independently, so a program route wins even when its value comes from a
    less-specific config layer than the declaration route. The supplied and
    program-route values form :attr:`ParamValueTiers.upper`; the
    declaration-route values not already covered by that tier form
    :attr:`ParamValueTiers.lower`.

    *package_config* is the owning package's manifest ``[config]`` table
    (``None``/empty when the program is not package-owned). Its values
    address the same program-route spellings and are merged into ``lower``,
    above the plain declaration-route value, for every key not already resolved
    in ``upper`` — never overriding an ``upper`` entry. Its path values anchor
    at *manifest_anchor*, the package root.

    Configured leaves on those routes that no host input consumes are reported
    as warnings here, where the routes are known. *package_command_paths* is
    every command path the owning package registers (aliases expanded), used
    to exempt a command's own table from that warning when one of its leaves
    is really an inherited default for a registered descendant command.

    Program arguments and engine settings are addressed like surface entries,
    so their program-route and manifest values merge into the tiers like any
    parameter's. A program argument has no declaration route; an engine setting's
    is ``[exec]``.

    Only the *surface* parameters (those the program reaches) take values.
    *declared_params* are the parameters the program's closure declares, reached
    or not, and never an engine setting; a configured key addressing one of them is not
    undeclared, so it draws no warning. *declared_programs*
    are the other programs the closure declares: the declaration route of each is
    also that program's route, so it declares all its name-addressable arguments.

    A table at a leaf position is a nested route, not a value, when it is a
    command table (``package_command_paths`` and the program's own) or a route
    table of any declaration or the program; any other table is a native value.

    A parameter's ``@inherit`` addresses fill ``inherited`` for every parameter
    that no CLI, environment, positional or program-route value covers, read from
    the registered commands of *inherit_scope* (``None``: ``@inherit`` is not
    resolved). An address naming no registered command warns for every
    parameter, covered or not.
    """
    supplied = frozenset(params) | positional
    entries = surface.entries
    module_params = tuple(entry.param for entry in entries)
    reached = frozenset(param.key for param in module_params)
    unreached_leaves = _declared_leaves(
        param for param in declared_params if param.key not in reached
    )
    targets = _targets(program, entries)
    program_routes = _program_routes(program, entry_segments, command_paths, targets)
    program_leaves = unreached_leaves | frozenset(
        spelling for _target, key in program_routes for spelling in key.leaf_spellings()
    )
    reports = _route_reports(
        program, entry_segments, command_paths, declared_params, declared_programs, program_leaves
    )
    nested_routes = known_route_tables(
        (report.route for report in reports), (*package_command_paths, *command_paths)
    )
    if entry_segments:
        program_tables = configured_leaf_table_candidates(
            config,
            ConfigRoute(entry_segments, program.route_path, command_paths),
            nested_routes=nested_routes,
        )
        _reject_configured_cross_route_ambiguities(
            config, targets, module_params, program_tables, nested_routes
        )
        _reject_configured_ambiguous_program_leaves(
            program_tables, command_paths, surface, package_command_paths, package_config
        )
    module_values = resolve_declaration_route_values(config, module_params, nested_routes)
    program_values = resolve_qualified_values(
        config, tuple(key for _target, key in program_routes), nested_routes
    )
    upper = dict(params)
    _merge_route_values(upper, supplied, program_routes, program_values)
    lower: dict[HostParamKey, object] = {
        key: value for key, value in module_values.items() if key not in upper
    }
    if package_config:
        manifest_values = resolve_manifest_values(
            package_config,
            tuple(key for _target, key in program_routes),
            manifest_anchor,
            frozenset((*package_command_paths, *command_paths)),
        )
        _merge_route_values(lower, frozenset(upper) | positional, program_routes, manifest_values)
        _report_unused_manifest_leaves(
            package_config,
            command_paths,
            program_leaves,
            _positional_only_leaves(program),
            package_command_paths,
        )

    _report_undeclared_config_keys(config, reports, package_command_paths, nested_routes)
    inherited = (
        {}
        if inherit_scope is None
        else _inherited_values(config, targets, inherit_scope, upper.keys() | supplied)
    )
    return ParamValueTiers(upper=upper, lower=lower, supplied=supplied, inherited=inherited)


def colliding_declarations(params: Iterable[HostParamInfo]) -> str | None:
    """Describe the first full spelling shared by several named-module *params*, if any.

    Anonymous-entry parameters are exempt: their spellings are claimed after
    the named modules'.
    """
    return full_spelling_collision(
        (param.declared_spellings[-1], host_param_label(param.key))
        for param in params
        if not param.module.is_entry
    )


def report_colliding_declarations(params: Iterable[HostParamInfo]) -> bool:
    """Print the error for *params* sharing a full spelling; return whether they do."""
    collision = colliding_declarations(params)
    if collision is not None:
        print(f"Error: {collision}.", file=sys.stderr)
    return collision is not None


class _Target(NamedTuple):
    """A parameter the selected program's tables address, by every leaf spelling it wins.

    ``label`` names it in diagnostics: a program argument by its name, any other
    parameter by its qualified spelling. ``param`` is the argument or parameter itself.
    """

    label: str
    key: HostParamKey
    spellings: tuple[str, ...]
    param: ProgramParamInfo | HostParamInfo


def _targets(program: ProgramDeclInfo, entries: Sequence[ParamSurfaceEntry]) -> tuple[_Target, ...]:
    """Return the program arguments and surface parameters its own tables address."""
    arguments = tuple(
        _Target(argument.name, program.param_key(argument.name), (argument.cli.name,), argument)
        for argument in program.parameters
        if argument.kind in NAME_ADDRESSABLE_ZONES
    )
    parameters = tuple(
        _Target(entry.param.declaration_path, entry.param.key, entry.spellings, entry.param)
        for entry in entries
    )
    return (*arguments, *parameters)


def _inherited_values(
    config: GeneralConfig,
    targets: Sequence[_Target],
    scope: InheritScope,
    covered: Collection[HostParamKey],
) -> dict[HostParamKey, InheritedParamValue]:
    """Resolve the ``@inherit`` values of *targets* not in *covered*, as raw argument values.

    Covered targets are resolved only for the diagnostics of their addresses.
    """
    resolved = resolve_inherited_values(
        config,
        {target.key: InheritRequest(target.label, target.param.inherit) for target in targets},
        scope,
    )
    by_key = {target.key: target for target in targets}
    return {
        key: InheritedParamValue(
            _native_value(by_key[key].param, value.configured),
            value.address,
        )
        for key, value in resolved.items()
        if key not in covered
    }


def _reject_configured_cross_route_ambiguities(
    config: GeneralConfig,
    targets: Sequence[_Target],
    module_params: Sequence[HostParamInfo],
    program_tables: Mapping[str, frozenset[tuple[str, ...]]],
    nested_routes: frozenset[tuple[str, ...]],
) -> None:
    """Reject a configured leaf that feeds a declared parameter and another parameter.

    A module or function parameter's declaration route addresses it by its bare name; the
    selected program's tables (*program_tables*: its exact route and the group
    tables it inherits) address every spelling each of *targets* (program
    arguments and surface parameters) won. One table may be both
    (``[devel.review]`` is the ``devel review`` program route and the
    ``devel/review`` declaration route), so a leaf set there that names a different
    parameter on each side feeds both. An engine setting's declaration
    route, the ``[exec]`` table, is never a program route, so it never shares a table with one.
    """
    if not program_tables:
        return
    for param, key in _declaration_routes(module_params):
        peers = tuple(
            target for target in targets if target.key != param.key and key.leaf in target.spellings
        )
        if not peers:
            continue
        module_tables = configured_leaf_table_candidates(
            config, key.route, nested_routes=nested_routes
        )
        shared = module_tables.get(key.leaf, frozenset()) & program_tables.get(
            key.leaf, frozenset()
        )
        if shared:
            _raise_ambiguous_leaf(
                min(shared),
                key.leaf,
                (param.declaration_path, *(peer.label for peer in peers)),
            )


def _raise_ambiguous_leaf(table: tuple[str, ...], leaf: str, names: Iterable[str]) -> NoReturn:
    raise QualifiedConfigLookupError(
        f"config key {display_table_path(table)}.{leaf} matches multiple parameters: "
        f"{', '.join(names)}"
    )


def _reject_configured_ambiguity(
    leaf_tables: Mapping[str, tuple[str, ...]],
    candidates: Mapping[str, Sequence[HostParamInfo]],
) -> None:
    """Reject the first *candidates* leaf that one of *leaf_tables*' tables sets."""
    for leaf, claimants in candidates.items():
        table = leaf_tables.get(leaf)
        if table is not None:
            _raise_ambiguous_leaf(
                table, leaf, (claimant.declaration_path for claimant in claimants)
            )


def declaration_route_tables(params: Iterable[HostParamInfo]) -> frozenset[tuple[str, ...]]:
    """Return the config table paths that are declaration routes of *params*."""
    return known_route_tables(key.route for _param, key in _declaration_routes(tuple(params)))


def resolve_declaration_route_values(
    config: GeneralConfig,
    params: Sequence[HostParamInfo],
    nested_routes: frozenset[tuple[str, ...]] = frozenset(),
) -> dict[HostParamKey, object]:
    """Resolve module and function parameter values from their declaration routes only.

    This is the config surface used by incremental hosts, which initialize
    modules without selecting a program, so they have no program route. A table
    at a leaf position is a nested route when it is a route of *params* or in
    *nested_routes* (other declarations the host knows), else a native value.
    """
    routes = _declaration_routes(params)
    configured = resolve_qualified_values(
        config, tuple(key for _param, key in routes), nested_routes
    )
    values: dict[HostParamKey, object] = {}
    for param, key in routes:
        if key not in configured:
            continue
        values[param.key] = _native_value(param, configured[key])
    return values


def _native_value(param: ProgramParamInfo | HostParamInfo, configured: ConfiguredValue) -> object:
    """Project a configured value onto *param*'s raw argument shape.

    A path-typed value anchors to the file (or manifest) that supplied it,
    unless it is the ``Optional`` keyword standing for no path.
    """
    projected = project_option(param.cli.name, param.type)
    raw = configured.value
    if param.is_path and not is_optional_default(projected, raw):
        raw = anchor_path_value(raw, configured.anchor)
    option_text = isinstance(param, HostParamInfo) and param.config_option_text
    return native_raw_value(projected, raw, option_text=option_text)


def _declaration_routes(
    params: Sequence[HostParamInfo],
) -> tuple[tuple[HostParamInfo, QualifiedConfigKey], ...]:
    """Return declaration routes for every non-anonymous parameter.

    An engine setting's declaration route is the ``[exec]`` table.
    """
    return tuple(
        (
            param,
            QualifiedConfigKey(
                ConfigRoute.in_section(EXEC_CONFIG_SECTION)
                if param.kind is HostParamKind.ENGINE
                else ConfigRoute(param.module.segments, param.route_path),
                param.cli.name,
            ),
        )
        for param in params
        if not param.module.is_entry
    )


def _program_routes(
    program: ProgramDeclInfo,
    entry_segments: tuple[str, ...],
    command_paths: tuple[tuple[str, ...], ...],
    targets: Sequence[_Target],
) -> tuple[tuple[_Target, QualifiedConfigKey], ...]:
    """Return selected-program routes for resolving every host parameter's spellings.

    Every spelling a parameter wins on the CLI surface addresses it in the
    selected program's table too, so a parameter whose bare name is claimed by
    a nearer declaration stays configurable per program through a qualified
    spelling. Program arguments and engine settings route like module
    parameters. :class:`ParamSurface` is the sole authority on which spellings
    surface parameters won.
    """
    if not entry_segments:
        return ()
    routes: list[tuple[_Target, QualifiedConfigKey]] = []
    for target in targets:
        if not target.spellings:
            continue
        leaf, *aliases = target.spellings
        routes.append(
            (
                target,
                QualifiedConfigKey(
                    ConfigRoute(entry_segments, program.route_path, command_paths),
                    leaf,
                    tuple(aliases),
                ),
            )
        )
    return tuple(routes)


def _merge_route_values(
    values: dict[HostParamKey, object],
    supplied: frozenset[HostParamKey],
    routes: Iterable[tuple[_Target, QualifiedConfigKey]],
    configured: Mapping[QualifiedConfigKey, ConfiguredValue],
) -> None:
    """Project configured native values into *values*, skipping keys already in *supplied*.

    Shared by the program-route merge into ``upper`` (*supplied* = CLI/
    environment keys) and the manifest merge into ``lower`` (*supplied* =
    every key already resolved in ``upper``, so a manifest value never
    overrides one): both are "does a higher tier already own this key".
    """
    for target, key in routes:
        if target.key in supplied or key not in configured:
            continue
        values[target.key] = _native_value(target.param, configured[key])


def _reject_configured_ambiguous_program_leaves(
    program_tables: Mapping[str, frozenset[tuple[str, ...]]],
    command_paths: tuple[tuple[str, ...], ...],
    surface: ParamSurface,
    package_command_paths: frozenset[tuple[str, ...]],
    package_config: TomlDict | None = None,
) -> None:
    """Reject a configured program-table leaf claimed by peer declared parameters.

    Every spelling a program table may use is checked, bare or qualified: a
    spelling several parameters claim resolves to none of them, so *using* it
    names the candidates rather than silently picking one. *program_tables*
    covers the exact route and the config group tables, since an inherited
    leaf is just as ambiguous as one set on the exact route. *package_config*
    extends the same check over the owning package's manifest command, group,
    and root tables.
    """
    configured = {leaf: min(tables) for leaf, tables in program_tables.items()}
    if package_config:
        manifest_leaves = manifest_leaf_tables(package_config, command_paths, package_command_paths)
        configured = {**manifest_leaves, **configured}
    _reject_configured_ambiguity(configured, surface.ambiguous)


def _route_reports(
    program: ProgramDeclInfo,
    entry_segments: tuple[str, ...],
    command_paths: tuple[tuple[str, ...], ...],
    declared_params: Sequence[HostParamInfo],
    declared_programs: Sequence[ProgramDeclInfo],
    program_leaves: frozenset[str],
) -> list[_RouteReport]:
    """Describe declaration and selected-program config routes for warning reporting.

    The program route declares *program_leaves*: the spellings reached parameters
    won plus unreached parameters' leaves, never a spelling a reached parameter lost. The
    declaration route of each *declared_programs* entry is also that program's
    route, so it declares every name-addressable argument of the program.
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
                ConfigRoute(module.segments),
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
            reports.append(
                _RouteReport(ConfigRoute(module.segments, scope_path), declared, frozenset())
            )

    for other in declared_programs:
        if not other.module.is_entry:
            reports.append(
                _RouteReport(
                    ConfigRoute(other.module.segments, other.route_path),
                    _program_leaves(other),
                    _positional_only_leaves(other),
                )
            )

    if entry_segments:
        reports.append(
            _RouteReport(
                ConfigRoute(entry_segments, program.route_path, command_paths),
                program_leaves,
                _positional_only_leaves(program),
            )
        )
    return reports


def _program_leaves(program: ProgramDeclInfo) -> frozenset[str]:
    """Return the config spellings of *program*'s name-addressable arguments."""
    return frozenset(
        parameter.cli.name
        for parameter in program.parameters
        if parameter.kind in NAME_ADDRESSABLE_ZONES
    )


def _positional_only_leaves(program: ProgramDeclInfo) -> frozenset[str]:
    """Return the config spellings of *program*'s positional-only arguments."""
    return frozenset(
        parameter.cli.name
        for parameter in program.parameters
        if parameter.kind is ParamZone.POSITIONAL_ONLY
    )


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
