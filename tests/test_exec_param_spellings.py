"""Spellings of host parameters across routes: imports, anonymous entries, colliding routes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agnette.agm.cli_support.args import ExecArgs
from agnette.agm.commands.exec import run
from tests._help_helpers import print_exec_help

_TOOLS = "@param let n: int = 10\ndef get() -> int = n\n"

_ENTRY_SCOPE = """\
import tools

scope tools
  @param let n: int = 1
end tools

program def main() -> unit =
  print tools::n
  print tools::get()
"""

_ENTRY_DEF = """\
import tools

def tools(@param n: int = 1) -> int = n

program def main() -> unit =
  print tools()
  print tools::get()
"""


def _library(tmp_path: Path, **modules: str) -> Path:
    root = tmp_path / "lib"
    root.mkdir()
    for name, source in modules.items():
        path = root / f"{name.replace('__', '/')}.agl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


def _entry(tmp_path: Path, source: str) -> Path:
    entry = tmp_path / "main.agl"
    entry.write_text(source)
    return entry


def _exec(entry: Path, library: Path, *tokens: str) -> None:
    run(
        ExecArgs(
            file=str(entry),
            argument_tokens=["--no-trace", *tokens],
            module_paths=[str(library)],
        )
    )


@pytest.mark.parametrize("source", [_ENTRY_SCOPE, _ENTRY_DEF], ids=["scope", "def"])
class TestEntryNeverTakesAnImportedModulesSpelling:
    def test_the_imported_modules_full_spelling_sets_its_parameter(
        self,
        source: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        library = _library(tmp_path, tools=_TOOLS)

        _exec(_entry(tmp_path, source), library, "--tools.n", "5")

        assert capsys.readouterr().out == "1\n5\n"

    def test_help_lists_the_imported_modules_parameters(
        self,
        source: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        library = _library(tmp_path, tools=_TOOLS)

        assert print_exec_help(
            tokens=["--help"],
            file=str(_entry(tmp_path, source)),
            code=None,
            module_paths=[str(library)],
        )

        assert "Parameters of tools" in capsys.readouterr().out

    def test_the_program_table_sets_the_imported_modules_parameter(
        self,
        source: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home('[main.main]\n"tools.n" = 7\n')
        library = _library(tmp_path, tools=_TOOLS)

        _exec(_entry(tmp_path, source), library)

        assert capsys.readouterr().out == "1\n7\n"


_COLLIDING_MAIN = """\
import a
import a/refine

program def main() -> unit =
  print a::refine()
  print a/refine::get()
"""

_COLLIDING_MODULE = "@param let x: int = 2\ndef get() -> int = x\n"

_COLLIDING_ROUTES = {
    "def": "def refine(@param x: int = 1) -> int = x\n",
    "scope": "scope refine\n  @param let x: int = 1\nend refine\n",
}


class TestCollidingRoutesAreRejectedStatically:
    """Named-module parameters of one program closure may not share a full spelling."""

    def test_a_function_route_and_a_module_route_collide(
        self, tmp_path: Path, config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        library = _library(tmp_path, a=_COLLIDING_ROUTES["def"], a__refine=_COLLIDING_MODULE)

        with pytest.raises(SystemExit) as exit_info:
            _exec(_entry(tmp_path, _COLLIDING_MAIN), library)

        assert exit_info.value.code == 1

    def test_a_scope_route_and_a_module_route_collide(
        self, tmp_path: Path, config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        library = _library(
            tmp_path,
            a=_COLLIDING_ROUTES["scope"],
            a__refine=_COLLIDING_MODULE,
        )
        main = _COLLIDING_MAIN.replace("print a::refine()", "print a::refine::x")

        with pytest.raises(SystemExit) as exit_info:
            _exec(_entry(tmp_path, main), library)

        assert exit_info.value.code == 1

    def test_distinct_routes_run(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        library = _library(
            tmp_path,
            a=_COLLIDING_ROUTES["def"].replace("refine", "tune"),
            a__refine=_COLLIDING_MODULE,
        )
        main = _COLLIDING_MAIN.replace("a::refine()", "a::tune()")

        _exec(_entry(tmp_path, main), library, "--a.refine.x", "5")

        assert capsys.readouterr().out == "1\n5\n"
