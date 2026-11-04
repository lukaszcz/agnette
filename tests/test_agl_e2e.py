"""End-to-end behavior tests for AgL (the `agm exec` workflow DSL).

This suite is the TDD specification for the AgL implementation. Every
tests/agl/programs/**/*.agl file is a complete AgL program executed under
each scenario in its sidecar `<name>.scenarios.json`; every
tests/agl/rejections/**/*.agl file is an invalid program that the static
pipeline must reject before executing anything. The data format is documented
in tests/agl/README.md.

Public contract exercised here:

    from agnette.agl import PipelineDriver

    runtime = PipelineDriver(
        default_strict_json=False,  # lenient JSON recovery is the default
        agent_dispatcher=fn,        # fn(request) -> str,
        get_sandbox_context=None,
    )
    result = runtime.run(source)

RunResult surface asserted:

    result.ok           True iff static checks and argument validation passed
                        and no uncaught AgL exception was raised
    result.diagnostics  pre-execution failures (static errors, program
                        argument validation), each with `.message: str` and
                        `.line: int` (1-based source line)
    result.error        the uncaught AgL exception or None, exposing
                        `.type_name: str` and `.fields` — a mapping of the
                        exception's declared fields (including "message") to
                        JSON-shaped Python values

`print` writes to the process stdout (captured with capsys).
"""

from __future__ import annotations

import re
import unittest.mock
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agnette.agent.session.protocol import (
    SessionHostError,
)
from agnette.packages.layout import MODULE_TREE_DIRNAME
from tests._agl_helpers import (
    REPO_STDLIB_ROOT,
    agl_roots,
    module_param_values,
    run_inline_code,
)
from tests._process_helpers import FakeShell
from tests.agl.isolated_runner import run_scenario
from tests.agl.scenario_harness import (
    AGL_DIR,
    EXTERNS_PROGRAMS_DIR,
    PROGRAMS_DIR,
    RESOURCE_PROGRAMS_DIR,
    SESSION_STATIC_DECLARATIONS,
    CompilationCache,
    ScenarioSessionHost,
    agent_from_spec,
    assert_program_scenario,
    assert_schema_paths,
    assert_sessions,
    fixture_roots,
    load_json,
    prepare_temp_filesystem,
    run_source_entry,
    spec_warning_policy,
)

REJECTIONS_DIR = AGL_DIR / "rejections"


STDLIB_MODULES_DIR = REPO_STDLIB_ROOT / MODULE_TREE_DIRNAME


@pytest.mark.parametrize(
    "levels", [{"no-such-warning": "deny"}, {"tab-indentation": "error"}, {"tab-indentation": None}]
)
def test_invalid_warning_levels_are_rejected_in_fixtures(levels: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="warning"):
        spec_warning_policy({"warning_levels": levels})


def test_module_param_paths_must_exist_in_selected_program_closure() -> None:
    from agnette.agl import PipelineDriver

    prepared = PipelineDriver.prepare_program(
        "@param let enabled: bool = false\nprogram def main() -> unit = ()\n",
        default_stdlib=False,
    )
    discovery = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).discover_programs(
        prepared
    )

    with pytest.raises(AssertionError):
        module_param_values(discovery, discovery.programs[0], {"<entry>::missing": True})


def test_inline_entry_module_params_seed_root_binding(capsys: pytest.CaptureFixture[str]) -> None:
    from agnette.agl import PipelineDriver

    result = run_inline_code(
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None),
        "@param var value: int = 1\nvalue := value + 1\nprint value\n",
        module_params={"<entry>::value": 4},
    )

    assert result.ok
    assert capsys.readouterr().out == "5\n"


