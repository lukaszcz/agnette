"""History functions of ``std/git``: ``resolve`` and ``resolve?``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._agl_git_helpers import run_git_program
from tests._git_helpers import commit_file, git_output, init_repo


def test_resolve_returns_full_commit_hashes_and_resolve_option_none_for_unknown_refs(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="second.txt")
    head = git_output(repo, ["rev-parse", "HEAD"], env)
    parent = git_output(repo, ["rev-parse", "HEAD~1"], env)

    out, result = run_git_program(
        """
print(git::resolve("HEAD", "repo"))
print(git::resolve("main~1", "repo"))
print(git::resolve("HEAD", "repo/.").size())
print(git::resolve?("HEAD", "repo").unwrap())
print(git::resolve?("no-such-ref", "repo").is-none())
try
  print(git::resolve("no-such-ref", "repo"))
catch GitError as e =>
  print(e.exit-code != 0)
""",
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::GitError",),
    )

    assert result.ok, result.error
    assert out.splitlines() == [head, parent, "40", head, "true", "true"]
