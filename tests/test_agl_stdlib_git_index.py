"""Working-tree and index functions of ``std/git``: ``status``, staging, commits, resets."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._agl_git_helpers import agl_check, agl_raises, run_git_program
from tests._git_helpers import (
    clone_local_remote,
    commit_file,
    git_fails,
    git_head,
    git_lines,
    git_output,
    git_run,
    init_repo,
    lines_of,
)


def test_status_reports_every_change_letter_with_rename_source_and_no_copies(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["config", "status.renames", "copies"], env)
    for name in ("mm", "sm", "um", "del-s", "del-u", "ts", "tu", "cp-src", "ren a", "ren-c"):
        commit_file(repo, env, name=f"{name}.txt", content=lines_of(name), message=name)
    commit_file(repo, env, name="sp ace.txt", content=lines_of("sp"), message="sp")

    (repo / "mm.txt").write_text(lines_of("mm") + "staged\n", encoding="utf-8")
    git_run(repo, ["add", "mm.txt"], env)
    (repo / "mm.txt").write_text(lines_of("mm") + "staged\nunstaged\n", encoding="utf-8")
    (repo / "sm.txt").write_text(lines_of("sm") + "staged\n", encoding="utf-8")
    git_run(repo, ["add", "sm.txt"], env)
    (repo / "um.txt").write_text(lines_of("um") + "unstaged\n", encoding="utf-8")
    git_run(repo, ["rm", "-q", "del-s.txt"], env)
    (repo / "del-u.txt").unlink()
    (repo / "ts.txt").unlink()
    os.symlink("README.md", repo / "ts.txt")
    git_run(repo, ["add", "ts.txt"], env)
    (repo / "tu.txt").unlink()
    os.symlink("README.md", repo / "tu.txt")
    (repo / "sp ace.txt").write_text(lines_of("sp") + "unstaged\n", encoding="utf-8")
    (repo / "add.txt").write_text(lines_of("add"), encoding="utf-8")
    git_run(repo, ["add", "add.txt"], env)
    git_run(repo, ["mv", "ren a.txt", "ren b.txt"], env)
    (repo / "ren-c.txt").rename(repo / "ren-d.txt")
    (repo / "ren-d.txt").write_text(lines_of("ren-c") + "edited\n", encoding="utf-8")
    git_run(repo, ["add", "-A", "ren-c.txt", "ren-d.txt"], env)
    (repo / "cp-src.txt").write_text(lines_of("cp-src") + "edited\n", encoding="utf-8")
    (repo / "cp-dst.txt").write_text(lines_of("cp-src"), encoding="utf-8")
    git_run(repo, ["add", "cp-src.txt", "cp-dst.txt"], env)

    out, result = run_git_program(
        agl_check(
            "entries",
            'git::status(repo = "repo").entries',
            """[
  StatusEntry("add.txt", Tracked(Added, Unmodified)),
  StatusEntry("cp-dst.txt", Tracked(Added, Unmodified)),
  StatusEntry("cp-src.txt", Tracked(Modified, Unmodified)),
  StatusEntry("del-s.txt", Tracked(Deleted, Unmodified)),
  StatusEntry("del-u.txt", Tracked(Unmodified, Deleted)),
  StatusEntry("mm.txt", Tracked(Modified, Modified)),
  StatusEntry("ren b.txt", Tracked(Renamed("ren a.txt", 100), Unmodified)),
  StatusEntry("ren-d.txt", Tracked(Renamed("ren-c.txt", 97), Unmodified)),
  StatusEntry("sm.txt", Tracked(Modified, Unmodified)),
  StatusEntry("sp ace.txt", Tracked(Unmodified, Modified)),
  StatusEntry("ts.txt", Tracked(TypeChanged, Unmodified)),
  StatusEntry("tu.txt", Tracked(Unmodified, TypeChanged)),
  StatusEntry("um.txt", Tracked(Unmodified, Modified)),
]""",
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"]


def test_status_reports_every_conflict_kind(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    for name in ("m", "du", "ud"):
        commit_file(repo, env, name=f"{name}.txt", content="base\n")
    commit_file(repo, env, name="f.txt", content=lines_of("f"))
    git_run(repo, ["checkout", "-qb", "other"], env)
    (repo / "m.txt").write_text("theirs\n", encoding="utf-8")
    (repo / "aa.txt").write_text("theirs\n", encoding="utf-8")
    (repo / "du.txt").write_text("theirs\n", encoding="utf-8")
    git_run(repo, ["rm", "-q", "ud.txt"], env)
    git_run(repo, ["mv", "f.txt", "h.txt"], env)
    git_run(repo, ["add", "-A"], env)
    git_run(repo, ["commit", "-qm", "other"], env)
    git_run(repo, ["checkout", "-q", "main"], env)
    (repo / "m.txt").write_text("ours\n", encoding="utf-8")
    (repo / "aa.txt").write_text("ours\n", encoding="utf-8")
    git_run(repo, ["rm", "-q", "du.txt"], env)
    (repo / "ud.txt").write_text("ours\n", encoding="utf-8")
    git_run(repo, ["mv", "f.txt", "g.txt"], env)
    git_run(repo, ["add", "-A"], env)
    git_run(repo, ["commit", "-qm", "main"], env)
    git_fails(repo, ["merge", "other"], env)

    out, result = run_git_program(
        agl_check(
            "entries",
            'git::status(repo = "repo").entries',
            """[
  StatusEntry("aa.txt", Conflict(BothAdded)),
  StatusEntry("du.txt", Conflict(DeletedByUs)),
  StatusEntry("f.txt", Conflict(BothDeleted)),
  StatusEntry("g.txt", Conflict(AddedByUs)),
  StatusEntry("h.txt", Conflict(AddedByThem)),
  StatusEntry("m.txt", Conflict(BothModified)),
  StatusEntry("ud.txt", Conflict(DeletedByThem)),
]""",
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"]


def test_status_untracked_and_ignored_switches(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name=".gitignore", content="*.log\nbuild/\n")
    commit_file(repo, env, name="tracked.txt")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo / "dir").mkdir()
    (repo / "dir" / "inner.txt").write_text("x", encoding="utf-8")
    (repo / "new file.txt").write_text("x", encoding="utf-8")
    (repo / "a  b\nc.txt").write_text("x", encoding="utf-8")
    (repo / "a.log").write_text("x", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("x", encoding="utf-8")
    git_run(repo, ["config", "status.showUntrackedFiles", "no"], env)

    tracked = 'StatusEntry("tracked.txt", Tracked(Unmodified, Modified))'
    untracked = (
        'StatusEntry("a  b\\nc.txt", Untracked), StatusEntry("dir/inner.txt", Untracked), '
        'StatusEntry("new file.txt", Untracked)'
    )
    ignored = 'StatusEntry("a.log", Ignored), StatusEntry("build/", Ignored)'
    out, result = run_git_program(
        agl_check("both", 'git::status(repo = "repo").entries', f"[{tracked}, {untracked}]")
        + agl_check("none", 'git::status(false, false, "repo").entries', f"[{tracked}]")
        + agl_check(
            "all", 'git::status(true, true, "repo").entries', f"[{tracked}, {untracked}, {ignored}]"
        )
        + agl_check(
            "ignored", 'git::status(false, true, "repo").entries', f"[{tracked}, {ignored}]"
        )
        + agl_check(
            "from-subdir", 'git::status(repo = "repo/dir").entries', f"[{tracked}, {untracked}]"
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 5


def test_status_branch_upstream_and_divergence(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, repo = clone_local_remote(tmp_path, env, branches=["gone"])
    git_run(None, ["clone", "-q", str(source), str(tmp_path / "synced")], env)
    git_run(None, ["clone", "-q", str(source), str(tmp_path / "local")], env)
    git_run(tmp_path / "local", ["checkout", "-qb", "topic"], env)
    git_run(None, ["clone", "-q", "-b", "gone", str(source), str(tmp_path / "gone")], env)
    git_run(source, ["branch", "-qD", "gone"], env)
    git_run(tmp_path / "gone", ["fetch", "-q", "--prune"], env)
    commit_file(repo, env, name="local.txt", message="local")
    commit_file(source, env, name="remote.txt", message="remote")
    commit_file(source, env, name="remote2.txt", message="remote 2")
    git_run(repo, ["fetch", "-q"], env)
    heads = {name: git_head(tmp_path / name, env) for name in ("repo", "synced", "local", "gone")}

    def state(name: str, branch: str, upstream: str, divergence: str) -> str:
        return agl_check(
            name,
            f'git::status(repo = "{name}")',
            f'Status(Some("{heads[name]}"), Some("{branch}"), {upstream}, {divergence}, [])',
        )

    out, result = run_git_program(
        state("repo", "main", 'Some("origin/main")', "Some(Divergence(1, 2))")
        + state("synced", "main", 'Some("origin/main")', "Some(Divergence(0, 0))")
        + state("local", "topic", "None", "None")
        + state("gone", "gone", 'Some("origin/gone")', "None"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 4


def test_status_of_unborn_and_detached_head(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_run(None, ["init", "-q", "-b", "trunk", str(tmp_path / "unborn")], env)
    (tmp_path / "unborn" / "new.txt").write_text("x", encoding="utf-8")
    git_run(tmp_path / "unborn", ["add", "new.txt"], env)
    detached = init_repo(tmp_path / "detached", env)
    git_run(detached, ["checkout", "-q", "--detach"], env)
    head = git_head(detached, env)

    out, result = run_git_program(
        agl_check(
            "unborn",
            'git::status(repo = "unborn")',
            'Status(None, Some("trunk"), None, None, '
            '[StatusEntry("new.txt", Tracked(Added, Unmodified))])',
        )
        + agl_check(
            "detached",
            'git::status(repo = "detached")',
            f'Status(Some("{head}"), None, None, None, [])',
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 2


def test_status_of_branch_named_with_leading_parenthesis(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "paren", env, branch="(x")
    head = git_head(repo, env)

    out, result = run_git_program(
        agl_check(
            "paren",
            'git::status(repo = "paren")',
            f'Status(Some("{head}"), Some("(x"), None, None, [])',
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"]


def test_status_ignores_submodule_ignore_configuration(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    head = git_head(repo, env)
    git_run(repo, ["update-index", "--add", "--cacheinfo", f"160000,{head},sub"], env)
    git_run(repo, ["commit", "-qm", "gitlink"], env)
    git_run(repo, ["config", "diff.ignoreSubmodules", "all"], env)
    nested = init_repo(repo / "sub", env)
    commit_file(nested, env, name="more.txt")

    out, result = run_git_program(
        agl_check(
            "entries",
            'git::status(repo = "repo").entries',
            '[StatusEntry("sub", Tracked(Unmodified, Modified))]',
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"]


def test_is_clean_distinguishes_untracked_from_tracked_changes(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_repo(tmp_path / "clean", env)
    init_repo(tmp_path / "untracked", env)
    ignored = init_repo(tmp_path / "ignored", env)
    commit_file(ignored, env, name=".gitignore", content="*.log\n")
    (ignored / "a.log").write_text("x", encoding="utf-8")
    (tmp_path / "untracked" / "new.txt").write_text("x", encoding="utf-8")
    modified = init_repo(tmp_path / "modified", env)
    (modified / "README.md").write_text("changed\n", encoding="utf-8")
    staged = init_repo(tmp_path / "staged", env)
    (staged / "new.txt").write_text("x", encoding="utf-8")
    git_run(staged, ["add", "new.txt"], env)

    out, result = run_git_program(
        """
