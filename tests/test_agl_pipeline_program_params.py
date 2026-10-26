"""Static module-parameter inventory exposed by program discovery."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agnette.agl.ir.static_keys import StaticBindingKey
from agnette.agl.modules.ids import ModuleId
from agnette.agl.modules.roots import RootSet
from agnette.agl.pipeline import (
    ArgumentPreflight,
    ArgumentPreflightFailure,
    PipelineDriver,
    PreparedProgram,
    ProgramDiscovery,
)
from agnette.agl.runtime.arguments import ProgramArguments
from tests._agl_helpers import REPO_STDLIB_ROOT, agl_roots


def _discover(source: str, tmp_path: Path) -> ProgramDiscovery:
    """Discover source with modules rooted at *tmp_path*."""
    prepared = PipelineDriver.prepare_program(
        source,
        roots=RootSet(roots=frozenset({tmp_path})),
        default_stdlib=False,
    )
    discovery = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).discover_programs(
        prepared
    )
    assert discovery.diagnostics == ()
    return discovery


def _prepared(source: str, tmp_path: Path) -> PreparedProgram:
    """Prepare source with modules rooted at *tmp_path*."""
    return PipelineDriver.prepare_program(
        source,
        roots=RootSet(roots=frozenset({tmp_path})),
        default_stdlib=False,
    )


def _preflight(
    runtime: PipelineDriver,
    prepared: PreparedProgram,
    discovery: ProgramDiscovery,
    *,
    param_values: dict[StaticBindingKey, object] | None = None,
    param_values_lower: dict[StaticBindingKey, object] | None = None,
) -> ArgumentPreflight:
    """Preflight the first discovered program with no value arguments."""
    return runtime.preflight_arguments(
        prepared,
        discovery.programs[0],
        ProgramArguments(positional=(), named={}),
        compiled=discovery.compiled,
        param_values=param_values,
        param_values_lower=param_values_lower,
    )


def test_discovery_inventories_module_params_in_declaration_order(tmp_path: Path) -> None:
    discovery = _discover(
        """
@param @opt-name("loud") @doc("Root verbosity") let verbose: bool = false
@param var retries: int = 3

scope Logging
  @param @doc("Trace logging") let trace: bool = false
  @param var level: int = 1
end Logging

program def main() -> unit = ()
""",
        tmp_path,
    )

    params = discovery.module_params[discovery.programs[0].module]

    assert [(param.name, param.cli.name, param.scope_path) for param in params] == [
        ("verbose", "loud", ()),
        ("retries", "retries", ()),
        ("trace", "trace", ("Logging",)),
        ("level", "level", ("Logging",)),
    ]
    assert params[0].cli.doc == "Root verbosity"
    assert params[2].cli.doc == "Trace logging"
    assert params[0].declaration_path == "<entry>::verbose"
    assert params[2].declaration_path == "<entry>::Logging::trace"
    assert params[0].key == (discovery.programs[0].module, (), "verbose")


def test_discovery_closure_and_params_follow_source_reachability_order(tmp_path: Path) -> None:
    (tmp_path / "first.agl").write_text("@param let first: int = 1\n", encoding="utf-8")
    (tmp_path / "second.agl").write_text(
        "import third\n@param let second: int = 2\ndef total() -> int = second + third::third\n",
        encoding="utf-8",
    )
    (tmp_path / "third.agl").write_text("@param let third: int = 3\n", encoding="utf-8")

    discovery = _discover(
        """
import first
import second
@param let root: int = 0
program def main() -> unit =
  let _ = root
  let _ = first::first
  let _ = second::total()
