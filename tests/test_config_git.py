"""Behavior tests for config-directory git commits."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import agnette.core.dry_run as dry_run
from agnette.agm.project.config_git import commit_config_dir_changes
from tests._git_helpers import commit_file, git_output, git_rev, git_run

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
_GIT_IDENTITY_VARIABLES = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)


@needs_git
class TestCommitConfigDirChanges:
    def _make_repo(self, tmp_path: Path) -> tuple[Path, dict[str, str]]:
        """Keep identity only in the supplied home's global git config."""
        home = tmp_path / "home"
        home.mkdir()
        (home / ".gitconfig").write_text(
            "[user]\n\tname = Config User\n\temail = config@example.com\n",
            encoding="utf-8",
        )
        env = {
            key: value for key, value in os.environ.items() if key not in _GIT_IDENTITY_VARIABLES
        }
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
        project_dir = tmp_path / "proj"
        config_dir = project_dir / "config"
        config_dir.mkdir(parents=True)
        git_run(config_dir, ["init", "-q", "-b", "main"], env)
        git_run(config_dir, ["config", "user.useConfigOnly", "true"], env)
        return project_dir, env

    def _write_workspace_config(self, project_dir: Path) -> Path:
        workspace_config = project_dir / "config" / "feature"
        workspace_config.mkdir()
        (workspace_config / "config.toml").write_text("[deps]\n", encoding="utf-8")
        return workspace_config

    @pytest.mark.parametrize("inherit_env", [False, True], ids=["explicit-env", "default-env"])
    def test_commits_new_workspace_config_using_home_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inherit_env: bool
    ) -> None:
        project_dir, env = self._make_repo(tmp_path)
        config_dir = project_dir / "config"
        workspace_config = self._write_workspace_config(project_dir)
        if inherit_env:
            for name in _GIT_IDENTITY_VARIABLES:
                monkeypatch.delenv(name, raising=False)
            monkeypatch.setenv("HOME", env["HOME"])
            monkeypatch.setenv("XDG_CONFIG_HOME", env["XDG_CONFIG_HOME"])

        commit_config_dir_changes(
            project_dir,
            "chore: add config for feature",
            add_paths=[workspace_config],
            env=None if inherit_env else env,
        )

        assert git_output(config_dir, ["log", "-1", "--format=%s"], env) == (
            "chore: add config for feature"
        )
        assert git_output(config_dir, ["log", "-1", "--format=%an <%ae>|%cn <%ce>"], env) == (
            "Config User <config@example.com>|Config User <config@example.com>"
        )
        assert git_output(config_dir, ["show", "HEAD:feature/config.toml"], env) == "[deps]"
        assert git_output(config_dir, ["status", "--porcelain"], env) == ""

    def test_dry_run_leaves_index_and_history_untouched(self, tmp_path: Path) -> None:
        project_dir, env = self._make_repo(tmp_path)
        config_dir = project_dir / "config"
        workspace_config = self._write_workspace_config(project_dir)
        previous = dry_run.enabled()
        dry_run.set_enabled(True)
        try:
            commit_config_dir_changes(
                project_dir,
                "chore: add config for feature",
                add_paths=[workspace_config],
                env=env,
            )
        finally:
            dry_run.set_enabled(previous)

        assert git_output(config_dir, ["diff", "--cached", "--name-only"], env) == ""
        assert git_output(config_dir, ["rev-list", "--all", "--count"], env) == "0"
        assert (workspace_config / "config.toml").read_text(encoding="utf-8") == "[deps]\n"

    def test_add_paths_commits_additions_modifications_and_deletions_only_in_scope(
        self, tmp_path: Path
    ) -> None:
        project_dir, env = self._make_repo(tmp_path)
        config_dir = project_dir / "config"
        workspace_config = self._write_workspace_config(project_dir)
        commit_file(config_dir, env, name="feature/removed.toml", content="remove me\n")
        git_run(config_dir, ["add", "feature"], env)
        git_run(config_dir, ["commit", "-qm", "initial config"], env)
        commit_file(config_dir, env, name="unrelated.txt")
        (config_dir / "unrelated.txt").write_text("hand-edited\n", encoding="utf-8")
        git_run(config_dir, ["add", "unrelated.txt"], env)
        (workspace_config / "removed.toml").unlink()
        (workspace_config / "config.toml").write_text('[deps]\nlib = "feature"\n', encoding="utf-8")
        (workspace_config / "added.toml").write_text("new config\n", encoding="utf-8")

        commit_config_dir_changes(
            project_dir,
            "chore: update feature config",
            add_paths=[workspace_config],
            env=env,
        )

        assert git_output(
            config_dir, ["show", "--name-status", "--format=", "HEAD"], env
        ).splitlines() == [
            "A\tfeature/added.toml",
            "M\tfeature/config.toml",
            "D\tfeature/removed.toml",
        ]
        assert git_output(config_dir, ["show", "HEAD:feature/config.toml"], env) == (
            '[deps]\nlib = "feature"'
        )
        assert git_output(config_dir, ["show", "HEAD:unrelated.txt"], env) == "content"
        assert git_output(config_dir, ["diff", "--cached", "--name-only"], env) == "unrelated.txt"

    def test_fails_without_identity_and_preserves_staged_config(self, tmp_path: Path) -> None:
        project_dir, env = self._make_repo(tmp_path)
        config_dir = project_dir / "config"
        workspace_config = self._write_workspace_config(project_dir)
        broken_env = {
            **env,
            "HOME": str(tmp_path / "empty-home"),
            "XDG_CONFIG_HOME": str(tmp_path / "empty-home" / ".config"),
        }

        with pytest.raises(SystemExit) as raised:
            commit_config_dir_changes(
                project_dir,
                "chore: add config for feature",
                add_paths=[workspace_config],
                env=broken_env,
            )

        assert raised.value.code != 0
        assert git_output(config_dir, ["rev-list", "--all", "--count"], env) == "0"
        assert git_output(config_dir, ["diff", "--cached", "--name-only"], env) == (
            "feature/config.toml"
        )

    def test_config_dir_does_not_exist_does_nothing(
        self, tmp_path: Path, env: dict[str, str]
    ) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        commit_config_dir_changes(project_dir, "chore: test", env=env)

        assert list(project_dir.iterdir()) == []

    def test_config_dir_inside_parent_repo_does_not_commit_parent(
        self, tmp_path: Path, env: dict[str, str]
    ) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        git_run(tmp_path, ["init", "-q", "-b", "main"], env)
        commit_file(tmp_path, env, name="tracked.txt")
        before = git_rev(tmp_path, env)
        (config_dir / "config.toml").write_text("[deps]\n", encoding="utf-8")
        (tmp_path / "tracked.txt").write_text("keep uncommitted\n", encoding="utf-8")
        status = git_output(tmp_path, ["status", "--porcelain"], env)

        commit_config_dir_changes(tmp_path, "chore: test", env=env)

        assert not (config_dir / ".git").exists()
        assert git_rev(tmp_path, env) == before
        assert git_output(tmp_path, ["status", "--porcelain"], env) == status

    @pytest.mark.parametrize("new_config", [False, True])
    def test_no_add_paths_commits_tracked_changes_and_only_new_config_toml(
        self, tmp_path: Path, new_config: bool
    ) -> None:
        project_dir, env = self._make_repo(tmp_path)
        config_dir = project_dir / "config"
        commit_file(config_dir, env, name="settings.toml", content="[base]\n")
        (config_dir / "settings.toml").write_text("[base]\nupdated = true\n", encoding="utf-8")
        (config_dir / "scratch.txt").write_text("leave untracked\n", encoding="utf-8")
        expected = {"settings.toml"}
        if new_config:
            (config_dir / "config.toml").write_text("[project]\n", encoding="utf-8")
            nested = config_dir / "nested"
            nested.mkdir()
            (nested / "config.toml").write_text("[deps]\n", encoding="utf-8")
            (config_dir / "directory" / "config.toml").mkdir(parents=True)
            expected.update({"config.toml", "nested/config.toml"})

        commit_config_dir_changes(project_dir, "chore: update config", env=env)

        assert (
            set(
                git_output(
                    config_dir, ["show", "--name-only", "--format=", "HEAD"], env
                ).splitlines()
            )
            == expected
        )
        assert git_output(config_dir, ["show", "HEAD:settings.toml"], env) == (
            "[base]\nupdated = true"
        )
        assert git_output(config_dir, ["log", "-1", "--format=%s"], env) == "chore: update config"
        assert git_output(config_dir, ["status", "--porcelain"], env) == "?? scratch.txt"

    @pytest.mark.parametrize(
        "scope", ["empty-directory", "missing-path", "empty-list", "unchanged"]
    )
    def test_no_scoped_changes_leaves_history_and_unrelated_index_untouched(
        self, tmp_path: Path, scope: str
    ) -> None:
        project_dir, env = self._make_repo(tmp_path)
        config_dir = project_dir / "config"
        commit_file(config_dir, env, name="config.toml", content="[deps]\n")
        before = git_rev(config_dir, env)
        (config_dir / "unrelated.txt").write_text("keep staged\n", encoding="utf-8")
        git_run(config_dir, ["add", "unrelated.txt"], env)
        if scope == "empty-list":
            paths: list[Path] = []
        elif scope == "unchanged":
            paths = [config_dir / "config.toml"]
        else:
            paths = [config_dir / "feature"]
            if scope == "empty-directory":
                paths[0].mkdir()

        commit_config_dir_changes(project_dir, "chore: test", add_paths=paths, env=env)

        assert git_rev(config_dir, env) == before
        assert git_output(config_dir, ["diff", "--cached", "--name-only"], env) == "unrelated.txt"
        assert (config_dir / "unrelated.txt").read_text(encoding="utf-8") == "keep staged\n"

    def test_git_add_failure_preserves_index_and_history(self, tmp_path: Path) -> None:
        project_dir, env = self._make_repo(tmp_path)
        config_dir = project_dir / "config"
        workspace_config = self._write_workspace_config(project_dir)
        lock = config_dir / ".git" / "index.lock"
        lock.write_text("held by another process\n", encoding="utf-8")

        with pytest.raises(SystemExit) as raised:
            commit_config_dir_changes(
                project_dir, "chore: test", add_paths=[workspace_config], env=env
            )

        assert raised.value.code != 0
        assert lock.read_text(encoding="utf-8") == "held by another process\n"
        assert git_output(config_dir, ["rev-list", "--all", "--count"], env) == "0"
        assert git_output(config_dir, ["diff", "--cached", "--name-only"], env) == ""
