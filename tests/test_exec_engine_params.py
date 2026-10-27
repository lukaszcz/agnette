"""Engine settings resolve as host parameters on ``agm exec``.

One chain ranks every source: CLI > program table > ``@inherit`` > ``@config`` >
package manifest ``[config]`` > ``[exec]`` > the ``builtin var`` initializer. The
derived ``trace`` setting follows from the winning tiers of ``trace`` and ``trace-file``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

import agnette.agm.commands.exec as exec_command
from agnette.agm.cli_support.args import ExecArgs
from agnette.agm.commands import exec_program
from agnette.packages.layout import MODULE_TREE_DIRNAME

_MANIFEST = """\
[package]
name = "eng"
version = "1.0.0"
{config}"""


def _entry(
    tmp_path: Path,
    *,
    source_config: str = "",
    manifest_config: str = "",
    setting: str = "timeout",
) -> Path:
    """Write package ``eng`` whose ``eng run`` program prints the *setting* engine setting."""
    root = tmp_path / "eng"
    (root / MODULE_TREE_DIRNAME).mkdir(parents=True)
    (root / "package.toml").write_text(_MANIFEST.format(config=manifest_config))
    entry = root / MODULE_TREE_DIRNAME / "main.agl"
    entry.write_text(
        "import std/config\n\n"
        f'{source_config}@command("eng run")\n'
        f"program def main() -> unit = print(config::{setting})\n"
    )
    return entry


def _printed(entry: Path, capsys: pytest.CaptureFixture[str], *tokens: str) -> str:
    exec_command.run(ExecArgs(file=str(entry), argument_tokens=["--no-trace", *tokens]))
    return capsys.readouterr().out


def _some(text: str) -> str:
    return f'Option::Some(value = "{text}")\n'


_EXEC = "[exec]\ntimeout = '1s'\n"
_MANIFEST_CONFIG = '\n[config]\ntimeout = "2s"\n'
_ATTRIBUTE = '@config(config::timeout = Some("3s"))\n'
_TABLE = "[eng.run]\ntimeout = '4s'\n"
_CLI = ("--timeout", "5s")


class TestEngineSettingPrecedence:
    """Each source overrides every one below it; a missing source falls through."""

    @pytest.mark.parametrize(
        ("exec_table", "manifest", "attribute", "table", "cli", "expected"),
        [
            (_EXEC, _MANIFEST_CONFIG, _ATTRIBUTE, _TABLE, _CLI, "5s"),
            (_EXEC, _MANIFEST_CONFIG, _ATTRIBUTE, _TABLE, (), "4s"),
            (_EXEC, _MANIFEST_CONFIG, _ATTRIBUTE, "", (), "3s"),
            (_EXEC, _MANIFEST_CONFIG, "", "", (), "2s"),
            (_EXEC, "", "", "", (), "1s"),
        ],
        ids=["cli", "program-table", "config-attribute", "manifest", "exec-table"],
    )
    def test_the_highest_source_wins(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
        exec_table: str,
        manifest: str,
        attribute: str,
        table: str,
        cli: tuple[str, ...],
        expected: str,
    ) -> None:
        config_home(exec_table + table)
        entry = _entry(tmp_path, source_config=attribute, manifest_config=manifest)

        assert _printed(entry, capsys, *cli) == _some(expected)

    def test_a_setting_no_source_sets_keeps_its_initializer(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")

        assert _printed(_entry(tmp_path), capsys) == "Option::None\n"

    def test_the_command_line_may_unset_what_a_lower_source_sets(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home(_EXEC)
        entry = _entry(tmp_path, manifest_config=_MANIFEST_CONFIG)

        assert _printed(entry, capsys, "--no-timeout") == "Option::None\n"

    def test_a_blank_program_table_value_unsets_what_a_lower_source_sets(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home(_EXEC + "[eng.run]\ntimeout = ''\n")
        entry = _entry(tmp_path, manifest_config=_MANIFEST_CONFIG)

        assert _printed(entry, capsys) == "Option::None\n"

    def test_a_blank_program_table_value_masks_a_config_attribute(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[eng.run]\ntimeout = ''\n")
        entry = _entry(tmp_path, source_config=_ATTRIBUTE)

        assert _printed(entry, capsys) == "Option::None\n"


class TestTableValuedEngineSettings:
    """A table value is a native value: a higher config layer replaces it wholesale."""

    _SANDBOX = (
        '[exec.sandbox]\n"$case" = "Sandbox"\npatch = {patch}\n\n'
        '[exec.sandbox.memory]\n"$case" = "Some"\nvalue = "8G"\n'
    )

    def test_a_higher_layer_replaces_the_table_instead_of_merging_into_it(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home(self._SANDBOX.format(patch="false"))
        cwd_config = tmp_path / ".agm" / "config.toml"
        cwd_config.parent.mkdir()
        cwd_config.write_text('[exec.sandbox]\n"$case" = "Sandbox"\npatch = true\n')
        entry = _entry(tmp_path)
        entry.write_text(
            'import std/config\n\n@command("eng run")\n'
            "program def main() -> unit = print(config::sandbox)\n"
        )

        printed = _printed(entry, capsys)

        assert "patch = true" in printed
        assert "8G" not in printed


class TestEngineSettingInherit:
    """An engine setting has no ``@inherit`` site; its name is an ordinary leaf elsewhere."""

    def test_another_commands_engine_setting_is_inheritable_into_an_ordinary_parameter(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[tools.review]\ntimeout = '7s'\n")
        beta = tmp_path / "beta"
        (beta / MODULE_TREE_DIRNAME).mkdir(parents=True)
        (beta / "package.toml").write_text('[package]\nname = "beta"\nversion = "1.0.0"\n')
        (beta / MODULE_TREE_DIRNAME / "commands.agl").write_text(
            '@command("tools review")\nprogram def review() -> unit = ()\n'
        )
        root = tmp_path / "alpha"
        (root / MODULE_TREE_DIRNAME).mkdir(parents=True)
        (root / "package.toml").write_text(
            '[package]\nname = "alpha"\nversion = "1.0.0"\n\n'
            '[dependencies]\nbeta = { version = "1", path = "../beta" }\n'
        )
        entry = root / MODULE_TREE_DIRNAME / "main.agl"
        entry.write_text(
            "import std/config\n\n"
            '@command("alpha run")\n'
            'program def main(@inherit("tools.review.timeout") wait: text = "none") -> unit =\n'
            "  print wait\n"
            "  print(config::timeout)\n"
        )

        assert _printed(entry, capsys) == "7s\nOption::None\n"


class TestTraceFollowsTheWinningTiers:
    """``trace`` is derived from ``trace``/``trace-file``; a supplied one decides alone."""

    _SOURCE = (
        "import std/config\n\n{attribute}"
        '@command("eng run")\n'
        "program def main() -> unit = print(config::trace)\n"
    )

    def _run(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        *,
        tokens: tuple[str, ...] = (),
        attribute: str = "",
    ) -> str:
        entry = _entry(tmp_path)
        entry.write_text(self._SOURCE.format(attribute=attribute))
        exec_command.run(ExecArgs(file=str(entry), argument_tokens=list(tokens)))
        return capsys.readouterr().out

    def test_a_higher_tier_trace_file_implies_tracing_over_a_lower_trace_false(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        trace_file = tmp_path / "program.jsonl"
        config_home(f"[exec]\ntrace = false\n\n[eng.run]\ntrace-file = '{trace_file}'\n")

        assert self._run(tmp_path, capsys) == "true\n"
        assert trace_file.exists()

    def test_a_config_attribute_trace_file_implies_tracing_over_the_exec_table(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        trace_file = tmp_path / "attribute.jsonl"
        config_home("[exec]\ntrace = false\n")
        attribute = f'@config(config::trace-file = Some("{trace_file}"))\n'

        assert self._run(tmp_path, capsys, attribute=attribute) == "true\n"
        assert trace_file.exists()

    def test_a_configured_trace_file_implies_tracing_beneath_a_higher_trace_false(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        trace_file = tmp_path / "exec.jsonl"
        config_home(f"[exec]\ntrace-file = '{trace_file}'\n\n[eng.run]\ntrace = false\n")

        assert self._run(tmp_path, capsys) == "true\n"
        assert trace_file.exists()

    def test_a_supplied_no_trace_beats_a_configured_trace_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        trace_file = tmp_path / "exec.jsonl"
        config_home(f"[exec]\ntrace-file = '{trace_file}'\n")

        assert self._run(tmp_path, capsys, tokens=("--no-trace",)) == "false\n"
        assert not trace_file.exists()

    def test_a_supplied_no_trace_beats_a_config_attribute_trace_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
    ) -> None:
        trace_file = tmp_path / "attribute.jsonl"
        config_home("")
        attribute = f'@config(config::trace-file = Some("{trace_file}"))\n'

        assert self._run(tmp_path, capsys, tokens=("--no-trace",), attribute=attribute) == "false\n"
        assert not trace_file.exists()

    def test_a_supplied_trace_file_beats_a_configured_trace_false(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        trace_file = tmp_path / "cli.jsonl"
        config_home("[exec]\ntrace = false\n\n[eng.run]\ntrace = false\n")

        assert self._run(tmp_path, capsys, tokens=("--trace-file", str(trace_file))) == "true\n"
        assert trace_file.exists()

    def test_a_blank_configured_trace_file_is_a_configured_trace_setting(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[exec]\ndebug = true\ntrace-file = ''\n")

        assert self._run(tmp_path, capsys) == "false\n"

    def test_debug_traces_when_no_trace_setting_is_configured(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[exec]\ndebug = true\n")

        assert self._run(tmp_path, capsys) == "true\n"

    def test_a_config_attribute_none_trace_file_masks_a_lower_trace_file(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        trace_file = tmp_path / "exec.jsonl"
        config_home(f"[exec]\ntrace-file = '{trace_file}'\n")
        attribute = "@config(config::trace-file = None)\n"

        assert self._run(tmp_path, capsys, attribute=attribute) == "false\n"
        assert not trace_file.exists()


class TestEngineSettingPaths:
    """A relative ``[exec]`` path anchors to the config layer's directory."""

    def test_a_relative_trace_file_anchors_to_its_config_directory(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[exec]\ntrace-file = 'layer.jsonl'\n")
        anchored = tmp_path / "home" / ".agm" / "layer.jsonl"
        anchored.touch()
        entry = _entry(tmp_path, setting="trace-file")

        assert _printed(entry, capsys) == _some(str(anchored))


class TestEngineSettingFailures:
    """A bad value the command line supplied is a usage error; any other exits 1."""

    def test_an_invalid_command_line_value_is_a_usage_error(
        self, tmp_path: Path, config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        entry = _entry(tmp_path)

        with pytest.raises(exec_program.CommandUsageError):
            exec_program.run(
                ExecArgs(file=str(entry), argument_tokens=["--no-trace", "--timeout", "forever"])
            )

    @pytest.mark.parametrize(
        ("exec_table", "manifest", "attribute"),
        [
            ("[exec]\ntimeout = 'forever'\n", "", ""),
            ("", '\n[config]\ntimeout = "forever"\n', ""),
            ("[eng.run]\ntimeout = 'forever'\n", "", ""),
            ("", "", '@config(config::timeout = Some("forever"))\n'),
        ],
        ids=["exec-table", "manifest", "program-table", "config"],
    )
    def test_an_invalid_configured_value_exits_1(
        self,
        tmp_path: Path,
        config_home: Callable[[str], None],
        exec_table: str,
        manifest: str,
        attribute: str,
    ) -> None:
        config_home(exec_table)
        entry = _entry(tmp_path, source_config=attribute, manifest_config=manifest)

        with pytest.raises(SystemExit) as exc_info:
            exec_program.run(ExecArgs(file=str(entry), argument_tokens=["--no-trace"]))

        assert exc_info.value.code == 1

    def test_an_invalid_configured_value_is_reported_without_a_source_location(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[exec]\ntimeout = 'forever'\n")
        entry = _entry(tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            exec_program.run(ExecArgs(file=str(entry), argument_tokens=["--no-trace"]))

        assert exc_info.value.code == 1
        assert re.search(r":\d+:\d+", capsys.readouterr().err) is None

    def test_an_overridden_config_attribute_value_is_still_validated(
        self, tmp_path: Path, config_home: Callable[[str], None]
    ) -> None:
        config_home("")
        entry = _entry(tmp_path, source_config='@config(config::timeout = Some("forever"))\n')

        with pytest.raises(SystemExit) as exc_info:
            exec_program.run(
                ExecArgs(file=str(entry), argument_tokens=["--no-trace", "--timeout", "5s"])
            )

        assert exc_info.value.code == 1
