"""Configuration routes shared by host-configurable parameters."""

from __future__ import annotations

import sys
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import NamedTuple

from agnette.agl.diagnostics import Diagnostic
from agnette.agl.ir.contracts import ParamDecoder
from agnette.agl.ir.static_keys import (
    FunctionParamKey,
    HostParamKey,
    StaticBindingKey,
    host_param_label,
)
from agnette.agl.modules.ids import ModuleId
from agnette.agl.runtime.arguments import InheritedParamValue, ParamTier
from agnette.agl.runtime.engine_config import (
    EngineSettings,
    engine_setting_decoder,
    engine_setting_params,
    split_engine_values,
)
from agnette.agl.runtime.param_resolution import resolve_host_params
from agnette.agl.runtime.types import (
    HostParamInfo,
    HostParamKind,
    ProgramDeclInfo,
    ProgramParamInfo,
)
from agnette.agl.zones import NAME_ADDRESSABLE_ZONES, ParamZone
from agnette.config.claims import (
    Claimant,
    ClaimsIndex,
    ConfigSource,
    ConfiguredValue,
    Table,
    display_table_path,
)
from agnette.config.general import (
    EXEC_CONFIG_SECTION,
    EXEC_OPTION_KEYS,
    GeneralConfig,
    PathAnchor,
    anchor_path_value,
)
from agnette.config.qualified_keys import (
    ConfigRoute,
    full_spelling_collision,
    known_route_tables,
    manifest_routes,
    manifest_table_tiers,
)
from agnette.core.toml import TomlDict
from agnette.host_params.inherit import (
    InheritRequest,
    InheritScope,
    resolve_inherited_values,
)
from agnette.host_params.projection import config_raw_value
from agnette.host_params.surface import ParamSurface, ParamSurfaceEntry