def test_inline_entry_with_its_own_program_def_follows_relaxed_binding_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An inline (``-c``) source declaring its own ``program def`` is static-root,
    like a file program: a def declared above a root var it reads still works."""
    from agnette.agl import PipelineDriver

    result = run_inline_code(
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None),
        "def read-counter() -> int = counter\n\nvar counter = 41\n\n"
        "program def main() -> unit =\n  counter := counter + 1\n  print read-counter()\n",
    )

    assert result.ok, result.diagnostics
    assert capsys.readouterr().out == "42\n"


def test_schema_paths_assert_exact_nested_values() -> None:
    schema = {
        "$ref": "#/$defs/Workflow_Task",
        "$defs": {
            "Workflow_Task": {
                "properties": {"children": {"items": {"$ref": "#/$defs/Workflow_Task"}}}
            }
        },
    }

    assert_schema_paths(
        schema,
        [
            {"path": ["$ref"], "equals": "#/$defs/Workflow_Task"},
            {
                "path": [
                    "$defs",
                    "Workflow_Task",
                    "properties",
                    "children",
                    "items",
                    "$ref",
                ],
                "equals": "#/$defs/Workflow_Task",
            },
        ],
    )


def test_filesystem_fixture_skips_symlink_scenarios_when_symlinks_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable_symlink(
        self: Path, target: str | Path, target_is_directory: bool = False
    ) -> None:
        raise OSError("symbolic links are unavailable")

    monkeypatch.setattr(Path, "symlink_to", unavailable_symlink)

    with pytest.raises(pytest.skip.Exception):
        prepare_temp_filesystem(
            {
                "filesystem": {
                    "directories": ["target"],
                    "directory_symlinks": {"link": "target"},
                }
            },
            tmp_path,
        )


def test_scripted_sessions_observe_identity_operations_and_lifecycle() -> None:
    from agnette.agent.session.protocol import SessionAskRequest

    agent = agent_from_spec(
        "writer",
        {
            "responses": ["answer"],
            "repeat_last": True,
            "session": {
                "operations": {
                    "compact": ["success"],
                    "reset": ["success"],
                    "fork": ["success"],
                    "stats": [
                        {
                            "input_tokens": 3,
                            "output_tokens": 5,
                            "cost": "0.12",
                            "context_percent": "4.5",
                        }
                    ],
                    "set-name": ["unsupported"],
                },
            },
        },
    )
    service = agent.session_service()

    parent = service.open(object(), "scripted", name="primary")
    assert service.ask(parent, SessionAskRequest("first")).content == "answer"
    service.compact(parent, "summarize")
    service.reset(parent)
    child = service.fork(parent)
    assert service.ask(parent, SessionAskRequest("second")).content == "answer"
    assert service.ask(child, SessionAskRequest("child prompt")).content == "answer"
    stats = service.stats(parent)
    with pytest.raises(SessionHostError) as raised:
        service.set_name(parent, "renamed")
    service.close(parent)
    service.close(child)

    assert stats.input_tokens == 3
    assert raised.value.operation == "set-name"
    assert_sessions(
        {"writer": agent},
        {
            "sessions": [
                {"agent": "writer", "tag": "session-1", "opened": True, "closed": True},
                {
                    "agent": "writer",
                    "tag": "session-2",
                    "parent": "session-1",
                    "closed": True,
                },
            ],
            "session_prompts": [
                {"agent": "writer", "session": "session-1", "call": 0, "equals": "first"},
                {
                    "agent": "writer",
                    "session": "session-1",
                    "call": 1,
                    "follow_up": {"equals": "second"},
                },
                {"agent": "writer", "session": "session-2", "call": 0, "equals": "child prompt"},
            ],
            "session_operations": [
                {
                    "agent": "writer",
                    "session": "session-1",
                    "operation": "compact",
                    "arg": "summarize",
                    "outcome": "success",
                },
                {
                    "agent": "writer",
                    "session": "session-1",
                    "operation": "reset",
                    "outcome": "success",
                },
                {
                    "agent": "writer",
                    "session": "session-1",
                    "operation": "fork",
                    "outcome": "success",
                },
                {
                    "agent": "writer",
                    "session": "session-1",
                    "operation": "stats",
                    "outcome": "success",
                },
                {
                    "agent": "writer",
                    "session": "session-1",
                    "operation": "set-name",
                    "arg": "renamed",
                    "outcome": "unsupported",
                },
            ],
        },
    )


def test_scripted_sessions_record_capability_rejections_without_outcomes() -> None:

    agent = agent_from_spec(
        "writer",
        {
            "responses": [],
            "session": {"capabilities": ["compact"]},
        },
    )
    service = agent.session_service()
    handle = service.open(object(), "scripted")

    with pytest.raises(SessionHostError) as raised:
        service.set_name(handle, "renamed")

    assert raised.value.operation == "set-name"
    assert_sessions(
        {"writer": agent},
        {
            "session_operations": [
                {
                    "agent": "writer",
                    "session": "session-1",
                    "operation": "set-name",
                    "arg": "renamed",
                    "outcome": "unsupported",
                }
            ]
        },
    )


def test_scripted_sessions_reconcile_ordered_operation_outcomes() -> None:
    agent = agent_from_spec(
        "writer",
        {
            "responses": [],
            "session": {"operations": {"compact": ["success", "success"]}},
        },
    )
    service = agent.session_service()
    handle = service.open(object(), "scripted")

    service.compact(handle, "first")
    with pytest.raises(AssertionError, match="not fully consumed"):
        assert_sessions({"writer": agent}, {})

    service.compact(handle, "second")
    assert_sessions(
        {"writer": agent},
        {
            "session_operations": [
                {
                    "agent": "writer",
                    "session": "session-1",
                    "operation": "compact",
                    "call": 0,
                    "arg": "first",
                    "outcome": "success",
                },
                {
                    "agent": "writer",
                    "session": "session-1",
                    "operation": "compact",
                    "call": 1,
                    "arg": "second",
                    "outcome": "success",
                },
            ]
        },
    )


def test_session_expectations_reject_unlisted_empty_sessions() -> None:
    agent = agent_from_spec("writer", {"responses": []})
    service = agent.session_service()
    service.open(object(), "scripted")
    service.open(object(), "scripted")

    with pytest.raises(AssertionError, match="session set"):
        assert_sessions(
            {"writer": agent},
            {
                "sessions": [
                    {"agent": "writer", "tag": "session-1", "opened": True, "closed": False}
                ]
            },
        )


def test_scenario_host_reports_unknown_handles_by_attempted_operation() -> None:
    from agnette.agl.runtime.sessions import SessionHostError as AglSessionHostError

    host = ScenarioSessionHost({})
    operations: tuple[tuple[str, Callable[[], object]], ...] = (
        ("ask", lambda: host.ask("unknown", "prompt")),
        ("compact", lambda: host.compact("unknown")),
        ("reset", lambda: host.reset("unknown")),
        ("fork", lambda: host.fork("unknown")),
        ("set-name", lambda: host.set_name("unknown", "name")),
        ("stats", lambda: host.stats("unknown")),
        ("snapshot", lambda: host.snapshot("unknown")),
        ("close", lambda: host.close("unknown")),
    )

    for operation, attempt in operations:
        with pytest.raises(AglSessionHostError) as raised:
            attempt()
        assert raised.value.operation == operation


def test_scripted_sessions_reject_extra_capability_rejection_operation() -> None:

    agent = agent_from_spec(
        "writer",
        {
            "responses": [],
            "session": {"capabilities": ["compact"]},
        },
    )
    service = agent.session_service()
    handle = service.open(object(), "scripted")
    service.compact(handle, "summarize")
    assert_sessions({"writer": agent}, {})
    with pytest.raises(SessionHostError):
        service.set_name(handle, "renamed")

    with pytest.raises(AssertionError, match="operation count"):
        assert_sessions(
            {"writer": agent},
            {
                "session_operations": [
                    {
                        "agent": "writer",
                        "session": "session-1",
                        "operation": "compact",
                        "arg": "summarize",
                        "outcome": "success",
                    }
                ]
            },
        )


def test_scripted_sessions_reject_unsupported_reset_outcomes() -> None:
    with pytest.raises(ValueError, match="reset"):
        agent_from_spec(
            "writer",
            {"responses": [], "session": {"operations": {"reset": ["unsupported"]}}},
        )


def test_scripted_sessions_detect_an_extra_retry_prompt() -> None:
    from agnette.agent.session.protocol import SessionAskRequest

    agent = agent_from_spec("writer", {"responses": ["answer"], "repeat_last": True})
    service = agent.session_service()
    handle = service.open(object(), "scripted")
    service.ask(handle, SessionAskRequest("first"))
    service.ask(handle, SessionAskRequest("retry"))

    with pytest.raises(AssertionError, match="prompt count"):
        assert_sessions(
            {"writer": agent},
            {
                "session_prompts": [
                    {"agent": "writer", "session": "session-1", "call": 0, "equals": "first"}
                ]
            },
        )


@pytest.mark.parametrize("value", [0, -1, True, "2", 1.5])
def test_interrupt_on_call_must_be_a_positive_integer(value: Any) -> None:
    with pytest.raises(ValueError, match="interrupt_on_call"):
        agent_from_spec("writer", {"responses": [], "interrupt_on_call": value})


_INTERRUPTED_PROGRAM = Path(__file__).parent / "agl/programs/calls/interrupt_propagation.agl"


@pytest.mark.parametrize(
    ("expect", "error"),
    [
        ({"interrupted": "SystemExit"}, ValueError),
        ({"interrupted": "KeyboardInterrupt"}, AssertionError),
        ({}, AssertionError),
    ],
)
def test_interrupted_expectation_must_match_the_termination(
    expect: dict[str, Any],
    error: type[Exception],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    scenario = {
        "name": "interrupted",
        "agents": {"builder": {"responses": ["a"], "interrupt_on_call": 2}},
        "expect": {"stdout": "start\na\n", "calls": {"builder": 2}, **expect},
    }
    with pytest.MonkeyPatch.context() as patch, pytest.raises(error):
        assert_program_scenario(
            _INTERRUPTED_PROGRAM,
            scenario,
            lambda: capsys.readouterr().out,
            tmp_path,
            patch,
            None,
        )


def test_scripted_session_ask_closes_an_ephemeral_session() -> None:
    from agnette.agent.session.protocol import SessionAskRequest

    agent = agent_from_spec("writer", ["answer"])
    service = agent.session_service()
    response = service.with_ephemeral(
        object(),
        "scripted",
        lambda handle: service.ask(handle, SessionAskRequest("one-off")),
        single_prompt=True,
    )

    assert response.content == "answer"
    assert_sessions(
        {"writer": agent},
        {
            "sessions": [{"agent": "writer", "tag": "session-1", "opened": True, "closed": True}],
            "session_prompts": [
                {"agent": "writer", "session": "session-1", "call": 0, "equals": "one-off"}
            ],
        },
    )


def _scenario_params() -> list[Any]:
    params: list[Any] = []
    for program in sorted(PROGRAMS_DIR.rglob("*.agl")):
        sidecar = program.with_name(program.stem + ".scenarios.json")
        rel = program.relative_to(PROGRAMS_DIR).with_suffix("")
        groups: list[tuple[dict[str, Any], ...]] = []
        batch: list[dict[str, Any]] = []
        for scenario in load_json(sidecar)["scenarios"]:
            shareable = not (
                program.is_relative_to(EXTERNS_PROGRAMS_DIR)
                or program.is_relative_to(RESOURCE_PROGRAMS_DIR)
                or {
                    "filesystem",
                    "sandbox_home",
                    "module_roots",
                    "packages",
                    "installed_packages",
                    "stdlib_root",
                    "jev",
                    "warning_levels",
                    "isolated",
                }
                & scenario.keys()
            )
            if not shareable and batch:
                groups.append(tuple(batch))
                batch = []
            if shareable:
                batch.append(scenario)
            if not shareable or len(batch) == 4:
                group = tuple(batch) if shareable else (scenario,)
                groups.append(group)
                batch = []
        if batch:
            groups.append(tuple(batch))
        params.extend(
            pytest.param(program, group, id=f"{rel}::" + "+".join(s["name"] for s in group))
            for group in groups
        )
    return params


def test_function_param_scenario_runs_isolated(tmp_path: Path) -> None:
    run_scenario(
        PROGRAMS_DIR / "params" / "function_param_seeds.agl",
        "seeds_replace_defaults_at_every_call_form",
        tmp_path,
    )


def _rejection_params() -> list[Any]:
    return [
        pytest.param(program, id=str(program.relative_to(REJECTIONS_DIR).with_suffix("")))
        for program in sorted(REJECTIONS_DIR.rglob("*.agl"))
    ]


@pytest.mark.parametrize(("program", "scenarios"), _scenario_params())
def test_program_scenarios(
    program: Path,
    scenarios: tuple[dict[str, Any], ...],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Share immutable compilation only; every scenario gets fresh runtime and patches.

    An ``isolated`` scenario runs in a fresh single-threaded subprocess instead.
    """
    compilations: CompilationCache | None = {} if len(scenarios) > 1 else None
    for scenario in scenarios:
        if scenario.get("isolated"):
            run_scenario(program, scenario["name"], tmp_path)
            continue
        with pytest.MonkeyPatch.context() as patch:
            assert_program_scenario(
                program,
                scenario,
                lambda: capsys.readouterr().out,
                tmp_path,
                patch,
                compilations,
            )


