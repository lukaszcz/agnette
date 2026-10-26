"""Package-aware shadowing of same-named host parameters through ``agm exec``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agnette.agm.cli_support.args import ExecArgs
from agnette.agm.commands import exec_program as exec_engine
from agnette.agm.commands.exec import run
from agnette.config.context import ConfigContext
from agnette.packages.layout import MODULE_TREE_DIRNAME
from tests._help_helpers import assert_lists_engine_settings, print_exec_help

_PARAM = "@param let verbose: bool = false\n"


def _package(
    root: Path, name: str, modules: dict[str, str], dependencies: tuple[str, ...] = ()
) -> Path:
    """Write package *name* under *root*, with a path dependency on each of *dependencies*."""
    package = root / name
    (package / MODULE_TREE_DIRNAME).mkdir(parents=True)
    manifest = f'[package]\nname = "{name}"\nversion = "1.0.0"\n'
    if dependencies:
        manifest += "\n[dependencies]\n"
        manifest += "".join(
            f'{dep} = {{ version = "1", path = "../{dep}" }}\n' for dep in dependencies
        )
    (package / "package.toml").write_text(manifest)
    for module, source in modules.items():
        (package / MODULE_TREE_DIRNAME / f"{module}.agl").write_text(source)
    return package / MODULE_TREE_DIRNAME / "main.agl"


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Return a setter for the isolated home config file's contents."""
    home = tmp_path / "home"
    (home / ".agm").mkdir(parents=True)
    monkeypatch.setattr(
        exec_engine,
        "current_config_context",
        lambda: ConfigContext(home=home, proj_dir=None, cwd=tmp_path),
    )

    def write(contents: str) -> None:
        (home / ".agm" / "config.toml").write_text(contents)

    return write


def _exec(entry: Path, *tokens: str, module_paths: tuple[Path, ...] = ()) -> None:
    run(
        ExecArgs(
            file=str(entry),
            argument_tokens=["--no-trace", *list(tokens)],
            module_paths=[str(path) for path in module_paths],
        )
    )


_NEAR_AND_FAR = (
    "import near\nimport far\n"
    "program def main() -> unit =\n  print near::verbose\n  print far::verbose\n"
)


def _shadowed_by_a_dependency(tmp_path: Path) -> Path:
    _package(tmp_path, "bravo", {"shared": _PARAM})
    return _package(
        tmp_path,
        "alpha",
        {
            "helper": _PARAM,
            "main": (
                "import alpha/helper\nimport bravo/shared\n"
                "program def main() -> unit =\n"
                "  print alpha/helper::verbose\n"
                "  print bravo/shared::verbose\n"
            ),
        },
        ("bravo",),
    )


def _peer_dependencies(tmp_path: Path) -> Path:
    _package(tmp_path, "bravo", {"shared": _PARAM})
    _package(tmp_path, "charlie", {"shared": _PARAM})
    return _package(
        tmp_path,
        "alpha",
        {
            "main": (
                "import bravo/shared\nimport charlie/shared\n"
                "program def main() -> unit =\n"
                "  print bravo/shared::verbose\n"
                "  print charlie/shared::verbose\n"
            ),
        },
        ("bravo", "charlie"),
    )