__all__ = [
    "ParamValueTiers",
    "colliding_declarations",
    "declaration_route_tables",
    "report_colliding_declarations",
    "resolve_declaration_route_values",
    "resolve_param_values",
    "ProgramlessEngineValues",
    "programless_engine_values",
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


class RouteKind(Enum):
    """How a claim reaches its tables."""

    PROGRAM = "program"  # the selected program's tables
    DECLARATION = "declaration"  # a declaration's own tables (``[exec]`` for engine settings)
    MANIFEST = "manifest"  # a package manifest's ``[config]`` tables


def _tables_with_registered_descendants(
    command_paths: tuple[tuple[str, ...], ...],
    package_command_paths: frozenset[tuple[str, ...]],
) -> frozenset[tuple[str, ...]]:
    """Return which of *command_paths* is a proper prefix of another registered command.

    Such a table's own leaves may be inherited defaults meant for that
    descendant command (see the group-table inheritance in
    :meth:`agnette.config.qualified_keys.ConfigRoute.table_tiers`), so a warning
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


# The engine settings' declaration route; its execution options are declared beside them.
_EXEC_ROUTE = ConfigRoute.in_section(EXEC_CONFIG_SECTION)


def _warn_ignored_config_key(leaf: str, table_name: str, reason: str) -> None:
    """Print the warning that config key *leaf* in table *table_name* is ignored."""
    print(
        f"warning: config key '{leaf}' in the '{table_name}' configuration table "
        f"{reason} and will be ignored",
        file=sys.stderr,
    )


def _report_undeclared_config_keys(
    source: ConfigSource,
    index: ClaimsIndex,
    routes: Mapping[ConfigRoute, frozenset[str]],
    command_tables: frozenset[Table],
    package_command_paths: frozenset[Table],
) -> None:
    """Warn for each leaf *source* sets in the exact tables of *routes* that no claim consumes.

    *routes* map to their positional-only argument names. Another command's own
    table is validated only against its program, and a table a registered
    descendant command inherits from may hold defaults for that command. Claims
    of any route reading a table consume its leaves, so overlapping routes warn
    once.
    """
    for route, positional_only in routes.items():
        exempt = (command_tables - frozenset(route.command_paths)) | (
            _tables_with_registered_descendants(route.command_paths, package_command_paths)
        )
        tables = [path for path in route.exact_table_paths() if path not in exempt]
        for leaf, table in sorted(source.first_tables(tables).items()):
            if index.consumed(source, table, leaf):
                continue
            reason = _POSITIONAL_ONLY if leaf in positional_only else _UNDECLARED
            _warn_ignored_config_key(leaf, display_table_path(table), reason)


def _report_unused_manifest_leaves(
    source: ConfigSource,
    index: ClaimsIndex,
    command_paths: tuple[Table, ...],
    positional_only: frozenset[str],
    package_command_paths: frozenset[Table],
) -> None:
    """Warn for manifest leaves in the selected command's own tables that no claim declares.

    The claims are those on the program route's spellings, a program argument
    or engine setting included, and on the unreached parameters' spellings.
    *positional_only* holds the program's positional-only argument names, which
    get their own reason. Inherited group and root tables are skipped, since a
    sibling command may be the one that consumes them, as are tables a
    registered descendant command inherits from.
    """
    exempt = _tables_with_registered_descendants(command_paths, package_command_paths)
    own_tables = {("config", *path) for path in command_paths} - {
        ("config", *path) for path in exempt
    }
    tables = [path for tier in manifest_table_tiers(command_paths) for path in tier]
    for leaf, table in sorted(source.first_tables(tables).items()):
        if table not in own_tables or index.consumed(source, table, leaf):
            continue
        reason = _POSITIONAL_ONLY if leaf in positional_only else _NOT_A_PARAMETER
        print(
            f"warning: manifest config key '{leaf}' in the '{display_table_path(table)}' "
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
    program_route = (
        ConfigRoute(entry_segments, program.route_path, command_paths) if entry_segments else None
    )
    targets = _targets(program, surface.entries)
    positional_only = _positional_only_leaves(program)
    command_tables = package_command_paths | frozenset(
        command_paths if program_route is not None else ()
    )
    claims = _build_claims(
        program,
        program_route,
        command_paths,
        command_tables,
        targets,
        surface,
        declared_params,
        declared_programs,
        positional_only,
        with_manifest=bool(package_config),
    )
    nested_routes = known_route_tables(claims.routes, (*package_command_paths, *command_paths))
    files = ConfigSource.from_config(config, nested_routes)
    claims.index.reject_ambiguities(files)
    manifest = (
        ConfigSource.from_manifest(
            package_config,
            manifest_anchor,
            manifest_routes(command_paths, frozenset((*package_command_paths, *command_paths))),
        )
        if package_config
        else None
    )
    if manifest is not None:
        claims.index.reject_ambiguities(manifest)

    resolved = claims.index.resolve(files)
    upper = dict(params)
    _merge_values(upper, supplied, claims.values(resolved, RouteKind.PROGRAM))
    declaration_values = claims.values(resolved, RouteKind.DECLARATION)
    lower: dict[HostParamKey, object] = {
        key: value for key, value in declaration_values.items() if key not in upper
    }
    if manifest is not None:
        manifest_values = claims.values(claims.index.resolve(manifest), RouteKind.MANIFEST)
        _merge_values(lower, frozenset(upper) | positional, manifest_values)
        _report_unused_manifest_leaves(
            manifest, claims.index, command_paths, positional_only, package_command_paths
        )

    _report_undeclared_config_keys(
        files, claims.index, claims.routes, command_tables, package_command_paths
    )
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
        (spelling, host_param_label(param.key))
        for param in params
        if not param.module.is_entry
        for spelling in (
            param.declared_spellings[-1],
            *(
                ".".join((*param.declared_spellings[-1].split(".")[:-1], alias))
                for alias in param.cli.name_aliases
            ),
        )
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
    ``spellings`` are the leaves addressing it in the selected program's table.
    """

    label: str
    key: HostParamKey
    param: ProgramParamInfo | HostParamInfo
    spellings: tuple[str, ...]


def _targets(program: ProgramDeclInfo, entries: Sequence[ParamSurfaceEntry]) -> tuple[_Target, ...]:
    """Return the program arguments and surface parameters its own tables address.

    Every spelling a parameter wins on the CLI surface addresses it in the
    selected program's table too, so a parameter whose bare name is claimed by
    a nearer declaration stays configurable per program through a qualified
    spelling. Program arguments and engine settings route like module
    parameters. :class:`ParamSurface` is the sole authority on which spellings
    surface parameters won.
    """
    return (
        *(
            _Target(
                argument.name,
                program.param_key(argument.name),
                argument,
                argument.cli.names,
            )
            for argument in _addressable_arguments(program)
        ),
        *(
            _Target(entry.param.declaration_path, entry.param.key, entry.param, entry.spellings)
            for entry in entries
        ),
    )


class _Claims:
    """The claims index of one resolution, with the parameters its claimants feed.

    ``routes`` holds every route read, with the positional-only argument names it
    reports (empty for most). ``params`` are the parameters whose claimants feed.
    """

    def __init__(self, command_tables: frozenset[Table] = frozenset()) -> None:
        self.index = ClaimsIndex()
        self.routes: dict[ConfigRoute, frozenset[str]] = {}
        self.params: dict[HostParamKey, ProgramParamInfo | HostParamInfo] = {}
        self._command_tables = command_tables

    def declare(self, params: Iterable[HostParamInfo], reached: frozenset[HostParamKey]) -> None:
        """Claim each of *params*'s name on its declaration route; only *reached* ones feed.

        ``[exec]`` also declares ``EXEC_OPTION_KEYS`` (declare-only).
        """
        by_route: dict[ConfigRoute, tuple[list[_Target], list[str]]] = {}
        for param in params:
            route = _declaration_route(param)
            if route is not None:
                feeding, declared = by_route.setdefault(route, ([], []))
                if param.key in reached:
                    feeding.append(
                        _Target(param.declaration_path, param.key, param, param.cli.names)
                    )
                else:
                    declared.extend(param.cli.names)
        if _EXEC_ROUTE in by_route:
            by_route[_EXEC_ROUTE][1].extend(EXEC_OPTION_KEYS)
        for route, (feeding, declared) in by_route.items():
            self.route(RouteKind.DECLARATION, route, feeding, declared)

    def read(self, route: ConfigRoute, positional_only: frozenset[str] = frozenset()) -> None:
        """Record *route* as read, with the *positional_only* argument names it reports."""
        self.routes[route] = self.routes.get(route, frozenset()) | positional_only

    def route(
        self,
        kind: RouteKind,
        route: ConfigRoute,
        feeding: Iterable[_Target],
        declared: Iterable[str] = (),
        positional_only: frozenset[str] = frozenset(),
    ) -> None:
        """Claim every table *route* reads, its exact tables first.

        The *feeding* targets claim their spellings and take values; the *declared*
        leaves are only declared.
        """
        self.read(route, positional_only)
        foreign = self._command_tables - frozenset(route.command_paths)
        self._claim(kind, route.table_tiers(), foreign, feeding, declared)

    def manifest(
        self, command_paths: tuple[Table, ...], feeding: Iterable[_Target], declared: Iterable[str]
    ) -> None:
        """Claim the manifest tables of *command_paths*, as :meth:`route` does."""
        self._claim(
            RouteKind.MANIFEST, manifest_table_tiers(command_paths), frozenset(), feeding, declared
        )

    def values(
        self, resolved: Mapping[Claimant, ConfiguredValue], kind: RouteKind
    ) -> dict[HostParamKey, object]:
        """Return the native values of the *resolved* claimants of *kind*, by parameter key."""
        values: dict[HostParamKey, object] = {}
        for key, param in self.params.items():
            # A claimant is compared by parameter and route; its label is no part of it.
            configured = resolved.get(Claimant(key, kind, ""))
            if configured is not None:
                values[key] = _native_value(param, configured)
        return values

    def _claim(
        self,
        kind: RouteKind,
        tiers: Sequence[Iterable[Table]],
        foreign: frozenset[Table],
        feeding: Iterable[_Target],
        declared: Iterable[str],
    ) -> None:
        manifest = kind is RouteKind.MANIFEST
        for target in feeding:
            self.params[target.key] = target.param
            self.index.claim(
                Claimant(target.key, kind, target.label),
                tiers,
                target.spellings,
                foreign=foreign,
                manifest=manifest,
            )
        self.index.claim(None, tiers, tuple(declared), foreign=foreign, manifest=manifest)


def _build_claims(
    program: ProgramDeclInfo,
    program_route: ConfigRoute | None,
    command_paths: tuple[Table, ...],
    command_tables: frozenset[Table],
    targets: Sequence[_Target],
    surface: ParamSurface,
    declared_params: Sequence[HostParamInfo],
    declared_programs: Sequence[ProgramDeclInfo],
    positional_only: frozenset[str],
    *,
    with_manifest: bool,
) -> _Claims:
    """Index every leaf the program, declaration and manifest routes claim.

    Every closure module's root route is read too, so stray keys in its table
    warn even when no parameter declares there. *positional_only* holds the
    selected program's positional-only argument names.

    Declaration routes claim each declared parameter's bare name; only the
    *surface* parameters feed values, the rest only declare. The selected
    program's routes (its tables, and its manifest tables when *with_manifest*)
    claim each target's won spellings; a spelling several surface parameters
    claim is claimed by all of them, so setting it is ambiguous. The
    declaration route of each of *declared_programs* is also that program's
    route, so it declares every name-addressable argument. *command_tables* are
    the registered commands' tables: one a route reads as its exact table
    without owning it is claimed for values only, not as a declaration.
    """
    claims = _Claims(command_tables)
    for module in _report_modules(program, declared_params):
        if not module.is_entry:
            claims.read(ConfigRoute(module.segments))
    module_params = tuple(entry.param for entry in surface.entries)
    reached = frozenset(param.key for param in module_params)
    unreached = tuple(param for param in declared_params if param.key not in reached)
    claims.declare((*module_params, *unreached), reached)
    for other in declared_programs:
        if not other.module.is_entry:
            claims.route(
                RouteKind.DECLARATION,
                ConfigRoute(other.module.segments, other.route_path),
                (),
                _program_leaves(other),
                _positional_only_leaves(other),
            )

    declared = [spelling for param in unreached for spelling in param.declared_spellings]
    feeding: list[_Target] = []
    if program_route is not None:
        feeding = [
            *targets,
            *(
                _Target(param.declaration_path, param.key, param, (spelling,))
                for spelling, peers in surface.ambiguous.items()
                for param in peers
            ),
        ]
        claims.route(RouteKind.PROGRAM, program_route, feeding, declared, positional_only)
    if with_manifest:
        claims.manifest(command_paths, feeding, declared)
    return claims


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


def declaration_route_tables(params: Iterable[HostParamInfo]) -> frozenset[tuple[str, ...]]:
    """Return the config table paths that are declaration routes of *params*."""
    return known_route_tables(route for param in params if (route := _declaration_route(param)))


def resolve_declaration_route_values(
    config: GeneralConfig,
    params: Sequence[HostParamInfo],
    nested_routes: frozenset[tuple[str, ...]] = frozenset(),
    *,
    report_undeclared: bool = False,
) -> dict[HostParamKey, object]:
    """Resolve module and function parameter values from their declaration routes only.

    This is the config surface used by incremental hosts, which initialize
    modules without selecting a program, so they have no program route. A table
    at a leaf position is a nested route when it is a route of *params* or in
    *nested_routes* (other declarations the host knows), else a native value.
    With *report_undeclared*, a key set in a route's table that no claim consumes warns
    (execution options are declared without being params); set only when *params* are the
    complete declarations of their routes (REPL engine settings), not for per-entry module
    seeding.
    """
    claims = _Claims()
    claims.declare(params, reached=frozenset(param.key for param in params))
    source = ConfigSource.from_config(config, nested_routes | known_route_tables(claims.routes))
    claims.index.reject_ambiguities(source)
    if report_undeclared:
        _report_undeclared_config_keys(
            source, claims.index, claims.routes, frozenset(), frozenset()
        )
    return claims.values(claims.index.resolve(source), RouteKind.DECLARATION)


@dataclass(frozen=True, slots=True)
class ProgramlessEngineValues:
    """A host's raw engine-setting values with no program: the ``[exec]`` route and flags."""

    declared: "Mapping[StaticBindingKey, object]"
    supplied: "Mapping[StaticBindingKey, object]"

    def resolve(
        self, decoders: "Mapping[str, ParamDecoder]", names: "Collection[str]"
    ) -> tuple[EngineSettings, tuple[Diagnostic, ...], tuple[Diagnostic, ...]]:
        """Rank the supplied over the declared values of the settings *names*, then decode.

        The ranking and decoding ``agm exec`` applies to a program's engine settings, without
        the program-bound tiers, through *decoders*
        (:func:`~agnette.agl.type_schema.engine_setting_decoders`). Returns the settings, the
        diagnostics of the invalid values, and those among them that supplied values caused.
        """

        def among(values: "Mapping[StaticBindingKey, object]") -> dict[StaticBindingKey, object]:
            return {key: raw for key, raw in values.items() if key[2] in names}

        decoded = resolve_host_params(
            {ParamTier.DECLARED: among(self.declared), ParamTier.SUPPLIED: among(self.supplied)},
            engine_setting_decoder(decoders),
        )
        return decoded.engine, decoded.diagnostics, decoded.usage_diagnostics


def programless_engine_values(
    config: "GeneralConfig", supplied: "Mapping[StaticBindingKey, object]"
) -> ProgramlessEngineValues:
    """Pair *supplied* flag values with the ``[exec]`` declaration route's; no program.

    An undeclared ``[exec]`` key warns.
    """
    declared = split_engine_values(
        resolve_declaration_route_values(config, engine_setting_params(), report_undeclared=True)
    )[1]
    return ProgramlessEngineValues(declared, supplied)


def _native_value(param: ProgramParamInfo | HostParamInfo, configured: ConfiguredValue) -> object:
    """Project a configured value onto *param*'s raw argument shape.

    A path-typed value anchors to the file (or manifest) that supplied it,
    unless it is the ``Optional`` keyword standing for no path.
    """
    return config_raw_value(
        param, configured.value, lambda raw: anchor_path_value(raw, configured.anchor)
    )


def _declaration_route(param: HostParamInfo) -> ConfigRoute | None:
    """Return the declaration route of *param*; ``None`` for an anonymous-entry parameter.

    An engine setting's declaration route is the ``[exec]`` table.
    """
    if param.module.is_entry:
        return None
    if param.kind is HostParamKind.ENGINE:
        return _EXEC_ROUTE
    return ConfigRoute(param.module.segments, param.route_path)


def _merge_values(
    values: dict[HostParamKey, object],
    skip: frozenset[HostParamKey],
    configured: Mapping[HostParamKey, object],
) -> None:
    """Add the *configured* values to *values*, skipping keys in *skip*.

    Shared by the program-route merge into ``upper`` (*skip* = CLI/environment
    keys) and the manifest merge into ``lower`` (*skip* = every key already
    resolved in ``upper``, so a manifest value never overrides one): both are
    "does a higher tier already own this key".
    """
    for key, value in configured.items():
        if key not in skip:
            values[key] = value


def _addressable_arguments(program: ProgramDeclInfo) -> Iterator[ProgramParamInfo]:
    """Yield *program*'s name-addressable arguments."""
    return (
        parameter for parameter in program.parameters if parameter.kind in NAME_ADDRESSABLE_ZONES
    )


def _program_leaves(program: ProgramDeclInfo) -> frozenset[str]:
    """Return the config spellings of *program*'s name-addressable arguments."""
    return frozenset(
        name for parameter in _addressable_arguments(program) for name in parameter.cli.names
    )


def _positional_only_leaves(program: ProgramDeclInfo) -> frozenset[str]:
    """Return the config spellings of *program*'s positional-only arguments."""
    return frozenset(
        parameter.cli.name
        for parameter in program.parameters
        if parameter.kind is ParamZone.POSITIONAL_ONLY
    )


def _report_modules(
    program: ProgramDeclInfo, declared: Sequence[HostParamInfo]
) -> tuple[ModuleId, ...]:
    """Return closure modules first, retaining any other declaring module afterward."""
    modules: list[ModuleId] = list(program.closure)
    for param in declared:
        if param.module not in modules:
            modules.append(param.module)
    return tuple(modules)