@pytest.mark.parametrize("program", _rejection_params())
def test_static_rejection(program: Path) -> None:
    from agnette.agl import PipelineDriver

    spec = load_json(program.with_name(program.stem + ".expect.json"))
    expect = spec["diagnostic"]
    roots = fixture_roots(spec)
    prepared = PipelineDriver.prepare_program(program.read_text(encoding="utf-8"), roots=roots)
    # Invalid programs need neither discovery nor argument preflight: compile once.
    result = PipelineDriver(
        resolve_agent_spec=None, get_sandbox_context=None, warning_policy=spec_warning_policy(spec)
    ).run_prepared(prepared)
    assert not result.ok, "expected the program to be rejected statically"
    assert result.error is None, "static rejection must happen before execution"
    diagnostics = list(result.diagnostics)
    assert diagnostics, "expected at least one diagnostic"
    if "warning" in expect:
        diagnostics = [d for d in diagnostics if d.warning_name == expect["warning"]]
        assert diagnostics, f"no diagnostic denies {expect['warning']!r}"
    needles = expect.get("message_contains", [])
    if "line" in expect:
        candidates = [diagnostic for diagnostic in diagnostics if diagnostic.line == expect["line"]]
        message_groups = [needles]
    else:
        # Unlocated expectations may describe several independent errors.
        candidates = diagnostics
        message_groups = [[needle] for needle in needles]
    assert candidates, f"no diagnostic at {expect!r}; diagnostics: {diagnostics!r}"
    for group in message_groups:
        assert any(
            all(needle.lower() in diagnostic.message.lower() for needle in group)
            for diagnostic in candidates
        ), f"no single diagnostic matches {group!r} at {expect!r}; diagnostics: {diagnostics!r}"


