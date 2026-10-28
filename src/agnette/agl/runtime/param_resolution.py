"""One precedence rule for every host parameter kind.

Module, function and engine-setting parameters and a program's own arguments are all
keyed by :data:`~agnette.agl.ir.static_keys.HostParamKey` and ranked by
:class:`~agnette.agl.runtime.arguments.ParamTier`, lowest first:
:func:`resolve_host_params` keeps each key's value from the highest tier that sets it and
decodes only those winners, with a decoder per kind, the engine settings among the results.
Whether a failure is a usage error follows from the winning tier alone.
:func:`resolve_program_params` is the whole resolution for a selected program; ``agm repl``
has no program and resolves only the engine settings through :func:`resolve_host_params`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache, partial
from itertools import chain
from typing import TYPE_CHECKING, assert_never

from agnette.agl.diagnostics import Diagnostic
from agnette.agl.ir.static_keys import FunctionParamKey, HostParamKey, split_host_param_values
from agnette.agl.runtime.arguments import (
    ParamTier,
    bind_positional_arguments,
    complete_program_arguments,
    decode_host_param,
    decode_program_argument,
    duplicate_argument_diagnostic,
    positional_only_by_name_diagnostic,
    program_signature,
    unknown_argument_diagnostic,
)
from agnette.agl.runtime.engine_config import (
    EngineSettings,
    engine_setting_decoder,
    engine_setting_name,
    engine_settings_from,
    prepare_engine_config,
    split_engine_values,
)
from agnette.agl.zones import ParamZone

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from collections.abc import Set as AbstractSet

    from agnette.agl.ir.nodes import UseDefault
    from agnette.agl.ir.program import ExecutableProgram
    from agnette.agl.ir.static_keys import StaticBindingKey
    from agnette.agl.runtime.arguments import InheritedParamValue
    from agnette.agl.runtime.convert import DefaultResolver
    from agnette.agl.runtime.types import ProgramDeclInfo
    from agnette.agl.semantics.values import Value

__all__ = [
    "DecodedParams",
    "ProgramParamResolution",
    "resolve_host_params",
    "resolve_program_params",
]


def _rank_param_tiers[K, V](
    tiers: "Mapping[ParamTier, Mapping[K, V]]",
) -> "dict[K, tuple[ParamTier, V]]":
    """Return each key's value from the highest tier that sets it, with that tier.

    The result is ordered by winning tier, highest first, then by input order within a tier.
    """
    ranked: dict[K, tuple[ParamTier, V]] = {}
    for tier in reversed(ParamTier):
        for key, value in tiers.get(tier, {}).items():
            ranked.setdefault(key, (tier, value))
    return ranked


@dataclass(frozen=True, slots=True)
class _Typed:
    """An already decoded value standing in for a raw one in a tier, such as ``@config``'s."""

    value: "Value"


@dataclass(frozen=True, slots=True)
class DecodedParams[K]:
    """The decoded winning values by key (with their tiers), the decode failures, and the
    engine settings among the values.

    ``usage_diagnostics`` are the failures of values the command line or environment
    supplied, a subset of ``diagnostics``; ``failed`` names every key that failed.
    """

    values: "Mapping[K, tuple[ParamTier, Value]]"
    diagnostics: tuple[Diagnostic, ...]
    usage_diagnostics: tuple[Diagnostic, ...]
    failed: frozenset[K]
    engine: EngineSettings


def _decode_ranked[K: HostParamKey](
    ranked: "Mapping[K, tuple[ParamTier, object]]",
    decode: "Callable[[K, ParamTier, object], Value | Diagnostic]",
) -> DecodedParams[K]:
    """Decode every winner of *ranked* with *decode*, which returns a value or a diagnostic."""
    values: dict[K, tuple[ParamTier, Value]] = {}
    diagnostics: list[Diagnostic] = []
    usage: list[Diagnostic] = []
    failed: set[K] = set()
    for key, (tier, raw) in ranked.items():
        decoded = decode(key, tier, raw)
        if isinstance(decoded, Diagnostic):
            diagnostics.append(decoded)
            if tier is ParamTier.SUPPLIED:
                usage.append(decoded)
            failed.add(key)
        else:
            values[key] = (tier, decoded)
    return DecodedParams(
        values, tuple(diagnostics), tuple(usage), frozenset(failed), engine_settings_from(values)
    )


def resolve_host_params[K: HostParamKey](
    tiers: "Mapping[ParamTier, Mapping[K, object]]",
    decode: "Callable[[K, ParamTier, object], Value | Diagnostic]",
) -> DecodedParams[K]:
    """Rank *tiers* and decode the winners with *decode*; the one resolution of every host."""
    return _decode_ranked(_rank_param_tiers(tiers), decode)


@dataclass(frozen=True, slots=True)
class ProgramParamResolution:
    """The resolved host parameters of a selected program, split by destination.

    ``arguments`` are the program's own arguments, one entry per parameter; the seeds
    are the module and function parameter values (the program's own ``@param``
    arguments among them).
    """

    arguments: "tuple[Value | UseDefault, ...]"
    param_seeds: "Mapping[StaticBindingKey, Value]"
    function_param_seeds: "Mapping[FunctionParamKey, Value]"
    engine: EngineSettings
    diagnostics: tuple[Diagnostic, ...]
    usage_diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class _Engine:
    """A key naming an engine setting."""

    name: str


@dataclass(frozen=True, slots=True)
class _Argument:
    """A key naming the selected program's own argument *name*."""

    name: str


@dataclass(frozen=True, slots=True)
class _Param:
    """A key naming any other module or function parameter."""


