"""The suite's own isolation from the machine it runs on.

Every other test trusts that AgM resolves its home, project, and shell state
from what that test set up.  These tests verify that trust: a developer with a
real ``~/.agm`` — installed packages and a personal ``config.toml`` — must
get the same results as a clean checkout, and a suite launched from inside a
project directory or a workspace shell must not inherit either.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from agnette.config.context import current_config_context
from agnette.config.home import agm_home_dir
from agnette.packages.activation import load_activation_index
from tests._git_helpers import git_output, init_repo
from tests.conftest import LAUNCH_ENVIRONMENT, isolate_host_environment


def test_the_suite_does_not_run_against_the_launching_home() -> None:
    launched_from = LAUNCH_ENVIRONMENT.get("HOME")

    assert launched_from is not None
    assert os.environ["HOME"] != launched_from
    assert Path.home() != Path(launched_from)


def test_isolated_homes_are_fresh_private_and_do_not_scan_siblings(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path_factory.getbasetemp()
    homes: list[Path] = []
    scandir = os.scandir

    def no_base_scan(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        assert Path(path) != base
        return scandir(path)

    monkeypatch.setattr(os, "scandir", no_base_scan)
    for _ in range(2):
        with pytest.MonkeyPatch.context() as patch:
            isolate_host_environment.__wrapped__(None, tmp_path_factory, patch)
            home = Path.home()
            assert home.parent == base
            assert not list(home.iterdir())
            assert home.stat().st_mode & 0o777 == 0o700
            assert os.environ["XDG_CONFIG_HOME"] == str(home / ".config")
            assert os.environ["XDG_DATA_HOME"] == str(home / ".local" / "share")
            assert os.environ["GIT_CEILING_DIRECTORIES"] == str(base.resolve())
            (home / ".gitconfig").write_text("[user]\nname = previous-test\n")
            homes.append(home)
    assert homes[0] != homes[1]


def test_the_agm_home_is_empty_of_installed_state() -> None:
    context = current_config_context()
    home = agm_home_dir(home=context.home)

    assert not home.exists()
    index = load_activation_index(home=context.home)
    assert index.packages == {}
    assert index.commands == {}


def test_project_and_shell_variables_are_not_inherited() -> None:
    for name in ("PROJ_DIR", "REPO_DIR", "TMUX", "TMUX_PANE"):
        assert name not in os.environ
    assert not any(name.startswith("AGM_") for name in os.environ if name != "AGM_STDLIB")
    assert not any(name.startswith("TYPESAFE_") for name in os.environ)


def test_proxy_variables_are_not_inherited() -> None:
    assert not any(name.lower().endswith("_proxy") for name in os.environ)


def test_a_real_httpx2_send_fails_the_test_without_dialing_out() -> None:
    with httpx2.Client() as client, pytest.raises(pytest.fail.Exception):
        client.get("https://typesafe.invalid/v1/models")


def test_git_identity_does_not_depend_on_a_personal_gitconfig() -> None:
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    assert os.environ["GIT_AUTHOR_NAME"]
    assert os.environ["GIT_COMMITTER_EMAIL"]
    assert not (Path(os.environ["HOME"]) / ".gitconfig").exists()


def test_git_discovery_cannot_climb_into_a_repository_the_temp_root_sits_inside(
    tmp_path: Path,
) -> None:
    """Git run from a directory with no repository of its own must never discover
    one, wherever the suite's own temp root happens to sit on the host -- even a
    repository rooted exactly at the temp root itself (e.g. a sandboxed ``TMPDIR``
    that is already a git working tree).

    Git never searches the ceiling directory itself or anything above it, so the
    ceiling must be the temp root, not its parent: a parent-of-root ceiling still
    lets a repository rooted at the temp root be found.
    """
    assert (
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=tmp_path,
            env=dict(os.environ),
            capture_output=True,
            text=True,
        ).returncode
        != 0
    )

    temp_root = tmp_path / "pytest-tmp"
    case_dir = temp_root / "case"
    init_repo(temp_root, dict(os.environ))
    case_dir.mkdir()

    unfenced_env = {k: v for k, v in os.environ.items() if k != "GIT_CEILING_DIRECTORIES"}
    assert git_output(case_dir, ["rev-parse", "--show-toplevel"], unfenced_env) == str(
        temp_root.resolve()
    )

    fenced_env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(temp_root)}
    fenced = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=case_dir,
        env=fenced_env,
        capture_output=True,
        text=True,
    )
    assert fenced.returncode != 0