for name in ["clean", "ignored", "untracked", "modified", "staged"] do
  print("%{name} %{git::is-clean(true, name)} %{git::is-clean(false, name)}")
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert out.splitlines() == [
        "clean true true",
        "ignored true true",
        "untracked false true",
        "modified false false",
        "staged false false",
    ]


def _tree(repo: Path) -> list[str]:
    """Return the sorted work-tree files of *repo*, excluding ``.git``."""

    return sorted(
        str(p.relative_to(repo))
        for p in repo.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    )


def test_add_stages_literal_paths_and_dot_stages_deletions(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    literal = init_repo(tmp_path / "literal", env)
    dot = init_repo(tmp_path / "dot", env)
    commit_file(dot, env, name="gone.txt")
    (dot / "sub").mkdir()
    for name in ("a*", "ab", "-dash"):
        (literal / name).write_text("x", encoding="utf-8")
    (dot / "new.txt").write_text("x", encoding="utf-8")
    (dot / "gone.txt").unlink()

    _, result = run_git_program(
        """
git::add(["a*", "-dash"], "literal")
git::add(["."], "dot/sub")
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert git_lines(literal, ["diff", "--cached", "--name-only"], env) == ["-dash", "a*"]
    assert git_lines(dot, ["diff", "--cached", "--name-status"], env) == [
        "D\tgone.txt",
        "A\tnew.txt",
    ]


def test_remove_deletes_from_index_and_optionally_keeps_or_forces_the_file(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    for name in ("plain.txt", "kept.txt", "dirty.txt", "a*", "ab"):
        commit_file(repo, env, name=name)
    (repo / "tree").mkdir()
    commit_file(repo, env, name="tree/leaf.txt")
    (repo / "dirty.txt").write_text("edited\n", encoding="utf-8")
    git_run(repo, ["add", "dirty.txt"], env)
    (repo / "dirty.txt").write_text("edited again\n", encoding="utf-8")

    out, result = run_git_program(
        agl_raises('git::remove(["dirty.txt"], repo = "repo")')
        + """
git::remove(["plain.txt", "tree", "a*"], repo = "repo/tree")
git::remove(["kept.txt"], cached = true, repo = "repo")
git::remove(["dirty.txt"], force = true, repo = "repo")
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised"]
    assert git_lines(repo, ["ls-files"], env) == ["README.md", "ab"]
    assert _tree(repo) == ["README.md", "ab", "kept.txt"]


def test_move_renames_root_relative_paths_from_a_subdirectory(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    (repo / "sub").mkdir()
    for name in ("old.txt", "-dash", "dir-file.txt"):
        (repo / name).write_text("x", encoding="utf-8")
    git_run(repo, ["add", "--", "old.txt", "-dash", "dir-file.txt"], env)
    git_run(repo, ["commit", "-qm", "files"], env)

    out, result = run_git_program(
        agl_raises('git::move("missing.txt", "x.txt", "repo")')
        + """
git::move("old.txt", "sub/new.txt", "repo/sub")
git::move("-dash", "-other", "repo")
git::move("dir-file.txt", "sub", "repo")
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised"]
    assert git_lines(repo, ["status", "--porcelain"], env) == [
        "R  -dash -> -other",
        "R  dir-file.txt -> sub/dir-file.txt",
        "R  old.txt -> sub/new.txt",
    ]


def test_commit_returns_head_and_honors_each_flag(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["config", "commit.cleanup", "strip"], env)
    (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
    git_run(repo, ["add", "tracked.txt"], env)
    git_run(repo, ["commit", "-qm", "track"], env)
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    out, result = run_git_program(
        agl_raises('git::commit("nothing staged", verify = false, repo = "repo")')
        + agl_raises('git::commit("unverified", all = true, repo = "repo")')
        + """
let all = git::commit("all", all = true, verify = false, repo = "repo")
print(all == git::resolve("HEAD", "repo"))
let amended = git::commit("amended  \\n\\n\\n\\n# kept\\n\\n", amend = true, verify = false,
                          repo = "repo")
print(amended == git::resolve("HEAD", "repo"))
print(amended != all)
let empty = git::commit("empty", allow-empty = true, verify = false, repo = "repo/.")
print(empty == git::resolve("HEAD", "repo"))
print(git::resolve("HEAD~1", "repo") == amended)
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised", "raised", "true", "true", "true", "true", "true"]
    assert git_output(repo, ["show", "-s", "--format=%B", "HEAD~1"], env) == "amended\n\n# kept"
    assert git_output(repo, ["show", "HEAD~1:tracked.txt"], env) == "v2"
    assert git_output(repo, ["rev-list", "--count", "HEAD"], env) == "4"


def test_checkout_switches_to_local_branches_and_detaches_otherwise(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, repo = clone_local_remote(tmp_path, env, branches=["feat"])
    commit_file(repo, env, name="second.txt")
    git_run(repo, ["branch", "feature/x", "HEAD~1"], env)
    git_run(repo, ["tag", "v1", "HEAD~1"], env)
    git_run(repo, ["tag", "same", "HEAD~1"], env)
    git_run(repo, ["branch", "same", "HEAD"], env)
    head = git_head(repo, env)
    first = git_output(repo, ["rev-parse", "HEAD~1"], env)

    def step(index: int, target: str, branch: str | None, commit: str) -> str:
        shown = "None" if branch is None else f'Some("{branch}")'
        return (
            f'git::checkout("{target}", "repo")\n'
            + agl_check(f"b{index}", 'git::status(repo = "repo").branch', shown)
            + agl_check(f"h{index}", 'git::status(repo = "repo").head', f'Some("{commit}")')
        )

    out, result = run_git_program(
        step(0, "feature/x", "feature/x", first)
        + step(1, "main", "main", head)
        + step(2, "v1", None, first)
        + step(3, "same", "same", head)
        + step(4, head, None, head)
        + step(5, "origin/feat", None, git_output(repo, ["rev-parse", "origin/feat"], env))
        + step(6, "main", "main", head)
        + agl_raises('git::checkout("feat", "repo")')
        + agl_raises('git::checkout("nope", "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 14 + ["raised", "raised"]
    assert git_output(repo, ["rev-parse", "--abbrev-ref", "HEAD"], env) == "main"


def test_checkout_files_restores_from_index_or_from_a_source(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    (repo / "sub").mkdir()
    for name in ("from-index.txt", "from-head.txt", "a*", "ab"):
        commit_file(repo, env, name=name, content="v1\n")
        commit_file(repo, env, name=name, content="v2\n")
    for name in ("from-index.txt", "a*", "ab"):
        (repo / name).write_text("staged\n", encoding="utf-8")
        git_run(repo, ["add", name], env)
        (repo / name).write_text("worktree\n", encoding="utf-8")
    (repo / "from-head.txt").write_text("worktree\n", encoding="utf-8")
    git_run(repo, ["add", "from-head.txt"], env)

    _, result = run_git_program(
        """
git::checkout-files(["from-index.txt", "a*"], repo = "repo/sub")
git::checkout-files(["from-head.txt"], Some("HEAD"), "repo")
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert [(repo / n).read_text(encoding="utf-8") for n in ("from-index.txt", "a*", "ab")] == [
        "staged\n",
        "staged\n",
        "worktree\n",
    ]
    assert (repo / "from-head.txt").read_text(encoding="utf-8") == "v2\n"
    assert git_lines(repo, ["diff", "--cached", "--name-only"], env) == [
        "a*",
        "ab",
        "from-index.txt",
    ]


def _reset_repo(path: Path, env: dict[str, str]) -> Path:
    """Return a repo at ``HEAD`` = c2 (changing ``a.txt``) with ``k.txt`` modified locally."""

    repo = init_repo(path, env)
    commit_file(repo, env, name="k.txt", content="k1\n")
    commit_file(repo, env, name="a.txt", content="a1\n")
    commit_file(repo, env, name="a.txt", content="a2\n")
    (repo / "k.txt").write_text("local\n", encoding="utf-8")
    return repo


def test_reset_moves_head_and_applies_each_mode(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    modes = ("Soft", "Mixed", "Hard", "Keep")
    repos = {mode: _reset_repo(tmp_path / mode.lower(), env) for mode in modes}
    default = _reset_repo(tmp_path / "default", env)
    git_run(default, ["add", "k.txt"], env)
    targets = {mode: git_output(repo, ["rev-parse", "HEAD~1"], env) for mode, repo in repos.items()}
    _, result = run_git_program(
        "".join(f'git::reset("HEAD~1", {mode}, "{mode.lower()}")\n' for mode in modes)
        + 'git::reset(repo = "default")\n',
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error

    for mode, repo in repos.items():
        assert git_head(repo, env) == targets[mode], mode
    states = {
        mode: (git_lines(repo, ["status", "--porcelain"], env), (repo / "a.txt").read_text("utf-8"))
        for mode, repo in repos.items()
    }
    assert states == {
        "Soft": (["M  a.txt", " M k.txt"], "a2\n"),
        "Mixed": ([" M a.txt", " M k.txt"], "a2\n"),
        "Hard": ([], "a1\n"),
        "Keep": ([" M k.txt"], "a1\n"),
    }
    assert git_lines(default, ["status", "--porcelain"], env) == [" M k.txt"]


def test_reset_works_when_a_file_is_named_like_the_target_hash(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _reset_repo(tmp_path / "repo", env)
    target = git_output(repo, ["rev-parse", "HEAD~1"], env)
    (repo / target).write_text("x", encoding="utf-8")

    _, result = run_git_program(
        'git::reset("HEAD~1", repo = "repo")', tmp_path, monkeypatch, capsys
    )

    assert result.ok, result.error
    assert git_head(repo, env) == target


def test_reset_keep_refuses_to_lose_local_changes(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _reset_repo(tmp_path / "repo", env)
    head = git_head(repo, env)

    out, result = run_git_program(
        agl_raises('git::reset("HEAD~3", Keep, "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised"]
    assert git_head(repo, env) == head
    assert git_lines(repo, ["status", "--porcelain"], env) == [" M k.txt"]


def test_reset_files_restores_the_index_from_a_source(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _reset_repo(tmp_path / "repo", env)
    (repo / "sub").mkdir()
    for name in ("a*", "ab"):
        commit_file(repo, env, name=name, content="v1\n")
    for name in ("k.txt", "a*", "ab", "new.txt"):
        (repo / name).write_text("staged\n", encoding="utf-8")
        git_run(repo, ["add", name], env)
    everything = init_repo(tmp_path / "everything", env)
    (everything / "n.txt").write_text("x", encoding="utf-8")
    git_run(everything, ["add", "n.txt"], env)

    _, result = run_git_program(
        """
git::reset-files(["k.txt", "a*"], repo = "repo/sub")
git::reset-files(["a.txt"], "HEAD~3", "repo")
git::reset-files(["new.txt"], repo = "repo")
git::reset-files(["."], repo = "everything/.")
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert git_lines(repo, ["status", "--porcelain"], env) == [
        " M a*",
        "MM a.txt",
        "M  ab",
        " M k.txt",
        "?? new.txt",
    ]
    assert git_lines(everything, ["status", "--porcelain"], env) == ["?? n.txt"]


def _dirty_repo(path: Path, env: dict[str, str]) -> Path:
    """Return a repo with untracked files and directories and an ignored file."""

    repo = init_repo(path, env)
    (repo / "sub").mkdir()
    commit_file(repo, env, name=".gitignore", content="*.log\n")
    commit_file(repo, env, name="sub/tracked.txt")
    (repo / "dir").mkdir()
    for name in ("u.txt", "dir/in.txt", "sub/n.txt", "ign.log"):
        (repo / name).write_text("x", encoding="utf-8")
    return repo


def test_clean_removes_untracked_files_by_directory_ignored_and_path_filter(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenarios = {
        "default": 'git::clean(repo = "default")',
        "files-only": 'git::clean(directories = false, repo = "files-only")',
        "ignored": 'git::clean(ignored = true, repo = "ignored")',
        "filtered": 'git::clean(["dir"], repo = "filtered")',
        "from-sub": 'git::clean(repo = "from-sub/sub")',
        "root-dot": 'git::clean(["."], repo = "root-dot/sub")',
        "paths": 'git::clean(["sub"], directories = false, repo = "paths")',
    }
    repos = {name: _dirty_repo(tmp_path / name, env) for name in scenarios}
    (repos["paths"] / "sub" / "ud").mkdir()
    (repos["paths"] / "sub" / "ud" / "x.txt").write_text("x", encoding="utf-8")

    _, result = run_git_program("\n".join(scenarios.values()), tmp_path, monkeypatch, capsys)

    assert result.ok, result.error
    base = [".gitignore", "README.md", "ign.log", "sub/tracked.txt"]
    expected = {
        "default": base,
        "files-only": sorted([*base, "dir/in.txt"]),
        "ignored": [n for n in base if n != "ign.log"],
        "filtered": sorted([*base, "u.txt", "sub/n.txt"]),
        "from-sub": base,
        "root-dot": base,
        "paths": sorted([*base, "u.txt", "dir/in.txt"]),
    }
    assert {name: _tree(repo) for name, repo in repos.items()} == expected


def test_operand_style_functions_with_no_paths_run_no_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out, result = run_git_program(
        """
git::add([], "missing")
git::remove([], repo = "missing")
git::checkout-files([], repo = "missing")
git::reset-files([], repo = "missing")
print("done")
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert out.splitlines() == ["done"]
