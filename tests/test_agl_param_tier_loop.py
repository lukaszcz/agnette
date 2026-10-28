"""One precedence rule for every host parameter kind in ``PipelineDriver.preflight_arguments``.

Module, function and engine-setting parameters and a program's own arguments are ranked
by ``ParamTier`` in one loop: the higher tier of any adjacent pair wins, and a failure
is a usage error exactly when the command line or environment supplied the winner.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agnette.agl.ir.static_keys import FunctionParamKey, HostParamKey, static_binding_key
from agnette.agl.modules.ids import STD_CONFIG_ID
from agnette.agl.pipeline import (
    ArgumentPreflight,
    ArgumentPreflightFailure,
    PipelineDriver,
    PreparedProgram,
    ProgramDiscovery,
)
from agnette.agl.runtime.arguments import InheritedParamValue, ParamTier
from agnette.agl.runtime.types import HostParamKind, ProgramDeclInfo
from agnette.agl.semantics.values import IntValue
from tests._agl_helpers import agl_roots

_ENGINE_SETTING = "parse-error-retries"


@dataclass(frozen=True)
class _Kind:
    """One kind of host parameter holding an `int`: its source, key and how to read the result."""

    name: str
    source: str
    config_entry: str
    key: Callable[[ProgramDeclInfo, ProgramDiscovery], HostParamKey]
    read: Callable[[ArgumentPreflight, HostParamKey], object]
    #: Whether a failure of its value is a diagnostic with no source location.
    sourceless: bool = False

    def program(self, config: int | None) -> str:
        attribute = "" if config is None else f"@config({self.config_entry} = {config})\n"
        return self.source.replace("{config}", attribute)


def _module_key(program: ProgramDeclInfo, discovery: ProgramDiscovery) -> HostParamKey:
    (param,) = (p for p in discovery.params_for(program) if p.kind is HostParamKind.MODULE)
    return param.key


def _function_key(program: ProgramDeclInfo, discovery: ProgramDiscovery) -> HostParamKey:
    return FunctionParamKey(program.module, (), "refine", "steps")


def _engine_key(program: ProgramDeclInfo, discovery: ProgramDiscovery) -> HostParamKey:
    return static_binding_key(STD_CONFIG_ID, (), _ENGINE_SETTING)


def _argument_key(program: ProgramDeclInfo, discovery: ProgramDiscovery) -> HostParamKey:
    return program.param_key("count")


def _read_module(preflight: ArgumentPreflight, key: HostParamKey) -> object:
    assert not isinstance(key, FunctionParamKey)
    return preflight.param_seeds[key]


def _read_function(preflight: ArgumentPreflight, key: HostParamKey) -> object:
    assert isinstance(key, FunctionParamKey)
    return preflight.function_param_seeds[key]


def _read_engine(preflight: ArgumentPreflight, key: HostParamKey) -> object:
    return preflight.engine.values[_ENGINE_SETTING]


def _read_argument(preflight: ArgumentPreflight, key: HostParamKey) -> object:
    (value,) = preflight.arguments
    return value


_KINDS = [
    _Kind(
        "module",
        "@param let count: int = 1\n\n{config}program def main() -> unit =\n  let _ = count\n",
        "count",
        _module_key,
        _read_module,
    ),
    _Kind(
        "function",
        "def refine(@param steps: int = 1) -> int = steps\n\n"
        "{config}program def main() -> unit =\n  let _ = refine()\n",
        "refine::steps",
        _function_key,
        _read_function,
    ),
    _Kind(
        "engine",
        "import std/config\n\n{config}program def main() -> unit = ()\n",
        f"config::{_ENGINE_SETTING}",
        _engine_key,
        _read_engine,
        sourceless=True,
    ),
    _Kind(
        "argument",
        "{config}program def main(count: int = 1) -> unit = ()\n",
        "main::count",
        _argument_key,
        _read_argument,
    ),
]

_TIERS = list(ParamTier)
_ADJACENT_PAIRS = list(zip(_TIERS, _TIERS[1:], strict=False))
_KIND_IDS = [kind.name for kind in _KINDS]
_PAIR_IDS = [f"{low.name.lower()}-{high.name.lower()}" for low, high in _ADJACENT_PAIRS]


def _prepare(
    source: str, tmp_path: Path
) -> tuple[PipelineDriver, PreparedProgram, ProgramDiscovery]:
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    prepared = PipelineDriver.prepare_program(source, roots=agl_roots(tmp_path))
    discovery = runtime.discover_programs(prepared)
    assert discovery.diagnostics == ()
    return runtime, prepared, discovery


def _preflight(
    kind: _Kind, tmp_path: Path, values: dict[ParamTier, object]
) -> tuple[ArgumentPreflight | ArgumentPreflightFailure, HostParamKey]:
    """Preflight *kind*'s program with each tier of *values* (`@config` as an attribute).

    Returns the outcome and the key of the parameter.
    """
    config = values.get(ParamTier.CONFIG)
    assert config is None or isinstance(config, int)
    runtime, prepared, discovery = _prepare(kind.program(config), tmp_path)
    program = discovery.programs[0]
    key = kind.key(program, discovery)
    upper: dict[HostParamKey, object] = {}
    supplied: frozenset[HostParamKey] = frozenset()
    if ParamTier.CONFIGURED in values:
        upper[key] = values[ParamTier.CONFIGURED]
    if ParamTier.SUPPLIED in values:
        upper[key] = values[ParamTier.SUPPLIED]
        supplied = frozenset({key})
    outcome = runtime.preflight_arguments(
        prepared,
        program,
        compiled=discovery.compiled,
        param_values=upper,
        param_values_lower=(
            {key: values[ParamTier.DECLARED]} if ParamTier.DECLARED in values else {}
        ),
        param_values_inherited=(
            {key: InheritedParamValue(values[ParamTier.INHERITED], "a.b")}
            if ParamTier.INHERITED in values
            else {}
        ),
        supplied=supplied,
    )
    return outcome, key


def _resolved(kind: _Kind, tmp_path: Path, values: dict[ParamTier, object]) -> object:
    """Return the value *kind*'s parameter resolves to."""
    outcome, key = _preflight(kind, tmp_path, values)
    assert isinstance(outcome, ArgumentPreflight), outcome.result.diagnostics
    return kind.read(outcome, key)