def test_pipeline_run_invokes_the_single_entry_program(capsys: pytest.CaptureFixture[str]) -> None:
    from agnette.agl import PipelineDriver

    result = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).run(
        'program def main() -> unit = print "hello"'
    )

    assert result.ok
    assert capsys.readouterr().out == "hello\n"


def test_fun_id_is_reached_through_its_module_not_the_prelude(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agnette.agl import PipelineDriver

    driver = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    qualified = driver.run("import std/fun\nprogram def main() -> unit = print(fun::id(3))")
    bare = driver.run("program def main() -> unit = print(id(3))", check_only=True)

    assert qualified.ok
    assert capsys.readouterr().out == "3\n"
    assert not bare.ok


def test_pipeline_check_only_rejects_ambiguous_default_program() -> None:
    from agnette.agl import PipelineDriver

    result = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).run(
        "program def first() -> unit = ()\nprogram def second() -> unit = ()\n",
        check_only=True,
    )

    assert not result.ok
    assert result.error is None
    assert len(result.diagnostics) == 1
    assert "program" in result.diagnostics[0].message


def test_direct_std_option_import_runs_without_the_automatic_prelude(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An explicit ``std/option`` import remains sufficient under ``--no-stdlib``."""
    from agnette.agl import PipelineDriver

    roots = agl_roots()
    result = run_source_entry(
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None),
        "import std/prelude::print\n"
        "import std/option::Option\n"
        "program def main() -> unit =\n"
        "  let option: Option[int] = Option::Some(value = 2)\n"
        "  print(option.map(fn(x: int) => x + 1))\n",
        roots=roots,
        default_stdlib=False,
    )

    assert list(result.diagnostics) == []
    assert result.error is None
    assert capsys.readouterr().out == "Option::Some(value = 3)\n"


def test_std_core_option_reexport_preserves_nominal_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The re-export and direct module name one interoperable ``Option`` type."""
    from agnette.agl import PipelineDriver

    roots = agl_roots()
    result = run_source_entry(
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None),
        "import std/prelude::{Option as CoreOption, print}\n"
        "import std/option::Option\n"
        "program def main() -> unit =\n"
        "  let value: CoreOption[int] = Option::Some(value = 3)\n"
        "  print(value.unwrap-or(0))\n",
        roots=roots,
        default_stdlib=False,
    )

    assert list(result.diagnostics) == []
    assert result.error is None
    assert capsys.readouterr().out == "3\n"


