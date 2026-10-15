"""Repository functions of ``std/git``: detection, ``init``, ``clone``, ``files``, errors."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests._agl_git_helpers import run_git_program
from tests._git_helpers import commit_file, conflicted_merge, git_output, git_run, init_repo


def test_is_repo_and_root_distinguish_work_trees_from_everything_else(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    (repo / "sub").mkdir()
    (tmp_path / "plain").mkdir()
    git_run(None, ["init", "--bare", "-q", str(tmp_path / "bare.git")], env)

    out, result = run_git_program(
        """
print(git::is-repo("repo"))
print(git::is-repo("repo/sub"))
print(git::is-repo("plain"))
print(git::is-repo("missing"))
print(git::is-repo("bare.git"))
print(git::root("repo/sub"))
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert result.warnings == []
    assert out.splitlines() == [
        "true",
        "true",
        "false",
        "false",
        "false",
        str(repo.resolve()),
    ]


def test_init_and_clone_honor_branch_depth_and_bare(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = init_repo(tmp_path / "source", env)
    commit_file(source, env, name="main2.txt", message="main commit 2")
    git_run(source, ["checkout", "-b", "dev", "-q"], env)
    commit_file(source, env, name="dev.txt", message="dev commit")
    commit_file(source, env, name="dev2.txt", message="dev commit 2")
    git_run(source, ["checkout", "main", "-q"], env)

    _, result = run_git_program(
        f"""
git::init("fresh", Some("trunk"))
git::init("fresh-bare", None, true)
git::init("plain-init")
git::clone("{source}", "full")
git::clone("{source}", "on-dev", Some("dev"))
git::clone("{source}", "shallow", None, Some(1))
git::clone("{source}", "mirror", None, None, true)
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert git_output(tmp_path / "fresh", ["symbolic-ref", "--short", "HEAD"], env) == "trunk"
    assert git_output(tmp_path / "fresh-bare", ["rev-parse", "--is-bare-repository"], env) == "true"
    assert (
        git_output(tmp_path / "plain-init", ["rev-parse", "--is-inside-work-tree"], env) == "true"
    )
    assert (tmp_path / "full" / "README.md").is_file()
    assert git_output(tmp_path / "on-dev", ["symbolic-ref", "--short", "HEAD"], env) == "dev"
    assert git_output(tmp_path / "shallow", ["rev-parse", "--is-shallow-repository"], env) == "true"
    assert git_output(tmp_path / "full", ["rev-list", "--count", "HEAD"], env) == "2"
    assert git_output(tmp_path / "shallow", ["rev-list", "--count", "HEAD"], env) == "1"
    assert git_output(tmp_path / "mirror", ["rev-parse", "--is-bare-repository"], env) == "true"


def test_files_lists_tracked_paths_relative_to_the_root(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    (repo / "sub").mkdir()
    commit_file(repo, env, name="sub/a.txt")
    commit_file(repo, env, name="sub/b c.txt")
    commit_file(repo, env, name="a*")
    commit_file(repo, env, name="ab")
    (repo / "untracked.txt").write_text("x", encoding="utf-8")

    out, result = run_git_program(
        """
print(git::files([], "repo").size())
for path in git::files(["sub"], "repo/sub") do
  print(path)
for path in git::files(["a*"], "repo") do
  print(path)
for spec in ["./sub", "sub/", "sub/../ab", "./sub/./", "missing"] do
  print(git::files([spec], "repo"))
print(git::files(["."], "repo").size())
print(git::files(["./"], "repo/sub").size())
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    sub = '["sub/a.txt", "sub/b c.txt"]'
    assert out.splitlines() == [
        "5",
        "sub/a.txt",
        "sub/b c.txt",
        "a*",
        sub,
        sub,
        '["ab"]',
        sub,
        "[]",
        "5",
        "5",
    ]


def test_files_lists_an_unmerged_path_once(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conflicted_merge(tmp_path / "repo", env, name="clash.txt")

    out, result = run_git_program(
        """
print(git::files([], "repo"))
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert out.strip() == '["README.md", "clash.txt"]'


def test_missing_git_executable_raises_git_error_127(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    only_sh = tmp_path / "only-sh"
    only_sh.mkdir()
    shell = shutil.which("sh")
    assert shell is not None
    (only_sh / "sh").symlink_to(shell)
    monkeypatch.setenv("PATH", str(only_sh))

    out, result = run_git_program(
        """
try
  print(git::is-repo("."))
  print(git::root("."))
catch GitError as e =>
  print(e.exit-code)
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert out.splitlines() == ["false", "127"]


def test_git_error_carries_the_failed_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out, result = run_git_program(
        """
try
  print(git::root("missing"))
catch GitError as e =>
  print(e.args[0])
  print(e.args[1])
  print(e.exit-code)
  print(e.stdout.is-empty())
  print(e.stderr.is-empty())
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert out.splitlines() == ["-C", "missing", "128", "true", "false"]
