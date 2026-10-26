"""Pure spelling and claim-level table for host parameters."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType

from agnette.agl.runtime.types import HostParamInfo, ProgramDeclInfo
from agnette.agl.zones import ParamZone
from agnette.agm.cli_support.program_options import ProjectedOption, param_spellings, project_option
from agnette.config.qualified_keys import param_spellings_for

__all__ = ["ParamSurface", "ParamSurfaceEntry", "build_param_surface", "declared_spellings"]


@dataclass(frozen=True, slots=True)
class ParamSurfaceEntry:
    """One host parameter's spellings after precedence has been applied."""

    param: HostParamInfo
    spellings: tuple[str, ...]
    option_spellings: tuple[str, ...]
    positive_option_spellings: tuple[str, ...]
    negative_option_spellings: tuple[str, ...]
    short_option: str | None
    section: str
    hidden: bool


@dataclass(frozen=True, slots=True)
class ParamSurface:
    """The resolving and ambiguous spellings of one program's host parameters."""

    entries: tuple[ParamSurfaceEntry, ...]
    ambiguous: Mapping[str, tuple[HostParamInfo, ...]]
    ambiguous_options: Mapping[str, tuple[HostParamInfo, ...]]


@dataclass(frozen=True, slots=True)
class _ParamRoutes:
    """One host parameter's long-option names and their CLI projections.

    Derived once per parameter and threaded through every level, since both
    the claim levels and the resolved entry read the same routes.
    """

    param: HostParamInfo
    spellings: tuple[str, ...]
    projections: tuple[ProjectedOption, ...]


_Claimant = HostParamInfo | None
_Claims = dict[str, tuple[_Claimant, ...]]


def _append_claim(claims: dict[str, list[_Claimant]], spelling: str, claimant: _Claimant) -> None:
    claims.setdefault(spelling, []).append(claimant)


def _freeze_claims(claims: dict[str, list[_Claimant]]) -> _Claims:
    return {spelling: tuple(claimants) for spelling, claimants in claims.items()}


def declared_spellings(param: HostParamInfo) -> tuple[str, ...]:
    """Return every spelling that can address *param*, before any claim is resolved."""
    return param_spellings_for(
        param.module.segments, param.route_path, param.cli.name, is_entry=param.module.is_entry
    )


def _param_routes(param: HostParamInfo) -> _ParamRoutes:
    """Project *param*'s logical long-option names, shortest route first."""
    names = declared_spellings(param)
    return _ParamRoutes(
        param=param,
        spellings=names,
        projections=tuple(project_option(name, param.type) for name in names),
    )


def _host_long_claims(reserved_flags: Collection[str]) -> _Claims:
    """Project host long flags into the bare-name level they reserve."""
    claims: dict[str, list[_Claimant]] = {}
    for flag in reserved_flags:
        if not flag.startswith("--"):
            continue
        name = flag[2:]
        _append_claim(claims, name, None)
        if name.startswith("no-"):
            _append_claim(claims, name[3:], None)
    return _freeze_claims(claims)


def _host_option_claims(reserved_flags: Collection[str]) -> _Claims:
    """Return the exact option flags the host has already claimed."""
    return {flag: (None,) for flag in reserved_flags}


def _signature_long_claims(program: ProgramDeclInfo) -> _Claims:
    claims: dict[str, list[_Claimant]] = {}
    for parameter in program.parameters:
        _append_claim(claims, parameter.cli.name, None)
    return _freeze_claims(claims)


def _signature_option_claims(program: ProgramDeclInfo) -> _Claims:
    claims: dict[str, list[_Claimant]] = {}
    for parameter in program.parameters:
        if parameter.kind is ParamZone.POSITIONAL_ONLY or parameter.cli.cli_positional:
            continue
        projected = project_option(parameter.cli.name, parameter.type)
        for flag in param_spellings(parameter, projected):
            _append_claim(claims, flag, None)
    return _freeze_claims(claims)


def _param_long_claims(routes: Iterable[_ParamRoutes]) -> _Claims:
    claims: dict[str, list[_Claimant]] = {}
    for route in routes:
        for spelling in route.spellings:
            _append_claim(claims, spelling, route.param)
    return _freeze_claims(claims)


def _param_option_claims(routes: Iterable[_ParamRoutes]) -> _Claims:
    claims: dict[str, list[_Claimant]] = {}
    for route in routes:
        for spelling in param_spellings(route.param, *route.projections):
            _append_claim(claims, spelling, route.param)
    return _freeze_claims(claims)