def test_qualified_std_prelude_print_still_works(capsys: pytest.CaptureFixture[str]) -> None:
    """A fully qualified ``std/prelude::print(...)`` call still runs, exactly as
    the bare form does — a built-in call is classified once its callee
    resolves to a ``builtin def``, and ``std/prelude::print`` reaches the same
    declaration a bare ``print`` does, just by a qualified route."""
    from agnette.agl import PipelineDriver

    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    result = run_source_entry(runtime, 'program def main() -> unit = std/prelude::print("hi")\n')

    assert list(result.diagnostics) == [], (
        f"unexpected static diagnostics: {' | '.join(d.message for d in result.diagnostics)}"
    )
    assert result.error is None, f"unexpected error: {result.error}"
    assert capsys.readouterr().out == "hi\n"


_SCOPED_STDLIB_MODULES = ("errors", "agent", "session", "exec", "io", "value", "package")


def _without_ordinary_defs(source: str) -> str:
    """Drop every ordinary ``def`` (not ``builtin``/``extern``) with its attributes and body."""
    kept: list[str] = []
    attributes: list[str] = []
    in_def = False
    for line in source.splitlines(keepends=True):
        if in_def:
            if line.startswith((" ", "\t", ")")) or not line.strip():
                continue
            in_def = False
        if line.startswith("@"):
            attributes.append(line)
        elif line.startswith("def "):
            attributes.clear()
            in_def = True
        else:
            kept.extend(attributes)
            attributes.clear()
            kept.append(line)
    return "".join(kept + attributes)


def _scoped_stdlib_root(tmp_path: Path) -> Path:
    """Build a throwaway standard-library root whose builtin surface is one scoped module.

    The standard library's builtin declarations are concatenated inside a
    ``scope Std`` region of a replacement ``std/prelude``, importing only
    ``Option`` and ``Result``: the region itself holds everything else they
    name, including a local ``path`` alias standing in for extern-backed
    ``std/path``. Canonical
    session statics are omitted because wrapping changes their owner path,
    and ``exec``'s and the agent calls' defaults are rewritten so that no
    default names the environment or config module. Ordinary ``def``s are
    dropped from those modules because they call extern-backed methods this
    fixture lacks. Infix declarations stay at module root because scopes
    cannot contain them.
    """
    scoped_stdlib_root = tmp_path / "scoped_stdlib"
    std_dir = scoped_stdlib_root / MODULE_TREE_DIRNAME
    std_dir.mkdir(parents=True)

    def module_lines(name: str) -> list[str]:
        source = (STDLIB_MODULES_DIR / f"{name}.agl").read_text(encoding="utf-8")
        # Documentation must not affect the declaration rewrites below.
        source = re.sub(r'(?m)^ *@doc\((?:"""[\s\S]*?"""|"(?:\\.|[^"\\])*")\)\n', "", source)
        return source.splitlines(keepends=True)

    fun_lines = module_lines("fun")
    infix_declarations = "".join(line for line in fun_lines if line.startswith("infix"))
    scoped_sources = [
        "import std/option::Option\n",
        "import std/result::Result\n",
        "import std/sandbox::Sandbox\n",
        "type path = text\n",
    ]
    scoped_sources.append("".join(line for line in fun_lines if not line.startswith("infix")))
    for name in _SCOPED_STDLIB_MODULES:
        # ``std/sandbox`` stays a real top-level module (see below); its
        # import is hoisted to the region's own leading imports above so
        # imports stay first, while every per-module import is dropped in
        # favor of the scoped Option/Result/Sandbox imports.
        source = "".join(line for line in module_lines(name) if not line.startswith("import "))
        # Externs belong to their companion modules, not this builtin-only fixture.
        source = re.sub(
            r"(?m)(?:^@extern-name\([^\n]*\)\n)?^extern def [\s\S]*?\) -> [^\n]*\n",
            "",
            source,
        )
        source = _without_ordinary_defs(source)
        source = source.replace(
            "parse-error-retries: int = std/config::parse-error-retries",
            "parse-error-retries: int = 0",
        )
        if name == "session":
            source = source.replace(SESSION_STATIC_DECLARATIONS, "")
        # ``default-sandbox`` is an extern, dropped above.
        source = re.sub(
            r"sandbox: AgentSandbox = default-sandbox\((?:agent|self)\)",
            "sandbox: AgentSandbox = Disabled",
            source,
        )
        if name == "agent":
            source = source.replace(
                "  agent: Agent = std/config::agent,\n",
                '  agent: Agent = AgentCommand(command = ""),\n',
            ).replace("  env: Environ = std/env::environ,\n", "")
        if name == "exec":
            source = source.replace(
                "builtin def exec[T](\n"
                "  command: text,\n"
                "  env: Environ = std/env::environ,\n"
                "  cwd: Option[path] = None,\n"
                "  timeout: Option[text] = std/config::timeout,\n"
                "  sandbox: Option[Sandbox] = None,\n"
                '  format: text = "",\n'
                "  strict-json: bool = false,\n"
                "  parse-error-retries: int = 0,\n"
                ") -> T\n",
                "builtin def exec[T](command: text) -> T\n",
            )
        scoped_sources.append(source)
    (std_dir / "prelude.agl").write_text(
        f"{infix_declarations}\nscope Std\n{''.join(scoped_sources)}end Std\n", encoding="utf-8"
    )
    (std_dir / "path.agl").write_text("builtin type path = text\n", encoding="utf-8")
    for name in (
        "option.agl",
        "optional.agl",
        "pair.agl",
        "either.agl",
        "result.agl",
        "sandbox.agl",
    ):
        source = (STDLIB_MODULES_DIR / name).read_text(encoding="utf-8")
        if name == "result.agl":
            source = source.replace(
                "def attempt[T](f: () -> T) -> Result[T, Exception] =\n"
                "  try Result::Ok(value = f()) catch Exception as e => Result::Err(error = e)\n",
                "def attempt[T](f: () -> T) -> Result[T, Exception] = Result::Ok(value = f())\n",
            )
        (std_dir / name).write_text(source, encoding="utf-8")
    return scoped_stdlib_root


