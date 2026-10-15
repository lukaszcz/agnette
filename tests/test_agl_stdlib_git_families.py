"""Family scopes of ``std/git``: ``branch``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._agl_git_helpers import agl_raises, run_git_program
from tests._git_helpers import clone_local_remote, git_run


def test_branch_exists_is_true_only_for_local_branches(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, repo = clone_local_remote(tmp_path, env, branches=["feat"])
    git_run(repo, ["branch", "feature/x"], env)
    git_run(repo, ["tag", "a-tag"], env)
    (tmp_path / "plain").mkdir()

    out, result = run_git_program(
        """
for name in ["main", "feature/x", "a-tag", "feat", "origin/feat", "--force", "nope"] do
  print("%{name} %{git::branch::exists(name, "repo")}")
"""
        + agl_raises('git::branch::exists("main", "plain")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert out.splitlines() == [
        "main true",
        "feature/x true",
        "a-tag false",
        "feat false",
        "origin/feat false",
        "--force false",
        "nope false",
        "raised",
    ]