""",
        tmp_path,
    )
    program = discovery.programs[0]

    assert program.closure == (
        program.module,
        ModuleId(("first",)),
        ModuleId(("second",)),
        ModuleId(("third",)),
    )
    assert [param.name for param in discovery.params_for(program)] == [
        "root",
        "first",
        "second",
        "third",
    ]


def test_sibling_programs_share_one_module_closure(tmp_path: Path) -> None:
    """Programs of one module report the closure their module was walked for once."""
    (tmp_path / "library.agl").write_text("@param let library: int = 1\n", encoding="utf-8")

    discovery = _discover(
        "import library\nprogram def one() -> unit = ()\nprogram def two() -> unit = ()\n",
        tmp_path,
    )
    one, two = discovery.programs

    assert one.closure == (one.module, ModuleId(("library",)))
    assert one.closure is two.closure


def test_discovery_marks_params_annotated_through_an_imported_path_alias(tmp_path: Path) -> None:
    (tmp_path / "shared.agl").write_text("type location = path\n", encoding="utf-8")

    discovery = _discover(
        "import shared\n"
        '@param let out: shared::location = "/tmp"\n'
        'program def main(target: shared::location = "/tmp") -> unit = ()\n',
        tmp_path,
    )
    program = discovery.programs[0]

    assert [param.is_path for param in discovery.module_params[program.module]] == [True]
    assert [param.is_path for param in program.parameters] == [True]


def test_imported_program_uses_its_own_source_reachable_closure(tmp_path: Path) -> None:
    (tmp_path / "library.agl").write_text(
        "import dependency\n@param let library: int = 1\n"
        "program def run() -> unit =\n  let _ = library\n  let _ = dependency::dependency\n",
        encoding="utf-8",
    )
    (tmp_path / "dependency.agl").write_text("@param let dependency: int = 2\n", encoding="utf-8")

    discovery = _discover(
        "import library\n@param let entry: int = 0\nprogram def main() -> unit = ()\n",
        tmp_path,
    )
    imported_program = next(program for program in discovery.programs if program.name == "run")

    assert imported_program.closure == (ModuleId(("library",)), ModuleId(("dependency",)))
    assert [param.name for param in discovery.params_for(imported_program)] == [
        "library",
        "dependency",
    ]
    assert discovery.module_params[ModuleId(("library",))][0].declaration_path == "library::library"


def test_discovery_has_empty_module_param_inventory_without_param_bindings(tmp_path: Path) -> None:
    (tmp_path / "library.agl").write_text("let value: int = 1\n", encoding="utf-8")

    discovery = _discover(
        "import library\nprogram def main() -> unit = ()\n",
        tmp_path,
    )

    assert all(not params for params in discovery.module_params.values())
    assert tuple(discovery.params_for(discovery.programs[0])) == ()


def _reachable_names(discovery: ProgramDiscovery) -> list[str]:
    return [param.name for param in discovery.params_for(discovery.programs[0])]


def test_a_param_of_an_imported_but_unreferenced_module_is_not_reachable(tmp_path: Path) -> None:
    (tmp_path / "used.agl").write_text("@param let used: int = 1\n", encoding="utf-8")
    (tmp_path / "unused.agl").write_text("@param let unused: int = 2\n", encoding="utf-8")
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    prepared = _prepared(
        "import used\nimport unused\nprogram def main() -> unit =\n  let _ = used::used\n", tmp_path
    )
    discovery = runtime.discover_programs(prepared)
    program = discovery.programs[0]

    assert _reachable_names(discovery) == ["used"]
    assert [param.name for param in discovery.declared_params_for(program)] == ["used", "unused"]
    preflight = _preflight(runtime, prepared, discovery)
    assert set(preflight.executable.param_bindings) == {
        param.key for param in discovery.params_for(program)
    }


def test_a_seed_for_a_declared_but_unreachable_param_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "unused.agl").write_text("@param let unused: int = 2\n", encoding="utf-8")
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    prepared = _prepared("import unused\nprogram def main() -> unit = ()\n", tmp_path)
    discovery = runtime.discover_programs(prepared)
    (unused,) = discovery.declared_params_for(discovery.programs[0])

    preflight = _preflight(runtime, prepared, discovery, param_values={unused.key: 5})

    assert isinstance(preflight, ArgumentPreflightFailure)


@pytest.mark.parametrize(
    ("entry", "library"),
    [
        pytest.param(
            "import library::*\nprogram def main() -> unit =\n  let _ = Point(x = 1).bumped()\n",
            "record Point\n  x: int\n\ndef Point::bumped(self) -> int = self.x + knob\n",
            id="method-only",
        ),
        pytest.param(
            "import library::*\nprogram def main() -> unit =\n  let f = read\n  let _ = f()\n",
            "def read() -> int = knob\n",
            id="function-value",
        ),
        pytest.param(
            "import library::*\nprogram def main() -> unit =\n  let _ = read()\n",
            "def read(n: int = knob) -> int = n\n",
            id="function-default",
        ),
        pytest.param(
            "import library\nprogram def main(n: int = library::knob) -> unit = ()\n",
            "",
            id="program-default",
        ),
        pytest.param(
            "import library\nprogram def main() -> unit =\n  let _ = library::derived\n",
            "let derived: int = knob\n",
            id="static-binding-initializer",
        ),
        pytest.param(
            "import library::*\nprogram def main() -> unit =\n  let _ = apply(read)\n",
            "def read() -> int = knob\ndef apply(f: () -> int) -> int = f()\n",
            id="function-value-argument",
        ),
    ],
)
def test_param_reached_through_a_reference_form_is_reachable(
    tmp_path: Path, entry: str, library: str
) -> None:
    (tmp_path / "library.agl").write_text(
        f"@param let knob: int = 1\n@param let other: int = 2\n{library}", encoding="utf-8"
    )

    discovery = _discover(entry, tmp_path)

    assert _reachable_names(discovery) == ["knob"]


def test_a_param_reached_through_the_prelude_is_reachable(tmp_path: Path) -> None:
    stdlib = tmp_path / "stdlib"
    (stdlib / "src").mkdir(parents=True)
    (stdlib / "src" / "prelude.agl").write_text("export std/knob\n", encoding="utf-8")
    (stdlib / "src" / "knob.agl").write_text(
        "@param let level: int = 1\ndef tuned() -> int = level\n", encoding="utf-8"
    )
    prepared = PipelineDriver.prepare_program(
        "program def main() -> unit =\n  let _ = tuned()\n",
        roots=RootSet(roots=frozenset(), stdlib_roots=frozenset({stdlib})),
    )

    discovery = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).discover_programs(
        prepared
    )

    assert discovery.diagnostics == ()
    assert _reachable_names(discovery) == ["level"]


def test_a_param_assigned_but_never_read_is_reachable(tmp_path: Path) -> None:
    (tmp_path / "library.agl").write_text(
        "@param var v: int = 1\n@param var unused: int = 2\n", encoding="utf-8"
    )

    discovery = _discover(
        "import library\nprogram def main() -> unit =\n  library::v := 3\n", tmp_path
    )

    assert _reachable_names(discovery) == ["v"]


def test_a_param_read_by_a_builtin_parameter_default_is_reachable(tmp_path: Path) -> None:
    """A host-backed declaration's parameter defaults are AgL expressions too."""
    stdlib = tmp_path / "stdlib"
    shutil.copytree(REPO_STDLIB_ROOT, stdlib)
    agent = stdlib / "src" / "agent.agl"
    original = agent.read_text(encoding="utf-8")
    patched = original.replace(
        "builtin def ask[T](\n  prompt: text,\n  agent: Agent = std/config::default-agent,\n"
        '  format: text = "",\n  strict-json: bool = false,\n'
        "  parse-error-retries: int = std/config::parse-error-retries,",
        "builtin def ask[T](\n  prompt: text,\n  agent: Agent = std/config::default-agent,\n"
        '  format: text = "",\n  strict-json: bool = false,\n'
        "  parse-error-retries: int = probe-retries,",
    )
    assert patched != original
    agent.write_text(f"{patched}\n@param let probe-retries: int = 1\n", encoding="utf-8")
    prepared = PipelineDriver.prepare_program(
        'program def main() -> unit =\n  let _: text = ask("hi")\n',
        roots=RootSet(roots=frozenset(), stdlib_roots=frozenset({stdlib})),
    )

    discovery = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).discover_programs(
        prepared
    )

    assert discovery.diagnostics == ()
    assert "probe-retries" in _reachable_names(discovery)