def test_legacy_exec_signature_rejects_extended_options_with_a_diagnostic(tmp_path: Path) -> None:
    """A custom old-style exec declaration must not trip an internal assertion."""
    from agnette.agl import PipelineDriver
    from agnette.agl.modules.roots import RootSet

    scoped_stdlib_root = _scoped_stdlib_root(tmp_path)
    result = run_source_entry(
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None),
        'program def main() -> unit = Std::exec("echo hi", env = ())\n',
        roots=RootSet(roots=frozenset(), stdlib_roots=frozenset({scoped_stdlib_root})),
    )

    assert not result.ok
    assert result.diagnostics


def test_scoped_stdlib_arrangement_runs_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A whole scoped ``std/prelude`` module works end to end.

    This runs the runtime-lowerable expanded prelude through parsing, scope
    resolution, typechecking, lowering, and evaluation. It exercises scoped
    ``exec`` dispatching to scoped ``ExecResult`` at a real host boundary,
    plus a scoped ``Exception`` / ``RangeError`` hierarchy raised and left
    uncaught. The throwaway root avoids colliding with the repository stdlib.
    """
    from agnette.agl import PipelineDriver
    from agnette.agl.modules.roots import RootSet

    scoped_stdlib_root = _scoped_stdlib_root(tmp_path)

    program = (
        "program def main() -> unit =\n"
        '  let r = Std::exec("echo hi")\n  Std::print(r.stdout)\n'
        '  raise Std::RangeError(message = "boom")\n'
    )

    shell = FakeShell([{"command": "echo hi", "stdout": "hi"}])
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    with unittest.mock.patch("agnette.core.process.run_capture_result", side_effect=shell):
        result = run_source_entry(
            runtime,
            program,
            roots=RootSet(roots=frozenset(), stdlib_roots=frozenset({scoped_stdlib_root})),
        )
    assert list(result.diagnostics) == [], (
        f"unexpected static diagnostics: {' | '.join(d.message for d in result.diagnostics)}"
    )
    shell.assert_complete()
    assert capsys.readouterr().out == "hi\n"
    assert result.error is not None, "expected the uncaught scoped RangeError"
    assert result.error.type_name == "Std::RangeError"
    assert result.error.fields["message"] == "boom"


def test_scoped_stdlib_arrangement_structured_exec_result_is_the_scoped_nominal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A structured ``exec`` result is tagged with its own scoped nominal.

    The result is annotated ``Std::ExecResult`` explicitly, so the static
    target type is the scoped nominal by the annotation rather than by
    ``exec``'s own default. The field access below then only succeeds if the
    host-minted value carries that very same scoped nominal — an exact
    nominal projection rejects any other identity.
    """
    from agnette.agl import PipelineDriver
    from agnette.agl.modules.roots import RootSet

    scoped_stdlib_root = _scoped_stdlib_root(tmp_path)

    program = (
        "program def main() -> unit =\n"
        '  let r: Std::ExecResult = Std::exec("echo hi")\n  Std::print(r.stdout)\n'
    )

    shell = FakeShell([{"command": "echo hi", "stdout": "hi"}])
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    with unittest.mock.patch("agnette.core.process.run_capture_result", side_effect=shell):
        result = run_source_entry(
            runtime,
            program,
            roots=RootSet(roots=frozenset(), stdlib_roots=frozenset({scoped_stdlib_root})),
        )
    shell.assert_complete()

    assert list(result.diagnostics) == [], (
        f"unexpected static diagnostics: {' | '.join(d.message for d in result.diagnostics)}"
    )
    assert result.error is None, f"unexpected error: {result.error}"
    assert capsys.readouterr().out == "hi\n"


