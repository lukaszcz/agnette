"""One warning policy per invocation: every driver a command creates is governed by it."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import agnette.agm.commands.check as check_command
from agnette.agl.warning_policy import WarningPolicy
from agnette.agm.cli_support.args import CheckArgs, ExecArgs
from agnette.agm.cli_support.program_discovery import ExecProgramDiscovery
from agnette.agm.commands import exec_program as exec_engine
from agnette.config.context import ConfigContext

_DENY_TAB = WarningPolicy({"tab-indentation": "deny"})
_PROGRAM = "# a comment carrying a TAB:\there\nprogram def main() -> unit = ()\n"


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "main.agl"
    source.write_text(_PROGRAM, encoding="utf-8")
    return source


def _exec_args(source: Path) -> ExecArgs:
    return ExecArgs(
        file=str(source), strict_json=None, no_trace=True, trace_file=None, no_stdlib=True
    )


@pytest.fixture(autouse=True)
def _isolated_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / ".agm").mkdir(parents=True)
    context = ConfigContext(home=home, proj_dir=None, cwd=tmp_path)
    monkeypatch.setattr(exec_engine, "current_config_context", lambda: context)
    monkeypatch.setattr("agnette.config.context.current_config_context", lambda: context)


def _discover(policy: WarningPolicy | None) -> ExecProgramDiscovery:
    return ExecProgramDiscovery(
        code=None,
        requested_program=None,
        module_paths=None,
        no_stdlib=True,
        warning_policy=policy,
    )


def test_discovery_is_governed_by_the_invocations_policy(tmp_path: Path) -> None:
    source = _source(tmp_path)

    default = _discover(None).artifacts(str(source))
    denied = _discover(_DENY_TAB).artifacts(str(source))

    assert default is not None and denied is not None
    assert [d.warning_name for d in default.discovery.warnings] == ["tab-indentation"]
    assert default.discovery.diagnostics == ()
    assert [d.warning_name for d in denied.discovery.diagnostics] == ["tab-indentation"]
    assert denied.discovery.warnings == ()


def test_exec_denies_a_warning_and_runs_nothing(tmp_path: Path) -> None:
    source = _source(tmp_path)

    exec_engine.run(_exec_args(source))
    with pytest.raises(SystemExit) as raised:
        exec_engine.run(_exec_args(source), warning_policy=_DENY_TAB)

    assert raised.value.code == 1


def test_exec_never_reuses_discovery_governed_by_another_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    cached = _discover(None).artifacts(str(source))
    args = replace(_exec_args(source), pipeline_cache=cached)

    exec_engine.run(args, warning_policy=WarningPolicy({"tab-indentation": "allow"}))

    assert capsys.readouterr().err == ""


def test_check_denies_a_warning(tmp_path: Path) -> None:
    source = _source(tmp_path)

    check_command.run(CheckArgs(files=[str(source)], no_stdlib=True))
    with pytest.raises(SystemExit) as raised:
        check_command.run(CheckArgs(files=[str(source)], no_stdlib=True), warning_policy=_DENY_TAB)

    assert raised.value.code == 1


def test_a_registered_command_of_a_store_package_is_capped_even_when_denied(
    tmp_path: Path,
) -> None:
    from agnette.agm.cli_support.program_discovery import ProgramDiscoveryArtifacts
    from agnette.agm.commands.exec_program import registered_program_declaration
    from tests._package_helpers import write_installed_package

    write_installed_package(tmp_path, "tools", source=_PROGRAM)
    context = ConfigContext(home=tmp_path, proj_dir=None, cwd=tmp_path)

    def discover(policy: WarningPolicy | None) -> ProgramDiscoveryArtifacts:
        sink: list[ProgramDiscoveryArtifacts] = []
        registered_program_declaration(
            "tools/main::main",
            "tools",
            context=context,
            artifact_sink=sink,
            warning_policy=policy,
        )
        return sink[0]

    default = discover(None)
    denied = discover(_DENY_TAB)

    assert default.discovery.warnings == ()
    assert denied.discovery.diagnostics == ()
    assert denied.discovery.warnings == ()
    assert denied.warning_policy == _DENY_TAB