class TestOwnerShadowing:
    def test_own_package_parameter_wins_the_bare_spelling_over_a_dependency(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        entry = _shadowed_by_a_dependency(tmp_path)

        _exec(entry, "--verbose")

        assert capsys.readouterr().out == "true\nfalse\n"

    def test_shadowed_dependency_parameter_stays_reachable_qualified(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        entry = _shadowed_by_a_dependency(tmp_path)

        _exec(entry, "--bravo.shared.verbose")

        assert capsys.readouterr().out == "false\ntrue\n"

    def test_loose_root_owner_wins_like_a_package_owner(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        project = tmp_path / "project"
        library = tmp_path / "library"
        project.mkdir()
        library.mkdir()
        (project / "near.agl").write_text(_PARAM)
        (library / "far.agl").write_text(_PARAM)
        entry = project / "main.agl"
        entry.write_text(
            "import near\nimport far\n"
            "program def main() -> unit =\n  print near::verbose\n  print far::verbose\n"
        )

        _exec(entry, "--verbose", module_paths=(library,))
        assert capsys.readouterr().out == "true\nfalse\n"
        _exec(entry, "--far.verbose", module_paths=(library,))
        assert capsys.readouterr().out == "false\ntrue\n"

    def test_config_route_sets_only_the_own_package_parameter(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        entry = _shadowed_by_a_dependency(tmp_path)

        config_home("[alpha.main.main]\nverbose = true\n")
        _exec(entry)
        assert capsys.readouterr().out == "true\nfalse\n"

        config_home('[alpha.main.main]\n"bravo.shared.verbose" = true\n')
        _exec(entry)
        assert capsys.readouterr().out == "false\ntrue\n"

    def test_a_symlinked_loose_module_is_owned_by_the_root_holding_the_link(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        project = tmp_path / "project"
        library = tmp_path / "library"
        elsewhere = tmp_path / "elsewhere"
        for directory in (project, library, elsewhere):
            directory.mkdir()
        (elsewhere / "real.agl").write_text(_PARAM)
        (project / "near.agl").symlink_to(elsewhere / "real.agl")
        (library / "far.agl").write_text(_PARAM)
        entry = project / "main.agl"
        entry.write_text(_NEAR_AND_FAR)

        _exec(entry, "--verbose", module_paths=(library,))

        assert capsys.readouterr().out == "true\nfalse\n"

    def test_a_module_owner_is_the_root_it_resolved_through_not_the_innermost_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        project = tmp_path / "project"
        nested = project / "lib"
        library = tmp_path / "library"
        nested.mkdir(parents=True)
        library.mkdir()
        (nested / "near.agl").write_text(_PARAM)
        (library / "far.agl").write_text(_PARAM)
        entry = project / "main.agl"
        entry.write_text(
            _NEAR_AND_FAR.replace("import near", "import lib/near", 1).replace(
                "near::", "lib/near::"
            )
        )

        _exec(entry, "--verbose", module_paths=(library, nested))

        assert capsys.readouterr().out == "true\nfalse\n"

    def test_inline_code_owns_the_helpers_in_its_working_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        (tmp_path / "helper.agl").write_text('@param @opt-name("log-level") let lvl: text = "h"\n')

        run(
            ExecArgs(
                file=None,
                code=(
                    "import helper\nimport std/log\n"
                    "program def main() -> unit = print helper::lvl\n"
                ),
                argument_tokens=["--no-trace", "--log-level", "x"],
            )
        )

        assert capsys.readouterr().out == "x\n"


class TestSameLevelPeers:
    def test_bare_spelling_of_peers_is_an_error_only_when_used(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        entry = _peer_dependencies(tmp_path)

        _exec(entry)
        assert capsys.readouterr().out == "false\nfalse\n"
        with pytest.raises(SystemExit):
            _exec(entry, "--verbose")

    def test_qualified_spellings_of_peers_resolve(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        entry = _peer_dependencies(tmp_path)

        _exec(entry, "--bravo.shared.verbose")

        assert capsys.readouterr().out == "true\nfalse\n"

    def test_bare_config_leaf_of_peers_is_an_error_only_when_used(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        entry = _peer_dependencies(tmp_path)

        config_home('[alpha.main.main]\n"bravo.shared.verbose" = true\n')
        _exec(entry)
        assert capsys.readouterr().out == "true\nfalse\n"

        config_home("[alpha.main.main]\nverbose = true\n")
        with pytest.raises(SystemExit):
            _exec(entry)


class TestHelp:
    def test_shadowed_parameter_is_listed_only_under_its_qualified_spelling(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        entry = _shadowed_by_a_dependency(tmp_path)

        assert print_exec_help(tokens=["--help"], file=str(entry), code=None)

        output = capsys.readouterr().out
        own, _, shadowed = output.partition("Parameters of bravo/shared")
        assert "Parameters of alpha/helper" in own
        assert "--verbose" in own
        assert "--bravo.shared.verbose" not in own
        assert "--shared.verbose" in shadowed
        assert "--verbose " not in shadowed


_ENGINE_SHADOW = (
    "import std/config\n"
    'program def main(timeout: text = "dflt") -> unit =\n'
    "  print timeout\n"
    "  print std/config::timeout\n"
)


def _engine_entry(tmp_path: Path, source: str = _ENGINE_SHADOW) -> Path:
    entry = tmp_path / "main.agl"
    entry.write_text(source)
    return entry


class TestEngineSettingShadowing:
    """A same-named program or module parameter shadows the bare engine-setting flag."""

    def _lines(self, capsys: pytest.CaptureFixture[str], entry: Path, *tokens: str) -> list[str]:
        run(ExecArgs(file=str(entry), argument_tokens=["--no-trace", *tokens]))
        return capsys.readouterr().out.splitlines()

    def test_the_bare_flag_feeds_the_program_argument(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        entry = _engine_entry(tmp_path)

        assert self._lines(capsys, entry, "--timeout", "7") == ["7", "Option::None"]

    def test_the_engine_setting_stays_settable_qualified(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        entry = _engine_entry(tmp_path)

        assert self._lines(capsys, entry, "--std.config.timeout", "5s") == [
            "dflt",
            'Option::Some(value = "5s")',
        ]
        assert self._lines(capsys, entry, "--no-std.config.timeout") == ["dflt", "Option::None"]

    def test_the_negated_bare_flag_is_not_left_live_for_the_shadowed_setting(
        self, tmp_path: Path, config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        entry = _engine_entry(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            run(ExecArgs(file=str(entry), argument_tokens=["--no-trace", "--no-timeout"]))

        assert exc_info.value.code != 0

    def test_the_exec_table_sets_the_engine_setting_and_leaves_the_argument(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home('[exec]\ntimeout = "9s"\n')
        entry = _engine_entry(tmp_path)

        assert self._lines(capsys, entry) == ["dflt", 'Option::Some(value = "9s")']

    def test_the_program_table_feeds_the_argument_not_the_engine_setting(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home('[main.main]\ntimeout = "tabled"\n')
        entry = _engine_entry(tmp_path)

        assert self._lines(capsys, entry) == ["tabled", "Option::None"]

    def test_an_unshadowed_engine_flag_sets_the_engine_setting(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home('[exec]\ntimeout = "9s"\n')
        entry = _engine_entry(
            tmp_path,
            "import std/config\nprogram def main() -> unit =\n  print std/config::timeout\n",
        )

        assert self._lines(capsys, entry, "--timeout", "5s") == ['Option::Some(value = "5s")']
        assert self._lines(capsys, entry, "--no-timeout") == ["Option::None"]
        assert self._lines(capsys, entry) == ['Option::Some(value = "9s")']

    def test_a_negated_boolean_engine_flag_sets_the_engine_setting(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("[exec]\nstrict-json = true\n")
        entry = _engine_entry(
            tmp_path,
            "import std/config\nprogram def main() -> unit =\n  print std/config::strict-json\n",
        )

        assert self._lines(capsys, entry) == ["true"]
        assert self._lines(capsys, entry, "--no-strict-json") == ["false"]

    def test_a_module_parameter_shadows_the_engine_setting_at_its_level(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        modules = tmp_path / "modules"
        modules.mkdir()
        (modules / "helper.agl").write_text('@param let timeout: text = "module"\n')
        entry = _engine_entry(
            tmp_path,
            "import helper\nimport std/config\n"
            "program def main() -> unit =\n"
            "  print helper::timeout\n"
            "  print std/config::timeout\n",
        )

        run(
            ExecArgs(
                file=str(entry),
                argument_tokens=["--no-trace", "--timeout", "7"],
                module_paths=[str(modules)],
            )
        )

        assert capsys.readouterr().out.splitlines() == ["7", "Option::None"]

    def test_help_lists_the_shadowed_engine_setting_qualified_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        entry = _engine_entry(tmp_path)

        assert print_exec_help(tokens=["--help"], file=str(entry), code=None)

        output = capsys.readouterr().out
        assert_lists_engine_settings(output, qualified=("timeout",))
