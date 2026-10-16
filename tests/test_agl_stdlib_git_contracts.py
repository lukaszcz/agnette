"""Cross-cutting guarantees of ``std/git``: paths, operands, and editors."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests._agl_git_helpers import (
    agl_check,
    agl_completed,
    agl_raises,
    run_git_program,
    status_literal,
    tracked_entry,
    untracked_entry,
)
from tests._git_helpers import (
    commit_file,
    conflicted_merge,
    conflicting_branches,
    git_fails,
    git_lines,
    git_output,
    git_rev,
    git_run,
    init_repo,
    worktree_files,
    write_file,
)


def test_status_paths_are_root_relative_and_feed_add_from_any_subdirectory(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    for directory in ("sub/deep", "other"):
        (repo / directory).mkdir(parents=True)
    commit_file(repo, env, name="sub/deep/tracked.txt", content="one\n")
    commit_file(repo, env, name="sub/keep.txt")
    head = git_rev(repo, env)
    write_file(repo, "sub/deep/tracked.txt", "two\n")
    for name in ("top.txt", "other/far.txt", "sub/deep/leaf.txt", "sub/sp ace.txt"):
        write_file(repo, name, "new\n")
    deep = "repo/sub/deep"

    def expected_status(*names: str, untracked: Sequence[str] = ()) -> str:
        changes = {n: tracked_entry(n, staged="Added") for n in names}
        changes["sub/deep/tracked.txt"] = tracked_entry("sub/deep/tracked.txt", staged="Modified")
        entries = [changes[n] for n in sorted(changes)]
        return status_literal(head, *entries, *(untracked_entry(n) for n in untracked))

    sub_paths = (
        f'git::status(repo = "{deep}").entries'
        '.select(fn(e: StatusEntry) => e.path.starts-with("sub/"))'
        ".map(fn(e: StatusEntry) => e.path)"
    )
    rest_paths = (
        f'git::status(repo = "{deep}").entries'
        ".select(fn(e: StatusEntry) => e.state is Untracked)"
        ".map(fn(e: StatusEntry) => e.path)"
    )
    body = f'let initial = git::status(repo = "{deep}")\n'
    # The paths found from one directory stage the same files when added from another.
    body += f'let sub-paths = {sub_paths}\ngit::add(sub-paths, "{deep}")\n'
    body += agl_check(
        "first",
        f'git::status(repo = "{deep}")',
        expected_status(
            "sub/deep/leaf.txt", "sub/sp ace.txt", untracked=["other/far.txt", "top.txt"]
        ),
    )
    body += f'let rest = {rest_paths}\ngit::add(rest, "repo/other")\n'
    body += agl_check(
        "second",
        'git::status(repo = "repo/sub")',
        expected_status("other/far.txt", "sub/deep/leaf.txt", "sub/sp ace.txt", "top.txt"),
    )
    # The paths of a diff unstage exactly what they name.
    body += 'let staged = git::diff(CommitToIndex("HEAD"), repo = "repo/sub")\n'
    body += 'git::reset-files(staged.map(fn(c: FileChange) => c.path), "HEAD", "repo/sub")\n'
    body += agl_check("restored", f'git::status(repo = "{deep}")', "initial")

    out = run_git_program(body, tmp_path, monkeypatch, capsys)

    assert out.splitlines() == ["ok"] * 3
    assert git_lines(repo, ["diff", "--cached", "--name-only"], env) == []


@pytest.mark.parametrize("pathspec_env", ["GIT_LITERAL_PATHSPECS", "GIT_ICASE_PATHSPECS"])
def test_pathspec_environment_does_not_override_literal_paths(
    pathspec_env: str,
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="a*.txt", message="lowercase")
    commit_file(repo, env, name="A*.txt", message="uppercase")
    write_file(repo, "a*.txt", "changed\n")
    write_file(repo, "A*.txt", "changed\n")
    monkeypatch.setenv(pathspec_env, "1")

    body = agl_check("files", 'git::files(["a*.txt"], "repo")', '["a*.txt"]')
    body += agl_check(
        "diff",
        'git::diff(paths = ["a*.txt"], repo = "repo").map(fn(c: FileChange) => c.path)',
        '["a*.txt"]',
    )
    body += agl_check(
        "log",
        'git::log(paths = ["a*.txt"], repo = "repo").map(fn(c: Commit) => c.subject)',
        '["lowercase"]',
    )
    out = run_git_program(body, tmp_path, monkeypatch, capsys)

    assert out.splitlines() == ["ok"] * 3


def test_paths_are_matched_literally(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="a*", content="star\n", message="add a*")
    commit_file(repo, env, name="ab", content="ab\n", message="add ab")
    head = git_rev(repo, env)
    write_file(repo, "a*", "star edited\n")
    write_file(repo, "ab", "ab edited\n")
    write_file(repo, "u*", "u\n")
    write_file(repo, "ux", "u\n")

    def status(*entries: str) -> str:
        return status_literal(head, *entries)

    star = tracked_entry("a*", unstaged="Modified")
    other = tracked_entry("ab", unstaged="Modified")
    pending = (star, other, untracked_entry("u*"), untracked_entry("ux"))
    star_staged = [tracked_entry("a*", staged="Modified"), *pending[1:]]
    body = agl_check("tracked", 'git::files(["a*"], "repo")', '["a*"]')
    body += agl_check(
        "history",
        'git::log(paths = ["a*"], repo = "repo").map(fn(c: Commit) => c.subject)',
        '["add a*"]',
    )
    body += agl_check(
        "diff", 'git::diff(paths = ["a*"], repo = "repo")', '[FileChange("a*", Modified)]'
    )
    body += agl_check(
        "stat",
        'git::diff-stat(paths = ["a*"], repo = "repo")',
        '[FileStat("a*", Some(1), Some(1))]',
    )
    body += 'git::add(["a*"], "repo")\n'
    body += agl_check("added", 'git::status(repo = "repo")', status(*star_staged))
    body += agl_check(
        "index",
        'git::diff(CommitToIndex("HEAD"), repo = "repo")',
        '[FileChange("a*", Modified)]',
    )
    body += 'git::reset-files(["a*"], repo = "repo")\n'
    body += agl_check("unstaged", 'git::status(repo = "repo")', status(*pending))
    body += agl_check(
        "stashed", 'git::stash::push(paths = ["a*"], repo = "repo").is-some()', "true"
    )
    body += agl_check("kept", 'git::status(repo = "repo")', status(*pending[1:]))
    body += 'let _ = git::stash::pop(repo = "repo")\n'
    body += agl_check("popped", 'git::status(repo = "repo")', status(*pending))
    body += 'git::checkout-files(["a*"], repo = "repo")\n'
    body += agl_check("restored", 'git::status(repo = "repo")', status(*pending[1:]))
    body += 'git::clean(["u*"], repo = "repo")\n'
    body += agl_check("cleaned", 'git::status(repo = "repo")', status(other, pending[3]))
    body += 'git::remove(["a*"], repo = "repo")\n'
    body += agl_check(
        "removed",
        'git::status(repo = "repo")',
        status(tracked_entry("a*", staged="Deleted"), other, pending[3]),
    )

    out = run_git_program(body, tmp_path, monkeypatch, capsys)

    assert out.splitlines() == ["ok"] * 13
    assert sorted(p.name for p in repo.iterdir() if p.name != ".git") == [
        "README.md",
        "ab",
        "ux",
    ]
    assert (repo / "ab").read_text(encoding="utf-8") == "ab edited\n"


_OPERATION_STATE = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "sequencer")


def _snapshot(repo: Path, env: dict[str, str]) -> dict[str, object]:
    """Return everything an operation could change in *repo*, as plain comparable values."""
    return {
        "head": git_rev(repo, env),
        "head-ref": git_output(repo, ["rev-parse", "--symbolic-full-name", "HEAD"], env),
        "refs": git_lines(repo, ["for-each-ref", "--format=%(refname) %(objectname)"], env),
        "config": git_lines(repo, ["config", "--local", "--list"], env),
        "status": git_lines(repo, ["status", "--porcelain=v2", "--branch", "-uall"], env),
        "index": git_lines(repo, ["ls-files", "--stage"], env),
        "stash": git_lines(repo, ["stash", "list", "--format=%H %gs"], env),
        "files": worktree_files(repo),
        "operation": [name for name in _OPERATION_STATE if (repo / ".git" / name).exists()],
    }


def test_option_like_operands_never_act_as_options(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="f.txt", content="base\n")
    git_run(repo, ["checkout", "-qb", "other"], env)
    commit_file(repo, env, name="o.txt")
    git_run(repo, ["checkout", "-q", "main"], env)
    for content in ("stash one", "stash two"):
        write_file(repo, "f.txt", content + "\n")
        git_run(repo, ["stash", "push", "-q"], env)
    write_file(repo, "f.txt", "staged\n")
    git_run(repo, ["add", "f.txt"], env)
    write_file(repo, "f.txt", "unstaged\n")
    write_file(repo, "u.txt", "untracked\n")
    merging = conflicted_merge(tmp_path / "merging", env)
    before = (_snapshot(repo, env), _snapshot(merging, env))

    # Each operand is an option its subcommand accepts (`--force`, `--hard`, `--squash`, `--abort`,
    # `--quit`, `--quiet`, ...), and would act as one if it reached git unguarded. All are
    # rejected before any state change, so the repositories are left exactly as they were.
    calls = [
        ('git::checkout({}, "repo")', ["--force", "-f", "--orphan=x"]),
        ('git::reset({}, Mixed, "repo")', ["--hard", "--force", "--orphan=x"]),
        ('git::merge({}, repo = "repo")', ["--force", "--orphan=x", "--squash"]),
        ('git::merge({}, repo = "merging")', ["--abort", "--quit", "--orphan=x"]),
        ('git::branch::delete({}, repo = "repo")', ["--force", "--orphan=x", "--list"]),
        ('git::stash::drop({}, "repo")', ["--force", "--orphan=x", "--quiet"]),
    ]
    body = "".join(
        agl_raises(template.format(f'"{operand}"'))
        for template, operands in calls
        for operand in operands
    )

    out = run_git_program(body, tmp_path, monkeypatch, capsys)

    assert (_snapshot(repo, env), _snapshot(merging, env)) == before
    assert out.splitlines() == ["raised"] * sum(len(operands) for _, operands in calls)


def _resolved(repo: Path, env: dict[str, str]) -> Path:
    """Resolve the conflict in ``f.txt`` of *repo* and stage it."""
    write_file(repo, "f.txt", "resolved\n")
    git_run(repo, ["add", "f.txt"], env)
    return repo


def test_failing_editors_do_not_affect_merge_revert_and_resume(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    merge = init_repo(tmp_path / "merge", env)
    git_run(merge, ["checkout", "-qb", "other"], env)
    commit_file(merge, env, name="o.txt")
    git_run(merge, ["checkout", "-q", "main"], env)
    commit_file(merge, env, name="m.txt")
    revert = init_repo(tmp_path / "revert", env)
    commit_file(revert, env, name="r.txt")
    resume_merge = _resolved(conflicted_merge(tmp_path / "resume-merge", env), env)
    resume_rebase = init_repo(tmp_path / "resume-rebase", env)
    conflicting_branches(resume_rebase, env)
    git_run(resume_rebase, ["checkout", "-q", "other"], env)
    git_fails(resume_rebase, ["rebase", "main"], env)
    _resolved(resume_rebase, env)
    repos = (merge, revert, resume_merge, resume_rebase)
    for repo in repos:
        git_run(repo, ["config", "core.editor", "false"], env)
    # `GIT_MERGE_AUTOEDIT` stands in for the terminal that makes git edit merge messages.
    failing = {"GIT_EDITOR": "false", "GIT_MERGE_AUTOEDIT": "yes"}
    plain = (
        (merge, ["merge", "--no-ff", "other"]),
        (resume_merge, ["merge", "--continue"]),
        (resume_rebase, ["rebase", "--continue"]),
    )
    # Unprotected, each of them runs the failing editor and fails. Outside a terminal git never
    # edits a revert message, so the revert step guards only the outcome.
    for repo, args in plain:
        copy = Path(shutil.copytree(repo, tmp_path / f"plain-{repo.name}"))
        git_fails(copy, args, {**env, **failing})
    for name, value in failing.items():
        monkeypatch.setenv(name, value)

    out = run_git_program(
        agl_completed("merged", 'git::merge("other", Never, repo = "merge")')
        + agl_completed("reverted", 'git::revert(["HEAD"], "revert")')
        + agl_completed("resumed-merge", 'git::resume(Merging, "resume-merge")')
        + agl_completed("resumed-rebase", 'git::resume(Rebasing, "resume-rebase")')
        + "".join(
            agl_check(f"idle-{repo.name}", f'git::operation("{repo.name}")', "None::[Operation]")
            for repo in repos
        ),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == [git_rev(repo, env) for repo in repos] + ["ok"] * len(repos)
    assert [git_lines(repo, ["status", "--porcelain"], env) for repo in repos] == [[]] * 4