def test_scoped_stdlib_arrangement_uncaught_host_raised_exec_error_reports_scoped_spelling(
    tmp_path: Path,
) -> None:
    """An uncaught host-raised exception reports its declared, scoped spelling.

    A failed (non-structured, ``text``-typed) ``Std::exec`` call raises
    ``ExecError`` at the host boundary; left uncaught, it now reports
    ``Std::ExecError`` — the outside-the-region half of the catchability fix
    observable without a qualified ``catch`` (the grammar admits none)."""
    from agnette.agl import PipelineDriver
    from agnette.agl.modules.roots import RootSet

    scoped_stdlib_root = _scoped_stdlib_root(tmp_path)

    program = (
        'program def main() -> unit =\n  let out: text = Std::exec("false")\n  Std::print(out)\n'
    )

    shell = FakeShell([{"command": "false", "returncode": 1}])
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    with unittest.mock.patch("agnette.core.process.run_capture_result", side_effect=shell):
        result = run_source_entry(
            runtime,
            program,
            roots=RootSet(roots=frozenset(), stdlib_roots=frozenset({scoped_stdlib_root})),
        )
    shell.assert_complete()

    assert list(result.diagnostics) == [], (
        f"unexpected static diagnostics: {' | '.join(d.message for d in result.diagnostics)}"
    )
    assert result.error is not None, "expected the uncaught scoped ExecError"
    assert result.error.type_name == "Std::ExecError"