def test_each_program_has_its_own_reachable_params(tmp_path: Path) -> None:
    (tmp_path / "library.agl").write_text(
        "@param let one: int = 1\n@param let two: int = 2\n", encoding="utf-8"
    )

    discovery = _discover(
        "import library\n"
        "program def a() -> unit =\n  let _ = library::one\n"
        "program def b() -> unit =\n  let _ = library::two\n",
        tmp_path,
    )
    first, second = discovery.programs

    assert [p.name for p in discovery.params_for(first)] == ["one"]
    assert [p.name for p in discovery.params_for(second)] == ["two"]
    assert [p.name for p in discovery.declared_params_for(first)] == ["one", "two"]


class TestParamSeeds:
    def test_executable_exposes_a_binding_and_decoder_for_every_inventory_key(
        self, tmp_path: Path
    ) -> None:
        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = _prepared(
            """
@param let root: int = 1
scope Tuning
  @param var retries: int = 2
end Tuning

program def main() -> unit =
  let _ = root
  let _ = Tuning::retries
""",
            tmp_path,
        )
        discovery = runtime.discover_programs(prepared)

        preflight = _preflight(runtime, prepared, discovery)

        assert preflight.result.ok
        assert preflight.executable is not None
        expected_keys = {param.key for param in discovery.params_for(discovery.programs[0])}
        assert set(preflight.executable.param_bindings) == expected_keys
        assert set(preflight.executable.param_decoders) == expected_keys
        assert set(preflight.executable.param_bindings.values()) <= set(
            preflight.executable.symbols
        )

    def test_preflight_decodes_all_param_values_and_reports_all_failures(
        self, tmp_path: Path
    ) -> None:
        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = _prepared(
            """
@param let number: int = 1
@param let enabled: bool = false
program def main() -> unit =
  let _ = number
  let _ = enabled
""",
            tmp_path,
        )
        discovery = runtime.discover_programs(prepared)
        number, enabled = discovery.params_for(discovery.programs[0])
        unknown = (ModuleId(("missing",)), (), "unknown")

        preflight = _preflight(
            runtime,
            prepared,
            discovery,
            param_values={number.key: "not-an-int", enabled.key: "not-a-bool", unknown: 1},
        )

        assert isinstance(preflight, ArgumentPreflightFailure)
        assert len(preflight.result.diagnostics) == 3

    def test_imported_param_decode_failure_points_to_its_declaration(self, tmp_path: Path) -> None:
        library = tmp_path / "library.agl"
        library.write_text("@param let retries: int = 1\n", encoding="utf-8")
        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = _prepared(
            "import library\nprogram def main() -> unit =\n  let _ = library::retries\n",
            tmp_path,
        )
        discovery = runtime.discover_programs(prepared)
        retries = discovery.params_for(discovery.programs[0])[0]

        preflight = _preflight(
            runtime, prepared, discovery, param_values={retries.key: "not-an-int"}
        )

        assert not preflight.result.ok
        assert preflight.result.diagnostics[0].source_label == str(library)

    def test_seeds_override_defaults_and_keep_vars_writable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "library.agl").write_text(
            """
@param let imported: int = 1
""",
            encoding="utf-8",
        )
        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = PipelineDriver.prepare_program(
            """
import library
@param let root: int = 2
@param var count: int = 3

scope Tuning
  @param let scoped: int = 4
end Tuning

program def main() -> unit =
  count := count + 1
  print library::imported
  print root
  print count
  print Tuning::scoped
""",
            roots=agl_roots(tmp_path),
        )
        discovery = runtime.discover_programs(prepared)
        params = {param.name: param for param in discovery.params_for(discovery.programs[0])}

        preflight = _preflight(
            runtime,
            prepared,
            discovery,
            param_values={
                params["imported"].key: 10,
                params["root"].key: 20,
                params["count"].key: 30,
                params["scoped"].key: 40,
            },
        )

        assert preflight.result.ok
        assert preflight.executable is not None
        assert set(preflight.param_seeds) == {param.key for param in params.values()}
        result = runtime.run_prepared(
            prepared,
            compiled=discovery.compiled,
            executable=preflight.executable,
            program_symbol=preflight.executable.program_symbols[discovery.programs[0].node_id],
            arguments=preflight.arguments,
            param_seeds=preflight.param_seeds,
        )

        assert result.ok
        assert capsys.readouterr().out == "10\n20\n31\n40\n"

    def test_unseeded_param_uses_its_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = PipelineDriver.prepare_program(
            """
@param let value: int = 7
program def main() -> unit = print value
""",
            roots=agl_roots(tmp_path),
        )
        discovery = runtime.discover_programs(prepared)

        result = runtime.run_prepared(
            prepared, compiled=discovery.compiled, select_default_program=True
        )

        assert result.ok
        assert capsys.readouterr().out == "7\n"

    def test_check_only_never_binds_preflighted_seeds(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = PipelineDriver.prepare_program(
            """
@param let value: int = 7
program def main() -> unit = print value
""",
            roots=agl_roots(tmp_path),
        )
        discovery = runtime.discover_programs(prepared)
        value = discovery.params_for(discovery.programs[0])[0]

        preflight = _preflight(
            runtime,
            prepared,
            discovery,
            param_values={value.key: 9},
        )
        assert preflight.result.ok
        assert preflight.executable is not None

        run_result = runtime.run_prepared(
            prepared,
            check_only=True,
            compiled=discovery.compiled,
            executable=preflight.executable,
            param_seeds=preflight.param_seeds,
        )
        check_result = runtime.check_prepared(prepared)

        assert run_result.ok
        assert run_result.bindings == {}
        assert check_result.ok
        assert check_result.bindings == {}
        assert capsys.readouterr().out == ""


class TestParamValueTierMerge:
    """``preflight_arguments`` merges two raw tiers (``param_values`` = supplied/
    program-route, ``param_values_lower`` = module-route) beneath the selected
    program's own ``@config`` param values: upper > @config > lower."""

    def test_lower_is_used_when_upper_and_config_are_silent(self, tmp_path: Path) -> None:
        from agnette.agl.semantics.values import IntValue

        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = _prepared(
            "@param let count: int = 1\nprogram def main() -> unit =\n  let _ = count\n", tmp_path
        )
        discovery = runtime.discover_programs(prepared)
        (count,) = discovery.params_for(discovery.programs[0])

        preflight = _preflight(runtime, prepared, discovery, param_values_lower={count.key: 5})

        assert preflight.result.ok
        assert preflight.param_seeds == {count.key: IntValue(5)}

    def test_upper_overrides_lower(self, tmp_path: Path) -> None:
        from agnette.agl.semantics.values import IntValue

        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = _prepared(
            "@param let count: int = 1\nprogram def main() -> unit =\n  let _ = count\n", tmp_path
        )
        discovery = runtime.discover_programs(prepared)
        (count,) = discovery.params_for(discovery.programs[0])

        preflight = _preflight(
            runtime,
            prepared,
            discovery,
            param_values={count.key: 7},
            param_values_lower={count.key: 5},
        )

        assert preflight.result.ok
        assert preflight.param_seeds == {count.key: IntValue(7)}

    def test_config_overrides_lower(self, tmp_path: Path) -> None:
        from agnette.agl.semantics.values import IntValue

        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = _prepared(
            "@param let count: int = 1\n\n@config(count = 2)\n"
            "program def main() -> unit =\n  let _ = count\n",
            tmp_path,
        )
        discovery = runtime.discover_programs(prepared)
        (count,) = discovery.params_for(discovery.programs[0])

        preflight = _preflight(runtime, prepared, discovery, param_values_lower={count.key: 5})

        assert preflight.result.ok
        assert preflight.param_seeds == {count.key: IntValue(2)}

    def test_upper_overrides_config(self, tmp_path: Path) -> None:
        from agnette.agl.semantics.values import IntValue

        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = _prepared(
            "@param let count: int = 1\n\n@config(count = 2)\n"
            "program def main() -> unit =\n  let _ = count\n",
            tmp_path,
        )
        discovery = runtime.discover_programs(prepared)
        (count,) = discovery.params_for(discovery.programs[0])

        preflight = _preflight(runtime, prepared, discovery, param_values={count.key: 7})

        assert preflight.result.ok
        assert preflight.param_seeds == {count.key: IntValue(7)}

    def test_a_lower_value_overridden_by_config_is_never_decoded(self, tmp_path: Path) -> None:
        """An invalid module-route raw value that ``@config`` overrides must not
        surface a decode diagnostic — exactly as a program-route override today."""
        from agnette.agl.semantics.values import IntValue

        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = _prepared(
            "@param let count: int = 1\n\n@config(count = 2)\n"
            "program def main() -> unit =\n  let _ = count\n",
            tmp_path,
        )
        discovery = runtime.discover_programs(prepared)
        (count,) = discovery.params_for(discovery.programs[0])

        preflight = _preflight(
            runtime, prepared, discovery, param_values_lower={count.key: "not-an-int"}
        )

        assert preflight.result.ok
        assert preflight.param_seeds == {count.key: IntValue(2)}

    def test_a_lower_value_overridden_by_upper_is_never_decoded(self, tmp_path: Path) -> None:
        from agnette.agl.semantics.values import IntValue

        runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
        prepared = _prepared(
            "@param let count: int = 1\nprogram def main() -> unit =\n  let _ = count\n", tmp_path
        )
        discovery = runtime.discover_programs(prepared)
        (count,) = discovery.params_for(discovery.programs[0])

        preflight = _preflight(
            runtime,
            prepared,
            discovery,
            param_values={count.key: 7},
            param_values_lower={count.key: "not-an-int"},
        )

        assert preflight.result.ok
        assert preflight.param_seeds == {count.key: IntValue(7)}