def _failure(
    kind: _Kind, tmp_path: Path, values: dict[ParamTier, object]
) -> ArgumentPreflightFailure:
    outcome, _ = _preflight(kind, tmp_path, values)
    assert isinstance(outcome, ArgumentPreflightFailure)
    return outcome


@pytest.mark.parametrize("kind", _KINDS, ids=_KIND_IDS)
@pytest.mark.parametrize(("low", "high"), _ADJACENT_PAIRS, ids=_PAIR_IDS)
def test_the_higher_of_two_adjacent_tiers_wins_for_every_kind(
    kind: _Kind, low: ParamTier, high: ParamTier, tmp_path: Path
) -> None:
    assert _resolved(kind, tmp_path, {low: 7}) == IntValue(7)
    assert _resolved(kind, tmp_path, {low: 7, high: 9}) == IntValue(9)


def test_an_engine_setting_records_its_winning_tier(tmp_path: Path) -> None:
    engine = next(kind for kind in _KINDS if kind.sourceless)
    outcome, _ = _preflight(engine, tmp_path, {ParamTier.CONFIG: 1, ParamTier.CONFIGURED: 2})
    assert isinstance(outcome, ArgumentPreflight)
    assert outcome.engine.ranks == {_ENGINE_SETTING: ParamTier.CONFIGURED}