def test_scoped_stdlib_arrangement_bare_print_is_undefined_but_qualified_works(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the real prelude wrapped in ``scope Std``, only ``Std::print`` works.

    A built-in call is classified only once its callee resolves to a
    ``builtin def`` declaration, the same as any other reference. The named
    region takes the bare route away from ``print``, leaving its qualified
    path."""
    from agnette.agl import PipelineDriver
    from agnette.agl.modules.roots import RootSet

    scoped_stdlib_root = _scoped_stdlib_root(tmp_path)
    roots = RootSet(roots=frozenset(), stdlib_roots=frozenset({scoped_stdlib_root}))
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)

    bare_result = run_source_entry(
        runtime, 'program def main() -> unit = print("hi")\n', roots=roots
    )
    assert not bare_result.ok, "expected the bare 'print' call to be statically rejected"
    assert bare_result.diagnostics, "expected at least one diagnostic"

    qualified_result = run_source_entry(
        runtime, 'program def main() -> unit = Std::print("hi")\n', roots=roots
    )
    assert list(qualified_result.diagnostics) == [], (
        f"unexpected static diagnostics: "
        f"{' | '.join(d.message for d in qualified_result.diagnostics)}"
    )
    assert qualified_result.error is None, f"unexpected error: {qualified_result.error}"
    assert capsys.readouterr().out == "hi\n"


def test_scoped_builtin_hierarchy_declared_in_the_entry_module_catches_a_host_raise(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A host-raised exception is catchable by its declared, scoped spelling.

    ``catch`` accepts only an unqualified NAME (the grammar admits no
    qualified ``A::B`` there), so the catching code lives inside the same
    ``scope Host`` region that declares the hierarchy, where the bare name
    resolves to the region's own declaration — the same arrangement
    ``scoped_builtin_stdlib_arrangement.agl``'s own ``report``/``RangeError``
    catch already uses. This program declares the whole exec/exception
    surface itself and loads without the standard library (``default_stdlib
    =False``), so its own ``scope Host`` module lowers normally (unlike the
    real ``std/prelude`` module, whose pure declarations are linked with the
    prelude). A failed (``text``-typed, non-structured) ``exec`` call
    raises ``ExecError`` at the host boundary; before per-path identity was
    restored the host would have minted a path-free nominal while the type
    kept its declared path, so this bare ``catch`` could never match. Now
    both sides agree, and the program handles it.
    """
    from agnette.agl import PipelineDriver

    program = (
        "scope Host\n"
        "  builtin\n"
        "  exception Exception\n"
        "    @arg-named message: text\n"
        "  builtin\n"
        "  exception ExecError extends Exception\n"
        "    command: text\n"
        "    exit-code: int\n"
        "    stdout: text\n"
        "    stderr: text\n"
        "    timed-out: bool\n"
        "  builtin record ExecResult\n"
        "    stdout: text\n"
        "    exit-code: int\n"
        "    stderr: text\n"
        "    timed-out: bool\n"
        "  builtin def exec[T](command: text) -> T\n"
        "  def run(cmd: text) -> text =\n"
        "    try\n"
        "      let out: text = exec(cmd)\n"
        "      out\n"
        "    catch ExecError as e =>\n"
        '      "caught"\n'
        "end Host\n"
        "\n"
        "builtin def print[T](value: T) -> unit\n"
        "\n"
        "program def main() -> unit =\n"
        '  print(Host::run("false"))\n'
    )

    shell = FakeShell([{"command": "false", "returncode": 1}])
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    with unittest.mock.patch("agnette.core.process.run_capture_result", side_effect=shell):
        result = run_source_entry(runtime, program, default_stdlib=False)
    shell.assert_complete()

    assert list(result.diagnostics) == [], (
        f"unexpected static diagnostics: {' | '.join(d.message for d in result.diagnostics)}"
    )
    assert result.error is None, f"expected the raised ExecError to be caught: {result.error}"
    assert capsys.readouterr().out == "caught\n"


def test_builtin_print_can_be_passed_as_a_function_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A builtin reference is an ordinary callable value, not only a direct-call marker."""
    from agnette.agl import PipelineDriver

    source = (
        "def apply(f: text -> unit, value: text) -> unit = f(value)\n"
        'program def main() -> unit = apply(print, "hello")\n'
    )

    result = run_source_entry(
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None), source
    )

    assert result.ok
    assert capsys.readouterr().out == "hello\n"


def test_scoped_builtin_reference_can_be_called_through_a_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agnette.agl import PipelineDriver

    source = (
        "scope Host\n"
        "  builtin def render[T](value: T) -> text\n"
        "end Host\n\n"
        "program def main() -> unit =\n"
        "  let show: int -> text = Host::render\n"
        "  print(show(7))\n"
    )

    result = run_source_entry(
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None), source
    )

    assert result.ok
    assert capsys.readouterr().out == "7\n"


def test_render_and_copy_builtins_can_be_called_through_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agnette.agl import PipelineDriver

    source = (
        "program def main() -> unit =\n"
        "  let show = render::[int]\n"
        "  let clone: array[int] -> array[int] = copy\n"
        "  let clone-level: array[int] -> array[int] = shallow-copy\n"
        "  print(show(clone-level(clone([7]))[0]))\n"
    )

    result = run_source_entry(
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None), source
    )

    assert result.ok
    assert capsys.readouterr().out == "7\n"


def test_ask_request_builtin_value_uses_its_default_text_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agnette.agl import PipelineDriver

    source = (
        "program def main() -> unit =\n"
        "  let make-request: text -> AgentRequest = ask-request\n"
        '  print(make-request("Review this").prompt)\n'
    )

    result = run_source_entry(
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None), source
    )

    assert result.ok
    assert capsys.readouterr().out == "Review this\n"


def test_builtin_exec_value_uses_ambient_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agnette.agl import PipelineDriver

    source = (
        "program def main() -> unit =\n"
        "  let run: text -> ExecResult = exec\n"
        '  print(run("answer").stdout)\n'
    )
    shell = FakeShell([{"command": "answer", "stdout": "42"}])

    with unittest.mock.patch("agnette.core.process.run_capture_result", side_effect=shell):
        result = run_source_entry(
            PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None), source
        )

    shell.assert_complete()
    assert result.ok
    assert capsys.readouterr().out == "42\n"


def test_effect_builtin_values_accept_explicit_output_specialization(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agnette.agl import PipelineDriver

    source = (
        "program def main() -> unit =\n"
        "  let query = ask::[int]\n"
        "  let run = exec::[int]\n"
        '  print(query("first") + run("second"))\n'
    )
    shell = FakeShell([{"command": "second", "stdout": "1\n"}])

    with unittest.mock.patch("agnette.core.process.run_capture_result", side_effect=shell):
        result = run_source_entry(
            PipelineDriver(
                resolve_agent_spec=None,
                agent_dispatcher=lambda _request: "41",
                get_sandbox_context=None,
            ),
            source,
        )

    shell.assert_complete()
    assert result.ok
    assert capsys.readouterr().out == "42\n"


def test_unconstrained_builtin_ask_value_defaults_to_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agnette.agl import PipelineDriver

    source = 'program def main() -> unit =\n  let query = ask\n  print(query("Question"))\n'

    result = run_source_entry(
        PipelineDriver(
            resolve_agent_spec=None,
            agent_dispatcher=lambda _request: "answer",
            get_sandbox_context=None,
        ),
        source,
    )

    assert result.ok
    assert capsys.readouterr().out == "answer\n"


def test_builtin_ask_can_be_passed_as_a_contextually_typed_function_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An ask value retains the statically compiled output contract of its occurrence."""
    from agnette.agl import PipelineDriver

    prompts: list[str] = []

    def answer(request: Any) -> str:
        prompts.append(request.prompt)
        return "42"

    source = (
        "def apply(f: text -> int, prompt: text) -> int = f(prompt)\n"
        'program def main() -> unit = print(apply(ask, "How many?"))\n'
    )

    result = run_source_entry(
        PipelineDriver(resolve_agent_spec=None, agent_dispatcher=answer, get_sandbox_context=None),
        source,
    )

    assert result.ok
    assert len(prompts) == 1
    assert prompts[0].startswith("How many?\n")
    assert capsys.readouterr().out == "42\n"


def test_program_def_entries_execute_via_agm_exec(tmp_path: Path) -> None:
    """Exec runs a sole program and selects a declaration path among several."""
    from click.testing import CliRunner
    from typer.main import get_command

    import agnette.agm.cli as cli

    sole = tmp_path / "sole.agl"
    sole.write_text('program def main() -> unit = print "sole"\n')
    selected = tmp_path / "selected.agl"
    selected.write_text(
        'program def first() -> unit = print "first"\n'
        "\n"
        "scope review\n"
        "\n"
        '  program def main() -> unit = print "review"\n'
        "end review\n"
    )

    runner = CliRunner()
    sole_result = runner.invoke(
        get_command(cli.app), ["exec", "--no-trace", str(sole)], catch_exceptions=False
    )
    selected_result = runner.invoke(
        get_command(cli.app),
        ["exec", "--no-trace", "-p", "review::main", str(selected)],
        catch_exceptions=False,
    )

    assert sole_result.exit_code == 0
    assert sole_result.output == "sole\n"
    assert selected_result.exit_code == 0
    assert selected_result.output == "review\n"
