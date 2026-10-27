"""``@inherit``: parameters defaulting to other commands' configured values."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agnette.agm.cli_support.args import ExecArgs
from agnette.agm.commands.exec import run
from agnette.packages.layout import MODULE_TREE_DIRNAME
from tests._package_helpers import write_installed_package

_BETA_MANIFEST = """\
[package]
name = "beta"
version = "1.0.0"

[aliases]
tr = "tools review"
"""

_BETA_COMMANDS = """\
@param let shared: int = 0

@command("tools review")
program def review(limit: int = 0, label: text = "") -> unit = print shared

@command("tools plan")
program def plan(limit: int = 0, depth: int = 0) -> unit = ()
"""

_ALPHA_MANIFEST = """\
[package]
name = "alpha"
version = "1.0.0"

[dependencies]
beta = { version = "1", path = "../beta" }
"""


def _packages(
    tmp_path: Path,
    main: str,
    *,
    beta_manifest: str = _BETA_MANIFEST,
    beta_commands: str = _BETA_COMMANDS,
    alpha_modules: dict[str, str] | None = None,
) -> Path:
    """Write package ``alpha`` (the entry, depending on ``beta``) and return its entry file."""
    beta = tmp_path / "beta"
    (beta / MODULE_TREE_DIRNAME).mkdir(parents=True)
    (beta / "package.toml").write_text(beta_manifest)
    (beta / MODULE_TREE_DIRNAME / "commands.agl").write_text(beta_commands)
    alpha = tmp_path / "alpha"
    (alpha / MODULE_TREE_DIRNAME).mkdir(parents=True)
    (alpha / "package.toml").write_text(_ALPHA_MANIFEST)
    for name, source in (alpha_modules or {}).items():
        (alpha / MODULE_TREE_DIRNAME / f"{name}.agl").write_text(source)
    entry = alpha / MODULE_TREE_DIRNAME / "main.agl"
    entry.write_text(main)
    return entry


def _main(parameter: str, *, attribute: str = '@inherit("tools.review.limit")') -> str:
    return (
        f'@command("alpha run")\nprogram def main({attribute} {parameter}) -> unit = print limit\n'
    )


def _run(entry: Path, *tokens: str) -> None:
    run(ExecArgs(file=str(entry), argument_tokens=["--no-trace", *tokens]))


def _run_inline(code: str) -> None:
    run(ExecArgs(file=None, code=code, argument_tokens=["--no-trace"]))


def _printed(capsys: pytest.CaptureFixture[str], entry: Path, *tokens: str) -> tuple[str, str]:
    _run(entry, *tokens)
    captured = capsys.readouterr()
    return captured.out, captured.err


class TestTargetLookup:
    def test_a_group_table_of_the_target_supplies_the_default(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools]\nlimit = 4\n")
        entry = _packages(tmp_path, _main("limit: int = 1"))

        assert _printed(capsys, entry) == ("4\n", "")

    def test_the_target_command_table_beats_its_group_table(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools]\nlimit = 4\n\n[tools.review]\nlimit = 6\n")
        entry = _packages(tmp_path, _main("limit: int = 1"))

        assert _printed(capsys, entry)[0] == "6\n"

    @pytest.mark.parametrize(
        ("home_config", "expected"),
        [
            ("", "5\n"),
            ("[tools.review]\nlimit = 6\n", "6\n"),
        ],
        ids=["manifest", "program-route-over-manifest"],
    )
    def test_the_target_packages_manifest_config_is_read_below_its_program_route(
        self,
        home_config: str,
        expected: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home(home_config)
        entry = _packages(
            tmp_path,
            _main("limit: int = 1"),
            beta_manifest=_BETA_MANIFEST + "\n[config.tools.review]\nlimit = 5\n",
        )

        assert _printed(capsys, entry)[0] == expected

    def test_a_manifest_path_value_anchors_at_the_target_package_root(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        entry = _packages(
            tmp_path,
            _main("limit: path = ''", attribute='@inherit("tools.review.label")'),
            beta_manifest=_BETA_MANIFEST + '\n[config.tools.review]\nlabel = "data.txt"\n',
        )

        (tmp_path / "beta" / "data.txt").write_text("")

        assert _printed(capsys, entry) == (f"{tmp_path / 'beta' / 'data.txt'}\n", "")

    def test_the_inheriting_closures_route_tables_do_not_shape_the_targets_read(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        """A table at ``tools.review.limit`` is the target's value, not the closure's route."""
        config_home("[tools.review.limit]\nx = 3\n")
        tools = tmp_path / "tools"
        (tools / MODULE_TREE_DIRNAME).mkdir(parents=True)
        (tools / "package.toml").write_text('[package]\nname = "tools"\nversion = "1.0.0"\n')
        (tools / MODULE_TREE_DIRNAME / "review.agl").write_text(
            "scope limit\n  @param let x: int = 1\nend limit\n"
        )
        entry = _packages(
            tmp_path,
            'import tools/review\n\n@command("alpha run")\n'
            'program def main(@inherit("tools.review.limit") limit: json = {}) -> unit =\n'
            "  print limit\n  print tools/review::limit::x\n",
        )
        alpha = tmp_path / "alpha" / "package.toml"
        alpha.write_text(
            alpha.read_text().replace(
                "[dependencies]", '[dependencies]\ntools = { version = "1", path = "../tools" }'
            )
        )

        out, _ = _printed(capsys, entry)

        assert out == '{"x": 3}\n3\n'

    def test_an_alias_path_addresses_the_target(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\nlimit = 6\n")
        entry = _packages(tmp_path, _main("limit: int = 1", attribute='@inherit("tr.limit")'))

        assert _printed(capsys, entry) == ("6\n", "")

    def test_the_alias_table_of_the_target_is_read(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tr]\nlimit = 7\n")
        entry = _packages(
            tmp_path, _main("limit: int = 1", attribute='@inherit("tools.review.limit")')
        )

        assert _printed(capsys, entry)[0] == "7\n"

    def test_the_longest_registered_prefix_splits_the_address(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home('[tools.review]\n"max.steps" = 9\n')
        entry = _packages(
            tmp_path, _main("limit: int = 1", attribute='@inherit("tools.review.max.steps")')
        )

        assert _printed(capsys, entry)[0] == "9\n"

    def test_the_targets_declaration_route_is_not_read(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[beta.commands]\nshared = 3\n")
        entry = _packages(
            tmp_path, _main("limit: int = 1", attribute='@inherit("tools.review.shared")')
        )

        assert _printed(capsys, entry)[0] == "1\n"

    def test_the_target_command_line_and_defaults_are_not_read(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        entry = _packages(tmp_path, _main("limit: int = 1"))

        assert _printed(capsys, entry)[0] == "1\n"

    def test_inheritance_is_not_transitive(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.plan]\nlimit = 8\n")
        entry = _packages(
            tmp_path,
            _main("limit: int = 1"),
            beta_commands=_BETA_COMMANDS.replace(
                "program def review(limit: int = 0,",
                'program def review(@inherit("tools.plan.limit") limit: int = 0,',
            ),
        )

        assert _printed(capsys, entry)[0] == "1\n"


class TestPrecedence:
    def test_the_command_line_and_the_program_route_win(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\nlimit = 4\n\n[alpha.run]\nlimit = 6\n")
        entry = _packages(tmp_path, _main("limit: int = 1"))

        assert _printed(capsys, entry)[0] == "6\n"
        assert _printed(capsys, entry, "--limit", "8")[0] == "8\n"

    def test_a_positional_value_wins(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\nlimit = 4\n")
        entry = _packages(tmp_path, _main("limit: int"))

        assert _printed(capsys, entry, "8")[0] == "8\n"

    def test_the_inherited_value_beats_config_and_the_inheriting_manifest_and_default(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\nlimit = 4\n")
        entry = _packages(
            tmp_path,
            '@config(main::limit = 2)\n@command("alpha run")\n'
            'program def main(@inherit("tools.review.limit") limit: int = 1) -> unit =\n'
            "  print limit\n",
        )
        manifest = entry.parent.parent / "package.toml"
        manifest.write_text(manifest.read_text() + "\n[config.alpha.run]\nlimit = 3\n")

        assert _printed(capsys, entry)[0] == "4\n"

    def test_config_and_the_manifest_apply_when_nothing_is_inherited(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        entry = _packages(
            tmp_path,
            '@config(main::limit = 2)\n@command("alpha run")\n'
            'program def main(@inherit("tools.review.limit") limit: int = 1) -> unit =\n'
            "  print limit\n",
        )

        assert _printed(capsys, entry)[0] == "2\n"

    def test_the_first_address_that_yields_a_value_wins(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        both = '@inherit("tools.plan.limit", "tools.review.limit")'
        entry = _packages(tmp_path, _main("limit: int = 1", attribute=both))

        config_home("[tools.plan]\nlimit = 3\n\n[tools.review]\nlimit = 4\n")
        assert _printed(capsys, entry)[0] == "3\n"

        config_home("[tools.review]\nlimit = 4\n")
        assert _printed(capsys, entry)[0] == "4\n"


class TestUnregisteredTargets:
    @pytest.mark.parametrize("address", ["nope.limit", "tools.review", "tools.limit"])
    def test_an_address_naming_no_command_warns_and_contributes_nothing(
        self,
        address: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\nlimit = 4\n")
        entry = _packages(tmp_path, _main("limit: int = 1", attribute=f'@inherit("{address}")'))

        out, err = _printed(capsys, entry)

        assert out == "1\n"
        assert address in err

    def test_a_declared_command_group_is_not_a_target(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools]\nlimit = 4\n")
        entry = _packages(
            tmp_path,
            _main("limit: int = 1", attribute='@inherit("tools.limit")'),
            beta_manifest=_BETA_MANIFEST + '\n[commands.tools]\ndoc = "Tools"\n',
        )

        out, err = _printed(capsys, entry)

        assert out == "1\n"
        assert "tools.limit" in err

    def test_a_later_address_still_applies_after_an_unregistered_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\nlimit = 4\n")
        entry = _packages(
            tmp_path,
            _main("limit: int = 1", attribute='@inherit("nope.limit", "tools.review.limit")'),
        )

        out, err = _printed(capsys, entry)

        assert out == "4\n"
        assert "nope.limit" in err

    @pytest.mark.parametrize("covering", [["--limit", "8"], ["8"]], ids=["option", "positional"])
    def test_the_warning_is_emitted_even_when_a_higher_tier_covers_the_parameter(
        self,
        covering: list[str],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        parameter = "limit: int" if covering == ["8"] else "limit: int = 1"
        entry = _packages(tmp_path, _main(parameter, attribute='@inherit("nope.limit")'))

        out, err = _printed(capsys, entry, *covering)

        assert out == "8\n"
        assert "nope.limit" in err

    def test_a_loose_file_program_argument_is_named_as_written(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        entry = tmp_path / "loose.agl"
        entry.write_text(
            'program def main(@inherit("nope.limit") limit: int = 1) -> unit = print limit\n'
        )

        out, err = _printed(capsys, entry)

        assert out == "1\n"
        assert "nope.limit" in err
        assert "<entry>" not in err

    def test_a_package_with_a_malformed_program_reference_is_not_a_target(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\nlimit = 4\n")
        entry = _packages(
            tmp_path,
            _main("limit: int = 1"),
            beta_manifest=_BETA_MANIFEST + '\n[commands."tools review"]\nprogram = "not a ref"\n',
        )

        out, err = _printed(capsys, entry)

        assert out == "1\n"
        assert "tools.review.limit" in err


class TestDecoding:
    def test_the_value_decodes_against_the_inheriting_type(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        """The target's ``label`` is text; the inheriting parameter reads it as an int."""
        config_home('[tools.review]\nlabel = "12"\n')
        entry = _packages(
            tmp_path, _main("limit: int = 1", attribute='@inherit("tools.review.label")')
        )

        assert _printed(capsys, entry)[0] == "12\n"

    def test_a_value_the_inheriting_type_rejects_is_a_configuration_error_naming_both(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home('[tools.review]\nlabel = "many"\n')
        entry = _packages(
            tmp_path, _main("limit: int = 1", attribute='@inherit("tools.review.label")')
        )

        with pytest.raises(SystemExit) as exit_info:
            _run(entry)

        assert exit_info.value.code == 1
        err = capsys.readouterr().err
        assert "limit" in err
        assert "tools.review.label" in err

    def test_a_path_value_anchors_to_its_defining_config_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home('[tools.review]\nlabel = "data.txt"\n')
        (tmp_path / "home" / ".agm" / "data.txt").write_text("")
        entry = _packages(
            tmp_path, _main("limit: path = ''", attribute='@inherit("tools.review.label")')
        )

        out, _ = _printed(capsys, entry)

        assert out == f"{tmp_path / 'home' / '.agm' / 'data.txt'}\n"


class TestParameterKinds:
    def test_a_module_param_and_a_function_param_inherit(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\nlimit = 4\n\n[tools.plan]\ndepth = 6\n")
        entry = _packages(
            tmp_path,
            'import alpha/lib\n\n@command("alpha run")\n'
            "program def main() -> unit =\n  print alpha/lib::width\n  print alpha/lib::reach()\n",
            alpha_modules={
                "lib": (
                    '@param @inherit("tools.review.limit") let width: int = 1\n\n'
                    'def reach(@param @inherit("tools.plan.depth", "tools.review.limit") '
                    "depth: int = 2) -> int = depth\n"
                )
            },
        )

        assert _printed(capsys, entry) == ("4\n6\n", "")

    def test_an_explicit_function_argument_beats_the_inherited_default(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\nlimit = 4\n")
        entry = _packages(
            tmp_path,
            'import alpha/lib\n\n@command("alpha run")\n'
            "program def main() -> unit =\n"
            "  print alpha/lib::reach(9)\n"
            "  print alpha/lib::reach()\n",
            alpha_modules={
                "lib": (
                    'def reach(@param @inherit("tools.review.limit") depth: int = 2) -> int = '
                    "depth\n"
                )
            },
        )

        assert _printed(capsys, entry)[0] == "9\n4\n"

    def test_the_command_line_beats_an_inherited_module_param(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\nlimit = 4\n")
        entry = _packages(
            tmp_path,
            'import alpha/lib\n\n@command("alpha run")\n'
            "program def main() -> unit =\n  print alpha/lib::width\n",
            alpha_modules={"lib": '@param @inherit("tools.review.limit") let width: int = 1\n'},
        )

        assert _printed(capsys, entry)[0] == "4\n"
        assert _printed(capsys, entry, "--width", "7")[0] == "7\n"


class TestInlineSource:
    def test_an_inline_program_inherits_into_an_argument_and_a_module_param(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\nlimit = 4\n")
        write_installed_package(
            tmp_path / "home",
            "beta",
            commands={"tools review": "beta/main::review"},
            source=_BETA_COMMANDS.replace("@command", "#"),
        )

        _run_inline(
            '@param @inherit("tools.review.limit") let width: int = 1\n\n'
            'program def main(@inherit("tools.review.limit") limit: int = 2) -> unit =\n'
            "  print limit\n  print width\n"
        )

        assert capsys.readouterr() == ("4\n4\n", "")