@pytest.mark.parametrize("kind", _KINDS, ids=_KIND_IDS)
class TestFailures:
    def test_an_invalid_supplied_value_is_a_usage_diagnostic(
        self, kind: _Kind, tmp_path: Path
    ) -> None:
        failure = _failure(kind, tmp_path, {ParamTier.SUPPLIED: "nope"})

        (diagnostic,) = failure.result.diagnostics
        assert failure.usage_diagnostics == (diagnostic,)
        assert (diagnostic.line is None) is kind.sourceless

    @pytest.mark.parametrize(
        "tier", [ParamTier.DECLARED, ParamTier.INHERITED, ParamTier.CONFIGURED]
    )
    def test_an_invalid_configured_value_is_no_usage_diagnostic(
        self, kind: _Kind, tier: ParamTier, tmp_path: Path
    ) -> None:
        failure = _failure(kind, tmp_path, {tier: "nope"})

        (diagnostic,) = failure.result.diagnostics
        assert not failure.usage_diagnostics
        assert (diagnostic.line is None) is kind.sourceless

    def test_an_overridden_invalid_value_is_never_reported(
        self, kind: _Kind, tmp_path: Path
    ) -> None:
        assert _resolved(
            kind, tmp_path, {ParamTier.DECLARED: "nope", ParamTier.SUPPLIED: 3}
        ) == IntValue(3)


def test_an_invalid_engine_config_value_is_reported_even_when_overridden(tmp_path: Path) -> None:
    engine = next(kind for kind in _KINDS if kind.sourceless)

    failure = _failure(engine, tmp_path, {ParamTier.CONFIG: -1, ParamTier.SUPPLIED: 2})

    (diagnostic,) = failure.result.diagnostics
    assert diagnostic.line is None
    assert not failure.usage_diagnostics


def _line_of(source: str, fragment: str) -> int:
    """Return the 1-based line of the first line of *source* containing *fragment*."""
    return next(n for n, line in enumerate(source.splitlines(), 1) if fragment in line)


