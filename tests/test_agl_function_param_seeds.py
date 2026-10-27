"""Function-parameter seeds: lowered tables, preflight decoding, interpreter substitution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agnette.agl import artifact_cache
from agnette.agl.ir.nodes import UseDefault
from agnette.agl.ir.static_keys import FunctionParamKey, HostParamKey
from agnette.agl.lower import module as module_cache
from agnette.agl.pipeline import (
    ArgumentPreflight,
    ArgumentPreflightFailure,
    PipelineDriver,
    PreparedProgram,
    ProgramDiscovery,
)
from agnette.agl.semantics.values import IntValue, TextValue
from tests._agl_helpers import agl_roots

_LIBRARY = """\
def tries(@param count: int = 3, label: text = "l") -> text = "%{label}:%{count}"
"""

_ENTRY = """\
import library

def local(@param size: int = 2) -> int = size

program def main() -> unit =
  print library::tries()
  print local()
"""

_TWO_PROGRAMS = """\
program def inner(@param limit: int = 1, name: text = "n") -> unit = print limit

program def outer() -> unit = inner()
"""


_RECURSIVE = """\
program def main(@arg-std @param n: int = 1, depth: int = 0) -> unit =
  print "%{depth}:%{n}"
  if depth < 1 =>
    main(depth = depth + 1)
