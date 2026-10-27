"""Tests for general config and sandbox utility helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.runtime.arguments import option_none_raw
from agnette.config.general import (
    _optional_bool,
    load_merged_config,
    load_run_config,
)
from agnette.config.sandbox.srt import sandbox_settings_candidates


def test_load_run_config_merges_global_and_local_sections(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".agm").mkdir()
    (home / ".agm" / "config.toml").write_text(
        "\n".join(
            [
                "[run]",
                'memory = "20G"',
                'swap = "1G"',
                "pty = false",
                "",
                "[run.echo]",
                'alias = "printf"',
                'memory = "10G"',
                'swap = "2G"',
                "pty = true",
                "",
                "[run.keep]",
                'alias = "cat"',
                "",
            ]
        )
    )

    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "config" / "config.toml").write_text(
        "\n".join(
            [
                "[run.echo]",
                'alias = "cat"',
                'memory = "5G"',
                'swap = "512M"',
                "",
                "[run.local]",
                'alias = "sed"',
                "",
            ]
        )
    )

    config = load_run_config(home=home, proj_dir=project, cwd=tmp_path / "work")

    assert config.alias_for("echo") == "cat"
    assert config.alias_for("keep") == "cat"
    assert config.alias_for("local") == "sed"
    assert config.alias_for("missing") is None
    assert config.memory_limit_for("echo") == "5G"
    assert config.memory_limit_for("keep") == "20G"
    assert config.memory_limit_for("local") == "20G"
    assert config.memory_limit_for("missing") == "20G"
    assert config.swap_limit_for("echo") == "512M"
    assert config.swap_limit_for("keep") == "1G"
    assert config.swap_limit_for("local") == "1G"
    assert config.swap_limit_for("missing") == "1G"
    assert config.pty_for("echo") is True
    assert config.pty_for("keep") is False
    assert config.pty_for("missing") is False


def test_load_run_config_treats_empty_text_settings_as_absent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".agm").mkdir(parents=True)
    (home / ".agm" / "config.toml").write_text(
        '[run]\nmemory = ""\nswap = ""\n[run.echo]\nalias = ""\nmemory = ""\n',
        encoding="utf-8",
    )

    config = load_run_config(home=home, proj_dir=None, cwd=tmp_path / "work")

    assert config.alias_for("echo") is None
    assert config.memory_limit_for("echo") is None
    assert config.swap_limit_for("echo") is None


def test_load_run_config_enables_pty_by_default(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    config = load_run_config(home=home, proj_dir=None, cwd=tmp_path / "work")

    assert config.pty_for("anything") is True


def test_load_run_config_prefers_dot_agm_config_after_project_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "config" / "config.toml").write_text('[run.echo]\nalias = "printf"\n')

    work = tmp_path / "work"
    (work / ".agm").mkdir(parents=True)
    (work / ".agm" / "config.toml").write_text('[run.echo]\nalias = "cat"\n')

    config = load_run_config(home=home, proj_dir=project, cwd=work)

    assert config.alias_for("echo") == "cat"


def test_load_run_config_prefers_home_over_install_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "prefix"
    (prefix / ".agm").mkdir(parents=True)
    (prefix / ".agm" / "config.toml").write_text('[run.echo]\nalias = "printf"\n')

    home = tmp_path / "home"
    (home / ".agm").mkdir(parents=True)
    (home / ".agm" / "config.toml").write_text('[run.echo]\nalias = "cat"\n')

    monkeypatch.setattr("agnette.config.home.agm_installation_prefix", lambda: prefix)

    config = load_run_config(home=home, proj_dir=None, cwd=tmp_path / "work")

    assert config.alias_for("echo") == "cat"


def test_load_run_config_falls_back_to_home_when_install_prefix_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".agm").mkdir(parents=True)
    (home / ".agm" / "config.toml").write_text('[run.echo]\nalias = "printf"\n')

    monkeypatch.setattr("agnette.config.home.agm_installation_prefix", lambda: tmp_path / "prefix")

    config = load_run_config(home=home, proj_dir=None, cwd=tmp_path / "work")

    assert config.alias_for("echo") == "printf"


def test_load_run_config_prefers_dot_agm_memory_after_project_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "config" / "config.toml").write_text(
        '[run]\nmemory = "10G"\nswap = "2G"\n[run.echo]\nmemory = "5G"\nswap = "1G"\n'
    )

    work = tmp_path / "work"
    (work / ".agm").mkdir(parents=True)
    (work / ".agm" / "config.toml").write_text('[run.echo]\nmemory = "2G"\nswap = "256M"\n')

    config = load_run_config(home=home, proj_dir=project, cwd=work)

    assert config.memory_limit_for("echo") == "2G"
    assert config.memory_limit_for("other") == "10G"
    assert config.swap_limit_for("echo") == "256M"
    assert config.swap_limit_for("other") == "2G"


# --- Path resolution and env var expansion in config file paths ---


def test_sandbox_settings_candidates_fall_back_to_alias_command(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".agm" / "sandbox").mkdir(parents=True)
    (home / ".agm" / "sandbox" / "printf.json").write_text("{}")

    project = tmp_path / "project"
    (project / "config" / "sandbox").mkdir(parents=True)
    (project / "config" / "sandbox" / "default.json").write_text("{}")

    work = tmp_path / "work"
    (work / ".sandbox").mkdir(parents=True)
    (work / ".sandbox" / "default.json").write_text("{}")

    candidates = sandbox_settings_candidates(
        cwd=work,
        home=home,
        proj_dir=project,
        command_name="echo",
        alias_command_name="printf",
    )

    assert candidates == [
        home / ".agm" / "sandbox" / "printf.json",
        project / "config" / "sandbox" / "default.json",
        work / ".sandbox" / "default.json",
    ]


class TestOptionalBool:
    def test_returns_bool_value(self) -> None:
        assert _optional_bool({"flag": True}, "flag") is True
        assert _optional_bool({"flag": False}, "flag") is False

    def test_defaults_to_false(self) -> None:
        assert _optional_bool({}, "flag") is False

    def test_defaults_to_false_for_malformed_value(self) -> None:
        assert _optional_bool({"flag": "yes"}, "flag") is False

    def test_custom_default_true(self) -> None:
        assert _optional_bool({}, "flag", default=True) is True

    def test_custom_default_false(self) -> None:
        assert _optional_bool({}, "flag", default=False) is False

    def test_custom_default_ignored_when_value_is_bool(self) -> None:
        assert _optional_bool({"flag": False}, "flag", default=True) is False


def _exec_values(home: Path, cwd: Path) -> dict[str, object]:
    """Resolve the ``[exec]`` engine settings of the config under *home* by name."""
    from agnette.agl.runtime.arguments import OptionSome
    from agnette.agl.runtime.engine_config import engine_setting_params, split_engine_values
    from agnette.config.general import load_general_config
    from agnette.host_params.config import resolve_declaration_route_values

    config = load_general_config(home=home, proj_dir=None, cwd=cwd)
    values = resolve_declaration_route_values(config, engine_setting_params())
    return {
        key[2]: value.value if isinstance(value, OptionSome) else value
        for key, value in split_engine_values(values)[1].items()
    }


def test_config_path_interpolation_runs_once_and_leaves_program_tables_as_written(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".agm"
    config_dir.mkdir(parents=True)
    (config_dir / "%{PROMPT_DIR}").mkdir()
    (config_dir / "%{PROMPT_DIR}" / "prompt.md").write_text("prompt")
    (config_dir / "config.toml").write_text(
        'version = 1\n[exec]\ntrace-file = "\\\\%{PROMPT_DIR}/prompt.md"\n'
        '[program]\ntrace-file = "program.log"\n',
        encoding="utf-8",
    )

    merged = load_merged_config(home=home, proj_dir=None, cwd=tmp_path)

    exec_values = _exec_values(home, tmp_path)
    assert exec_values["trace-file"] == str(config_dir / "%{PROMPT_DIR}" / "prompt.md")
    assert merged["program"]["trace-file"] == "program.log"


def test_every_path_valued_engine_key_anchors_when_read_from_the_exec_section(
    tmp_path: Path,
) -> None:
    """The declaration route anchors the catalog's path keys; program tables anchor once typed."""
    from agnette.config.engine_keys import ENGINE_KEYS

    path_keys = [spec.name for spec in ENGINE_KEYS if spec.is_path]

    home = tmp_path / "home"
    config_dir = home / ".agm"
    config_dir.mkdir(parents=True)
    lines: list[str] = []
    for section in ("exec", "program"):
        lines.append(f"[{section}]")
        lines.extend(f'{key} = "{section}-{key}.log"' for key in path_keys)
    (config_dir / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    merged = load_merged_config(home=home, proj_dir=None, cwd=tmp_path)

    exec_values = _exec_values(home, tmp_path)
    for key in path_keys:
        assert exec_values[key] == str(tmp_path / f"exec-{key}.log")
        assert merged["program"][key] == f"program-{key}.log"


def test_a_blank_path_value_is_not_anchored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".agm").mkdir(parents=True)
    (home / ".agm" / "config.toml").write_text('[exec]\ntrace-file = "  "\n', encoding="utf-8")

    assert _exec_values(home, tmp_path)["trace-file"] == option_none_raw()


