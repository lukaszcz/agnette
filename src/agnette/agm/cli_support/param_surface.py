"""Pure spelling and claim-level table for host parameters."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType

from agnette.agl.runtime.types import (
    HostParamInfo,
    HostParamKind,
    ProgramDeclInfo,
    ProgramParamInfo,
)
from agnette.agl.zones import ParamZone
from agnette.agm.cli_support.program_options import (
    ProjectedOption,
    param_spellings,
    project_option,
    project_param_option,
)

__all__ = ["ParamSurface", "ParamSurfaceEntry", "build_param_surface"]


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

    @property
    def primary_flag(self) -> str:
        """Return an available spelling, or the logical positive flag for diagnostics."""
        for flags in (self.positive_option_spellings, self.negative_option_spellings):
            if flags:
                return flags[0]
        return f"--{self.param.cli.name}"


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
    ``projections[i]`` is the projection of ``spellings[i]``.
    """

    param: HostParamInfo
    spellings: tuple[str, ...]
    projections: tuple[ProjectedOption, ...]

    def claimed(
        self,
        winners: Mapping[str, HostParamInfo | None],
        ambiguous: Mapping[str, tuple[HostParamInfo, ...]],
        *,
        only_won: bool,
    ) -> "_ParamRoutes":
        """Keep the long names the parameter won, or (not *only_won*) is a peer claimant of.

        The flags a long name derives (``--x``, ``--no-x``) belong to whoever
        won that name, so a shadowed parameter keeps none of them.
        """
        kept = [
            index
            for index, spelling in enumerate(self.spellings)
            if winners.get(spelling) == self.param
            or (not only_won and self.param in ambiguous.get(spelling, ()))
        ]
        return _ParamRoutes(
            param=self.param,
            spellings=tuple(self.spellings[index] for index in kept),
            projections=tuple(self.projections[index] for index in kept),
        )


_Claimant = HostParamInfo | None
_Claims = dict[str, tuple[_Claimant, ...]]


def _append_claim(claims: dict[str, list[_Claimant]], spelling: str, claimant: _Claimant) -> None:
    claims.setdefault(spelling, []).append(claimant)


def _freeze_claims(claims: dict[str, list[_Claimant]]) -> _Claims:
    return {spelling: tuple(claimants) for spelling, claimants in claims.items()}


def _param_routes(param: HostParamInfo, *, bare_only: bool) -> _ParamRoutes:
    """Project *param*'s logical long-option names, shortest route first.

    Only the bare name when *bare_only*.
    """
    names = param.declared_spellings[: 1 if bare_only else None]
    return _ParamRoutes(
        param=param,
        spellings=names,
        projections=tuple(project_param_option(param, name) for name in names),
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


def _signature_long_claims(parameters: Iterable[ProgramParamInfo]) -> _Claims:
    claims: dict[str, list[_Claimant]] = {}
    for parameter in parameters:
        _append_claim(claims, parameter.cli.name, None)
    return _freeze_claims(claims)


def _signature_option_claims(parameters: Iterable[ProgramParamInfo]) -> _Claims:
    claims: dict[str, list[_Claimant]] = {}
    for parameter in parameters:
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

    Function parameters share the module levels, chosen by their module; engine
    settings form the last level, after them.
    """

    PROGRAM_MODULE = 0  # declared in the program's module
    OWNER = 1  # declared in a module of the program's owner
    OTHER = 2  # declared in a module of any other owner
    ENGINE = 3  # an engine setting


def _param_level(param: HostParamInfo, program: ProgramDeclInfo | None) -> ParamLevel:
    """Return the claim level of *param* for *program*; an unknown owner is nobody's peer."""
    if param.kind is HostParamKind.ENGINE:
        return ParamLevel.ENGINE
    if program is not None and param.module == program.module:
        return ParamLevel.PROGRAM_MODULE
    if param.owner is not None and program is not None and param.owner == program.owner:
        return ParamLevel.OWNER
    return ParamLevel.OTHER


def build_param_surface(
    reserved_flags: Collection[str],
    program: ProgramDeclInfo | None,
    params: Iterable[HostParamInfo],
    *,
    bare_only: bool = False,
) -> ParamSurface:
    """Build the resolving parameter spellings for *program* (``None``: a host with no program).

    Claim levels, first claimant wins a spelling: the host's reserved flags,
    the program signature, parameters declared in the program's module,
    parameters of the program's owner, parameters of other owners, engine
    settings.  Several parameters claiming one spelling on a level make it
    ambiguous, but only where it is used.  With *bare_only* a parameter claims
    just its bare name.
    """
    signature = () if program is None else program.parameters
    routes = tuple(_param_routes(param, bare_only=bare_only) for param in params)
    tiers: tuple[list[_ParamRoutes], ...] = tuple([] for _ in ParamLevel)
    for route in routes:
        tiers[_param_level(route.param, program)].append(route)

    long_winners, ambiguous = _resolve_claims(
        (
            _host_long_claims(reserved_flags),
            _signature_long_claims(signature),
            *(_param_long_claims(tier) for tier in tiers),
        )
    )
    # Option flags derive from long names, so only a long name's winner (or,
    # to report it, a peer claimant) contends for the flags of that name.
    contenders = [
        [route.claimed(long_winners, ambiguous, only_won=False) for route in tier] for tier in tiers
    ]
    option_winners, ambiguous_options = _resolve_claims(
        (
            _host_option_claims(reserved_flags),
            _signature_option_claims(signature),
            *(_param_option_claims(tier) for tier in contenders),
        )
    )
    entries = tuple(
        _entry(route.claimed(long_winners, ambiguous, only_won=True), option_winners)
        for tier in tiers
        for route in tier
    )
    return ParamSurface(
        entries=entries,
        ambiguous=MappingProxyType(ambiguous),
        ambiguous_options=MappingProxyType(ambiguous_options),
    )


def _entry(
    route: _ParamRoutes, option_winners: Mapping[str, HostParamInfo | None]
) -> ParamSurfaceEntry:
    """Keep the option flags *route*'s parameter won, split by the polarity each fills.

    *route* holds just the long names the parameter won.
    """
    param = route.param
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
        spellings=route.spellings,
        option_spellings=tuple(option_spellings),
        positive_option_spellings=tuple(positive_option_spellings),
        negative_option_spellings=tuple(negative_option_spellings),
        short_option=short_option,
        section=param.module.display(),
        hidden=param.cli.hidden,
    )