type _Kind = _Engine | _Argument | _Param


def _kind(program: "ProgramDeclInfo", key: HostParamKey) -> _Kind:
    """Classify *key* as an engine setting, an argument of *program*, or another parameter."""
    engine_name = engine_setting_name(key)
    if engine_name is not None:
        return _Engine(engine_name)
    if isinstance(key, FunctionParamKey) and key.declaration_key == program.declaration_key:
        return _Argument(key.param)
    return _Param()


def resolve_program_params(
    executable: "ExecutableProgram",
    program: "ProgramDeclInfo",
    *,
    positional: "tuple[object, ...]",
    lower: "Mapping[HostParamKey, object]",
    config: "Mapping[HostParamKey, Value]",
    inherited: "Mapping[HostParamKey, InheritedParamValue]",
    upper: "Mapping[HostParamKey, object]",
    supplied: "AbstractSet[HostParamKey]",
    default_resolver: "DefaultResolver",
) -> ProgramParamResolution:
    """Rank and decode every host parameter value of *program*, whatever its kind.

    The tiers, lowest first: *lower* (the declaration route), *config* (the program's own
    ``@config`` values, already typed), *inherited* (each with its ``@inherit`` address),
    then *upper* split by *supplied* into configured and supplied values, the *positional*
    arguments ranking with the latter, first. A value a higher tier overrides is never
    decoded, except an invalid ``@config`` engine value, which is always reported. An
    argument given positionally and in any tier but ``@config`` is a usage error.
    Diagnostics come in the order: positional binding, duplicates, ``@config`` engine
    values, decode failures (by winning tier, highest first, then input order), missing
    arguments.
    """
    signature = program_signature(executable, program)
    parameters = {param.name: param for param in signature.parameters}
    positional_args, positional_diagnostics = bind_positional_arguments(signature, positional)
    positional_values: dict[HostParamKey, object] = {
        program.param_key(name): raw for name, raw in positional_args.items()
    }
    duplicate_diagnostics = tuple(
        duplicate_argument_diagnostic(parameters[name])
        for name in positional_args
        if any(program.param_key(name) in tier for tier in (lower, inherited, upper))
    )

    others, engine_config = split_engine_values(config)
    valid_engine_config, config_diagnostics = prepare_engine_config(
        engine_config, nominals=executable.builtin_nominals
    )

    kind = cache(partial(_kind, program))

    def declared(key: HostParamKey) -> bool:
        match kind(key):
            case _Engine():
                return True
            case _Argument(name):
                return name in parameters
            case _Param():
                return key in executable.param_bindings or key in executable.function_param_bindings
            case _ as unreachable:  # pragma: no cover
                assert_never(unreachable)

    config_tier: dict[HostParamKey, object] = {
        key: _Typed(value)
        for key, value in chain(valid_engine_config.items(), others.items())
        if declared(key)
    }
    tiers: dict[ParamTier, Mapping[HostParamKey, object]] = {
        ParamTier.DECLARED: lower,
        ParamTier.CONFIG: config_tier,
        ParamTier.INHERITED: {key: item.value for key, item in inherited.items()},
        ParamTier.CONFIGURED: {key: raw for key, raw in upper.items() if key not in supplied},
        ParamTier.SUPPLIED: {
            **positional_values,
            **{
                key: raw
                for key, raw in upper.items()
                if key in supplied and key not in positional_values
            },
        },
    }
    decode_engine = engine_setting_decoder()

    def decode(key: HostParamKey, tier: ParamTier, raw: object) -> "Value | Diagnostic":
        if isinstance(raw, _Typed):
            return raw.value
        address = inherited[key].address if tier is ParamTier.INHERITED else None
        match kind(key):
            case _Engine():
                return decode_engine(key, tier, raw)
            case _Argument(name):
                param = parameters.get(name)
                if param is None:
                    return unknown_argument_diagnostic(signature, name)
                if param.kind is ParamZone.POSITIONAL_ONLY and key not in positional_values:
                    return positional_only_by_name_diagnostic(param)
                return decode_program_argument(
                    param, raw, default_resolver=default_resolver, inherited_address=address
                )
            case _Param():
                return decode_host_param(
                    executable,
                    key,
                    raw,
                    default_resolver=default_resolver,
                    inherited_address=address,
                )
            case _ as unreachable:  # pragma: no cover
                assert_never(unreachable)

    decoded = resolve_host_params(tiers, decode)

    named = {
        arg.name: value
        for key, (_tier, value) in decoded.values.items()
        if isinstance(arg := kind(key), _Argument)
    }
    seeds = {
        key: value
        for key, (_tier, value) in decoded.values.items()
        if not isinstance(kind(key), _Engine)
    }
    param_seeds, function_param_seeds = split_host_param_values(seeds)

    failed = frozenset(
        arg.name for key in decoded.failed if isinstance(arg := kind(key), _Argument)
    )
    arguments, completion_diagnostics = (
        ((), ())
        if positional_diagnostics
        else complete_program_arguments(signature, named, failed=failed)
    )
    return ProgramParamResolution(
        arguments=arguments,
        param_seeds=param_seeds,
        function_param_seeds={
            key: value
            for key, value in function_param_seeds.items()
            if key in executable.function_param_bindings
        },
        engine=decoded.engine,
        diagnostics=(
            *positional_diagnostics,
            *duplicate_diagnostics,
            *config_diagnostics,
            *decoded.diagnostics,
            *completion_diagnostics,
        ),
        usage_diagnostics=(
            *positional_diagnostics,
            *duplicate_diagnostics,
            *decoded.usage_diagnostics,
            *completion_diagnostics,
        ),
    )