def test_a_later_layer_replaces_a_key_whose_kind_changed(tmp_path: Path) -> None:
    """Layers merge table into table; any other shape change is a plain override."""
    home = tmp_path / "home"
    (home / ".agm").mkdir(parents=True)
    (home / ".agm" / "config.toml").write_text(
        'version = 1\nworkflow = "claude"\n', encoding="utf-8"
    )

    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "config" / "config.toml").write_text(
        '[workflow]\ncommand = "codex"\n', encoding="utf-8"
    )

    merged = load_merged_config(home=home, proj_dir=project, cwd=tmp_path)

    assert merged["workflow"] == {"command": "codex"}


def test_exec_trace_file_expands_tilde_and_interpolates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the [exec] trace-file behavior: tilde expansion and
    ``%{name}`` interpolation both apply, same as every other path field."""
    home_dir = tmp_path / "user-home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("LOG_SUBDIR", "logs")

    home = tmp_path / "home"
    (home / ".agm").mkdir(parents=True)
    (home / ".agm" / "config.toml").write_text('[exec]\ntrace-file = "~/%{LOG_SUBDIR}/agm.log"\n')

    cwd = tmp_path / "work"
    cwd.mkdir()

    assert _exec_values(home, cwd)["trace-file"] == str(home_dir / "logs" / "agm.log")