"""


def _runtime() -> PipelineDriver:
    return PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)


def _key(
    discovery: ProgramDiscovery,
    scope: tuple[str, ...],
    function: str,
    param: str,
    *,
    index: int = 0,
) -> FunctionParamKey:
    return FunctionParamKey(discovery.programs[index].module, scope, function, param)


def _library_key(function: str, param: str) -> FunctionParamKey:
    from agnette.agl.modules.ids import ModuleId

    return FunctionParamKey(ModuleId.from_path("library"), (), function, param)


def _setup(
    source: str, tmp_path: Path, library: str | None = _LIBRARY
) -> tuple[PipelineDriver, PreparedProgram, ProgramDiscovery]:
    if library is not None:
        (tmp_path / "library.agl").write_text(library, encoding="utf-8")
    runtime = _runtime()
    prepared = PipelineDriver.prepare_program(source, roots=agl_roots(tmp_path))
    discovery = runtime.discover_programs(prepared)
    assert discovery.compiled is not None
    return runtime, prepared, discovery


def _preflight(
    runtime: PipelineDriver,
    prepared: PreparedProgram,
    discovery: ProgramDiscovery,
    values: dict[HostParamKey, object],
    *,
    lower: dict[HostParamKey, object] | None = None,
    supplied: frozenset[HostParamKey] = frozenset(),
    index: int = 0,
) -> ArgumentPreflight | ArgumentPreflightFailure:
    return runtime.preflight_arguments(
        prepared,
        discovery.programs[index],
        compiled=discovery.compiled,
        param_values=values,
        param_values_lower=lower,
        supplied=supplied,
    )


def _run(
    runtime: PipelineDriver,
    prepared: PreparedProgram,
    discovery: ProgramDiscovery,
    preflight: ArgumentPreflight,
    *,
    index: int = 0,
    trace_file: Path | None = None,
) -> None:
    program = discovery.programs[index]
    result = runtime.run_prepared(
        prepared,
        compiled=discovery.compiled,
        executable=preflight.executable,
        program_symbol=preflight.executable.program_symbols[program.node_id],
        arguments=preflight.arguments,
        param_seeds=preflight.param_seeds,
        function_param_seeds=preflight.function_param_seeds,
        trace_file=trace_file,
    )
    assert result.ok, result.diagnostics


def _ok(outcome: ArgumentPreflight | ArgumentPreflightFailure) -> ArgumentPreflight:
    assert isinstance(outcome, ArgumentPreflight), outcome.result.diagnostics
    return outcome


class TestScopeEnumeration:
    def test_yields_each_marked_def_parameter_with_its_ast_index(self, tmp_path: Path) -> None:
        source = (
            "record Box\n  n: int\n\n"
            "def Box::grow(self, @param by: int = 1) -> int = self.n + by\n"
            "def plain(x: int = 1, @param y: int = 2) -> int = y\n"
            "scope S\n  def deep(@param z: int = 3) -> int = z\nend S\n"
            "\n"
            "def other(w: int = 1) -> int = w\n"
            "\n"
            "program def main() -> unit = ()\n"
        )
        prepared = PipelineDriver.prepare_program(source, roots=agl_roots(tmp_path))
        discovery = _runtime().discover_programs(prepared)
        assert discovery.compiled is not None
        module = discovery.programs[0].module
        resolved = discovery.compiled.checked.modules[module].resolved
        found = [
            (
                binding.scope_path,
                binding.function.name,
                binding.index,
                binding.param.name,
                binding.cli.name,
            )
            for binding in resolved.function_param_bindings()
        ]
        assert found == [
            (("Box",), "grow", 1, "by", "by"),
            ((), "plain", 1, "y", "y"),
            (("S",), "deep", 0, "z", "z"),
        ]
        assert all(
            binding.param is binding.function.params[binding.index]
            for binding in resolved.function_param_bindings()
        )


class TestLoweredTables:
    def test_executable_carries_tables_for_reachable_functions(self, tmp_path: Path) -> None:
        runtime, prepared, discovery = _setup(_ENTRY, tmp_path)
        preflight = _ok(_preflight(runtime, prepared, discovery, {}))
        executable = preflight.executable
        local = _key(discovery, (), "local", "size")
        tries = _library_key("tries", "count")
        assert set(executable.function_param_bindings) == {local, tries}
        assert set(executable.param_decoders) >= {local, tries}
        assert set(executable.param_spans) >= {local, tries}
        for key, (function_id, index) in executable.function_param_bindings.items():
            descriptor = executable.functions[function_id]
            assert index == 0 and len(descriptor.params) >= 1, key

    def test_unreachable_function_is_narrowed_away(self, tmp_path: Path) -> None:
        runtime, prepared, discovery = _setup(
            "import library\nprogram def main() -> unit = ()\n", tmp_path
        )
        preflight = _ok(_preflight(runtime, prepared, discovery, {}))
        assert preflight.executable.function_param_bindings == {}
        failure = _preflight(runtime, prepared, discovery, {_library_key("tries", "count"): 1})
        assert isinstance(failure, ArgumentPreflightFailure)

    def test_tables_survive_a_warm_lowered_module_cache(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        artifact_cache.clear_retained_artifacts()
        served: list[object] = []
        load = module_cache.load

        def spy(key: bytes) -> object:
            served.append(loaded := load(key))
            return loaded

        monkeypatch.setattr(module_cache, "load", spy)
        outputs = []
        for _ in range(2):
            runtime, prepared, discovery = _setup(_ENTRY, tmp_path)
            preflight = _ok(
                _preflight(runtime, prepared, discovery, {_library_key("tries", "count"): 6})
            )
            assert _library_key("tries", "count") in preflight.executable.function_param_bindings
            _run(runtime, prepared, discovery, preflight)
            outputs.append(capsys.readouterr().out)
        assert outputs == ["l:6\n2\n"] * 2
        assert any(loaded is not None for loaded in served)


class TestSeeding:
    def test_seeds_replace_defaults_across_modules(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime, prepared, discovery = _setup(_ENTRY, tmp_path)
        values: dict[HostParamKey, object] = {
            _library_key("tries", "count"): 6,
            _key(discovery, (), "local", "size"): 9,
        }
        preflight = _ok(_preflight(runtime, prepared, discovery, values))
        assert preflight.function_param_seeds == {
            _library_key("tries", "count"): IntValue(6),
            _key(discovery, (), "local", "size"): IntValue(9),
        }
        _run(runtime, prepared, discovery, preflight)
        assert capsys.readouterr().out == "l:6\n9\n"

    def test_tiers_rank_upper_over_lower(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime, prepared, discovery = _setup(_ENTRY, tmp_path)
        tries = _library_key("tries", "count")
        local = _key(discovery, (), "local", "size")
        preflight = _ok(
            _preflight(runtime, prepared, discovery, {tries: 5}, lower={tries: 1, local: 4})
        )
        _run(runtime, prepared, discovery, preflight)
        assert capsys.readouterr().out == "l:5\n4\n"

    def test_own_param_argument_is_bound_at_the_entry_and_seeded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime, prepared, discovery = _setup(_TWO_PROGRAMS, tmp_path, library=None)
        inner = next(p for p in discovery.programs if p.name == "inner")
        index = discovery.programs.index(inner)
        limit = inner.param_key("limit")
        name = inner.param_key("name")
        preflight = _ok(
            _preflight(runtime, prepared, discovery, {limit: 7, name: "x"}, index=index)
        )
        assert preflight.function_param_seeds == {limit: IntValue(7)}
        assert preflight.arguments == (IntValue(7), TextValue("x"))
        _run(runtime, prepared, discovery, preflight, index=index)
        assert capsys.readouterr().out == "7\n"

    def test_omitted_own_param_argument_is_not_seeded(self, tmp_path: Path) -> None:
        runtime, prepared, discovery = _setup(_TWO_PROGRAMS, tmp_path, library=None)
        index = [p.name for p in discovery.programs].index("inner")
        preflight = _ok(_preflight(runtime, prepared, discovery, {}, index=index))
        assert preflight.function_param_seeds == {}
        assert isinstance(preflight.arguments[0], UseDefault)

    def test_positional_own_param_argument_reaches_every_omitting_call(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime, prepared, discovery = _setup(_RECURSIVE, tmp_path, library=None)
        preflight = _ok(
            runtime.preflight_arguments(
                prepared, discovery.programs[0], compiled=discovery.compiled, positional=(5,)
            )
        )
        _run(runtime, prepared, discovery, preflight)
        assert capsys.readouterr().out == "0:5\n1:5\n"

    def test_named_own_param_argument_reaches_every_omitting_call(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime, prepared, discovery = _setup(_RECURSIVE, tmp_path, library=None)
        n = discovery.programs[0].param_key("n")
        preflight = _ok(_preflight(runtime, prepared, discovery, {n: 7}))
        _run(runtime, prepared, discovery, preflight)
        assert capsys.readouterr().out == "0:7\n1:7\n"

    def test_own_param_argument_given_positionally_and_by_name_is_rejected(
        self, tmp_path: Path
    ) -> None:
        runtime, prepared, discovery = _setup(_RECURSIVE, tmp_path, library=None)
        n = discovery.programs[0].param_key("n")
        failure = runtime.preflight_arguments(
            prepared,
            discovery.programs[0],
            compiled=discovery.compiled,
            positional=(5,),
            param_values={n: 7},
            supplied=frozenset({n}),
        )
        assert isinstance(failure, ArgumentPreflightFailure)
        assert failure.usage_diagnostics

    def test_param_of_a_program_reached_from_another_program_is_seeded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime, prepared, discovery = _setup(_TWO_PROGRAMS, tmp_path, library=None)
        outer = next(p for p in discovery.programs if p.name == "outer")
        index = discovery.programs.index(outer)
        preflight = _ok(
            _preflight(
                runtime,
                prepared,
                discovery,
                {_key(discovery, (), "inner", "limit"): 8},
                index=index,
            )
        )
        _run(runtime, prepared, discovery, preflight, index=index)
        assert capsys.readouterr().out == "8\n"

    def test_decode_failure_of_a_supplied_value_is_a_usage_diagnostic(self, tmp_path: Path) -> None:
        runtime, prepared, discovery = _setup(_ENTRY, tmp_path)
        local = _key(discovery, (), "local", "size")
        supplied = _preflight(
            runtime, prepared, discovery, {local: "x"}, supplied=frozenset({local})
        )
        assert isinstance(supplied, ArgumentPreflightFailure)
        assert supplied.usage_diagnostics
        configured = _preflight(runtime, prepared, discovery, {local: "x"})
        assert isinstance(configured, ArgumentPreflightFailure)
        assert not configured.usage_diagnostics
        assert configured.result.diagnostics

    def test_unknown_function_parameter_is_rejected(self, tmp_path: Path) -> None:
        runtime, prepared, discovery = _setup(_ENTRY, tmp_path)
        failure = _preflight(
            runtime, prepared, discovery, {_key(discovery, (), "local", "nope"): 1}
        )
        assert isinstance(failure, ArgumentPreflightFailure)


class TestTracing:
    def test_run_start_records_function_seeds(self, tmp_path: Path) -> None:
        runtime, prepared, discovery = _setup(_ENTRY, tmp_path)
        preflight = _ok(
            _preflight(runtime, prepared, discovery, {_library_key("tries", "count"): 6})
        )
        trace_path = tmp_path / "trace.jsonl"
        _run(runtime, prepared, discovery, preflight, trace_file=trace_path)
        records = [json.loads(line) for line in trace_path.read_text().splitlines()]
        start = next(record for record in records if record["kind"] == "run_start")
        assert start["parameters"] == {"library::tries::count": 6}

    def test_own_param_argument_is_traced_as_an_argument_only(self, tmp_path: Path) -> None:
        runtime, prepared, discovery = _setup(_RECURSIVE, tmp_path, library=None)
        n = discovery.programs[0].param_key("n")
        preflight = _ok(_preflight(runtime, prepared, discovery, {n: 7}))
        trace_path = tmp_path / "trace.jsonl"
        _run(runtime, prepared, discovery, preflight, trace_file=trace_path)
        records = [json.loads(line) for line in trace_path.read_text().splitlines()]
        start = next(record for record in records if record["kind"] == "run_start")
        assert start["arguments"] == {"n": 7}
        assert start["parameters"] == {}