def _resolve_claims(
    levels: tuple[_Claims, ...],
) -> tuple[dict[str, HostParamInfo | None], dict[str, tuple[HostParamInfo, ...]]]:
    """Resolve claims lexically, retaining same-level parameter ambiguities."""
    winners: dict[str, HostParamInfo | None] = {}
    ambiguous: dict[str, tuple[HostParamInfo, ...]] = {}
    claimed: set[str] = set()
    for level in levels:
        for spelling, claimants in level.items():
            if spelling in claimed:
                continue
            claimed.add(spelling)
            if len(claimants) == 1:
                winners[spelling] = claimants[0]
                continue
            params = tuple(param for param in claimants if param is not None)
            if params:
                ambiguous[spelling] = params
    return winners, ambiguous


class ParamLevel(IntEnum):
    """The claim levels of parameters, in precedence order, below host flags and signature.

    Function parameters share these levels, chosen by their module; engine
    settings form the last level, after them.
    """

    PROGRAM_MODULE = 0  # declared in the program's module
    OWNER = 1  # declared in a module of the program's owner
    OTHER = 2  # declared in a module of any other owner


def _param_level(param: HostParamInfo, program: ProgramDeclInfo) -> ParamLevel:
    """Return the claim level of *param* for *program*; an unknown owner is nobody's peer."""
    if param.module == program.module:
        return ParamLevel.PROGRAM_MODULE
    if param.owner is not None and param.owner == program.owner:
        return ParamLevel.OWNER
    return ParamLevel.OTHER


def build_param_surface(
    reserved_flags: Collection[str],
    program: ProgramDeclInfo,
    params: Iterable[HostParamInfo],
) -> ParamSurface:
    """Build the resolving parameter spellings for *program*.

    Claim levels, first claimant wins a spelling: the host's reserved flags,
    the program signature, parameters declared in the program's module,
    parameters of the program's owner, parameters of other owners.  Several
    parameters claiming one spelling on a level make it ambiguous, but only
    where it is used.
    """
    routes = tuple(_param_routes(param) for param in params)
    tiers: tuple[list[_ParamRoutes], ...] = tuple([] for _ in ParamLevel)
    for route in routes:
        tiers[_param_level(route.param, program)].append(route)

    long_winners, ambiguous = _resolve_claims(
        (
            _host_long_claims(reserved_flags),
            _signature_long_claims(program),
            *(_param_long_claims(tier) for tier in tiers),
        )
    )
    option_winners, ambiguous_options = _resolve_claims(
        (
            _host_option_claims(reserved_flags),
            _signature_option_claims(program),
            *(_param_option_claims(tier) for tier in tiers),
        )
    )
    entries = tuple(_entry(route, long_winners, option_winners) for tier in tiers for route in tier)
    return ParamSurface(
        entries=entries,
        ambiguous=MappingProxyType(ambiguous),
        ambiguous_options=MappingProxyType(ambiguous_options),
    )


def _entry(
    route: _ParamRoutes,
    long_winners: Mapping[str, HostParamInfo | None],
    option_winners: Mapping[str, HostParamInfo | None],
) -> ParamSurfaceEntry:
    """Keep the spellings *route*'s parameter won, split by the polarity each fills."""
    param = route.param
    spellings = tuple(
        spelling for spelling in route.spellings if long_winners.get(spelling) == param
    )
    positive_option_spellings: list[str] = []
    negative_option_spellings: list[str] = []
    option_spellings: list[str] = []
    for projected in route.projections:
        if option_winners.get(projected.flag) == param:
            positive_option_spellings.append(projected.flag)
            option_spellings.append(projected.flag)
        negative = projected.negative_flag
        if negative is not None and option_winners.get(negative) == param:
            negative_option_spellings.append(negative)
            option_spellings.append(negative)
    short = param.cli.short
    short_option = (
        None if short is None or option_winners.get(f"-{short}") != param else f"-{short}"
    )
    if short_option is not None:
        option_spellings.append(short_option)
    return ParamSurfaceEntry(
        param=param,
        spellings=spellings,
        option_spellings=tuple(option_spellings),
        positive_option_spellings=tuple(positive_option_spellings),
        negative_option_spellings=tuple(negative_option_spellings),
        short_option=short_option,
        section=param.module.display(),
        hidden=param.cli.hidden,
    )
