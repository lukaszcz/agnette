"""Development-tooling CLI contracts, without repeating full-grammar export tests."""

from __future__ import annotations

import builtins
import subprocess
import sys
from pathlib import Path

import pytest
from typer._completion_classes import ZshComplete
from typer.main import get_command
from typer.testing import CliRunner

from agnette.agl.highlight.grammar import Grammar, Rule, State
from agnette.agl.highlight.targets import TARGETS
from agnette.agm.cli import app
from agnette.command_catalog import invalid_command_path


@pytest.mark.parametrize("target", TARGETS)
def test_highlight_stdout_and_nested_output(
    target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agnette.agl.highlight.agl_grammar as agl_grammar
    from agnette.agl.highlight.categories import Category

    grammar = Grammar((State("root", (Rule("x", Category.KEYWORD),)),))
    monkeypatch.setattr(agl_grammar, "AGL_GRAMMAR", grammar)
    expected = TARGETS[target](grammar)
    runner = CliRunner()
    result = runner.invoke(app, ["dev", "highlight", target])
    assert result.exit_code == 0, result.output
    assert result.stdout == expected

    output = tmp_path / "nested" / "parent" / f"agl.{target}"
    for _ in range(2):
        result = runner.invoke(app, ["dev", "highlight", target, "--output", str(output)])
        assert result.exit_code == 0, result.output
        assert result.stdout == ""
        assert output.read_text(encoding="utf-8") == expected


def test_highlight_rejects_missing_unknown_target_and_dry_run(tmp_path: Path) -> None:
    runner = CliRunner()
    output = tmp_path / "absent" / "output"
    for tail in ([], ["unknown", "--output", str(output)], ["kde", "--dry-run"]):
        result = runner.invoke(app, ["dev", "highlight", *tail])
        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit)
    assert not output.parent.exists()
    assert invalid_command_path("dev custom") is not None


def test_cli_startup_does_not_construct_highlight_grammar() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import agnette.agm.cli; "
            "assert 'agnette.agl.highlight.agl_grammar' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_highlight_help_and_completion_do_not_load_grammar(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def import_without_grammar(name: str, *args: object, **kwargs: object) -> object:
        assert name != "agnette.agl.highlight.agl_grammar"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_grammar)
    runner = CliRunner()
    for argv in (
        ["dev"],
        ["dev", "--help"],
        ["help", "dev"],
        ["dev", "highlight", "--help"],
        ["help", "dev", "highlight"],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output
        assert "highlight" in result.stdout

    complete = ZshComplete(get_command(app), {}, "agm", "_AGM_COMPLETE")
    for prefix in ("", "e", "k", "t", "missing"):
        candidates = complete.get_completions(["dev", "highlight"], prefix)
        assert [item.value for item in candidates] == sorted(
            name for name in TARGETS if name.startswith(prefix)
        )
    assert "highlight" in [item.value for item in complete.get_completions(["help", "dev"], "")]
    assert "dev" in [item.value for item in complete.get_completions(["help"], "d")]