class TestProgramArguments:
    _TWO = "{config}program def main(\n  @arg-std first: int,\n  second: int = 5,\n) -> unit = ()\n"

    def _preflight(
        self,
        tmp_path: Path,
        *,
        source: str = _TWO,
        config: str = "",
        positional: tuple[object, ...] = (),
        named: dict[str, object] | None = None,
        supplied: bool = False,
    ) -> tuple[ArgumentPreflight | ArgumentPreflightFailure, ProgramDeclInfo]:
        """Preflight *source*'s program with *named* values, all supplied when *supplied*."""
        runtime, prepared, discovery = _prepare(source.replace("{config}", config), tmp_path)
        program = discovery.programs[0]
        keys = {program.param_key(n): v for n, v in (named or {}).items()}
        outcome = runtime.preflight_arguments(
            prepared,
            program,
            positional=positional,
            compiled=discovery.compiled,
            param_values=keys,
            supplied=frozenset(keys) if supplied else frozenset(),
        )
        return outcome, program

    def _failure(
        self, tmp_path: Path, **kwargs: Any
    ) -> tuple[ArgumentPreflightFailure, ProgramDeclInfo]:
        outcome, program = self._preflight(tmp_path, **kwargs)
        assert isinstance(outcome, ArgumentPreflightFailure)
        return outcome, program

    def test_a_missing_required_argument_is_reported_when_config_supplies_the_others(
        self, tmp_path: Path
    ) -> None:
        failure, program = self._failure(tmp_path, config="@config(main::second = 2)\n")

        (diagnostic,) = failure.result.diagnostics
        assert diagnostic.line == program.parameters[0].span.start_line
        assert failure.usage_diagnostics == (diagnostic,)

    def test_config_satisfies_a_required_argument(self, tmp_path: Path) -> None:
        outcome, _ = self._preflight(tmp_path, config="@config(main::first = 2)\n")

        assert isinstance(outcome, ArgumentPreflight)
        assert outcome.arguments[0] == IntValue(2)

    def test_a_positional_argument_outranks_config_without_a_duplicate(
        self, tmp_path: Path
    ) -> None:
        outcome, _ = self._preflight(tmp_path, config="@config(main::first = 2)\n", positional=(8,))

        assert isinstance(outcome, ArgumentPreflight)
        assert outcome.arguments[0] == IntValue(8)

    def test_an_argument_given_positionally_and_by_name_is_a_usage_diagnostic_at_it(
        self, tmp_path: Path
    ) -> None:
        failure, program = self._failure(tmp_path, positional=(1,), named={"first": 2})

        (diagnostic,) = failure.result.diagnostics
        assert diagnostic.line == program.parameters[0].span.start_line
        assert failure.usage_diagnostics == (diagnostic,)

    @pytest.mark.parametrize("supplied", [True, False])
    def test_an_undeclared_argument_is_a_usage_diagnostic_only_when_supplied(
        self, supplied: bool, tmp_path: Path
    ) -> None:
        failure, program = self._failure(
            tmp_path, positional=(1,), named={"bogus": 2}, supplied=supplied
        )

        (diagnostic,) = failure.result.diagnostics
        assert diagnostic.line == program.span.start_line
        assert bool(failure.usage_diagnostics) is supplied

    def test_a_decode_failure_is_not_reported_missing_as_well(self, tmp_path: Path) -> None:
        failure, program = self._failure(tmp_path, named={"first": "nope"}, supplied=True)

        (diagnostic,) = failure.result.diagnostics
        assert diagnostic.line == program.parameters[0].span.start_line
        assert failure.usage_diagnostics == (diagnostic,)

    def test_too_many_positional_arguments_are_a_usage_diagnostic_at_the_program(
        self, tmp_path: Path
    ) -> None:
        failure, program = self._failure(tmp_path, positional=(1, 2, 3))

        (diagnostic,) = failure.result.diagnostics
        assert diagnostic.line == program.span.start_line
        assert failure.usage_diagnostics == (diagnostic,)

    @pytest.mark.parametrize("supplied", [True, False])
    def test_a_positional_only_argument_given_by_name_is_a_usage_diagnostic_only_when_supplied(
        self, supplied: bool, tmp_path: Path
    ) -> None:
        failure, program = self._failure(
            tmp_path,
            source="program def main(\n  @arg-pos first: int,\n) -> unit = ()\n",
            named={"first": 1},
            supplied=supplied,
        )

        (diagnostic,) = failure.result.diagnostics
        assert diagnostic.line == program.parameters[0].span.start_line
        assert bool(failure.usage_diagnostics) is supplied

    def test_diagnostics_follow_the_winning_tier_then_input_order(self, tmp_path: Path) -> None:
        source = (
            "import std/config\n\n"
            "@param let count: int = 1\n\n"
            "program def main(\n  @arg-std n: int,\n  scale: int = 1,\n) -> unit =\n"
            "  let _ = count\n"
        )
        runtime, prepared, discovery = _prepare(source, tmp_path)
        program = discovery.programs[0]
        (count,) = (p for p in discovery.params_for(program) if p.kind is HostParamKind.MODULE)
        retries = static_binding_key(STD_CONFIG_ID, (), _ENGINE_SETTING)
        # The configured module value comes first in the input, the supplied ones after it.
        upper: dict[HostParamKey, object] = {
            count.key: "bad",
            program.param_key("scale"): "bad",
            retries: "bad",
        }

        failure = runtime.preflight_arguments(
            prepared,
            program,
            positional=("bad",),
            compiled=discovery.compiled,
            param_values=upper,
            supplied=frozenset({program.param_key("scale"), retries}),
        )

        assert isinstance(failure, ArgumentPreflightFailure)
        expected = [
            _line_of(source, "n: int"),
            _line_of(source, "scale: int"),
            None,
            _line_of(source, "@param let count"),
        ]
        assert [d.line for d in failure.result.diagnostics] == expected
        assert [d.line for d in failure.usage_diagnostics] == expected[:3]
