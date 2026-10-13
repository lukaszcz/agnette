"""The `agm dev definition` command: argument wiring, output, and exit codes."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from typer.main import get_command

import agnette.agm.cli as cli
import agnette.agm.commands.dev.definition as definition_command
from agnette.agm.cli_support.args import DevDefinitionArgs

_AGM_COMMAND = get_command(cli.app)

_LIB = "def double(n: int) -> int = n * 2\n"
_MAIN = "import lib::*\n\nprogram def main() -> unit =\n  print(double(21))\n"


def invoke(argv: list[str], stdin: str | None = None) -> Result:
    return CliRunner().invoke(_AGM_COMMAND, argv, input=stdin, prog_name="agm")


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "lib.agl").write_text(_LIB, encoding="utf-8")
    (tmp_path / "main.agl").write_text(_MAIN, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_arguments_and_flags_map_to_args(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[DevDefinitionArgs] = []
    monkeypatch.setattr(definition_command, "run", calls.append)
    argv = ["dev", "definition", "-I", "a", "--module-path", "b", "--no-stdlib", "--stdin"]
    assert invoke([*argv, "f.agl", "3", "7"]).exit_code == 0
    assert invoke(["dev", "definition", "g.agl", "1", "2"]).exit_code == 0
    assert calls == [
        DevDefinitionArgs("f.agl", 3, 7, module_paths=["a", "b"], no_stdlib=True, stdin=True),
        DevDefinitionArgs("g.agl", 1, 2),
    ]


@pytest.mark.parametrize("tail", [[], ["f.agl"], ["f.agl", "1"], ["f.agl", "x", "1"]])
def test_missing_or_malformed_arguments_are_rejected(
    tail: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[DevDefinitionArgs] = []
    monkeypatch.setattr(definition_command, "run", calls.append)
    assert invoke(["dev", "definition", *tail]).exit_code != 0
    assert calls == []


def test_prints_each_declaration_as_path_line_column_text(project: Path) -> None:
    result = invoke(["dev", "definition", "main.agl", "4", "9"])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"{project / 'lib.agl'}:1:5: {_LIB.rstrip()}\n"


def test_stdin_replaces_the_file_but_imports_resolve_beside_it(project: Path) -> None:
    edited = "\n\n" + _MAIN
    result = invoke(["dev", "definition", "--stdin", "main.agl", "6", "9"], stdin=edited)
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(f"{project / 'lib.agl'}:1:5: ")


def test_module_path_adds_a_search_root(project: Path) -> None:
    library = project / "library"
    library.mkdir()
    (project / "lib.agl").rename(library / "lib.agl")
    assert invoke(["dev", "definition", "main.agl", "4", "9"]).exit_code == 1
    result = invoke(["dev", "definition", "-I", "library", "main.agl", "4", "9"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(f"{library / 'lib.agl'}:1:5: ")


@pytest.mark.parametrize(
    ("argv", "stderr"),
    [
        (["main.agl", "3", "1"], False),  # `program` is a keyword
        (["main.agl", "9", "1"], True),  # past the last line
        (["main.agl", "1", "40"], True),  # past the end of the line
        (["absent.agl", "1", "1"], True),
        (["--no-stdlib", "main.agl", "4", "9"], True),  # `print` is unresolved
    ],
)
def test_exits_1_without_a_declaration(project: Path, argv: list[str], stderr: bool) -> None:
    result = invoke(["dev", "definition", *argv])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert bool(result.stderr) is stderr


def test_invalid_module_roots_exit_1(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agnette.agm.cli_support import exec_roots as exec_roots_module

    def broken(**_: object) -> exec_roots_module.ExecRoots:
        raise ValueError("bad root")

    monkeypatch.setattr(exec_roots_module, "effective_exec_roots", broken)
    result = invoke(["dev", "definition", "main.agl", "4", "9"])
    assert result.exit_code == 1
    assert result.stderr
