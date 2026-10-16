"""Working-tree and index functions of ``std/git``: ``status``, staging, commits, resets."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._agl_git_helpers import (
    agl_array,
    agl_check,
    agl_raises,
    run_git_program,
    status_literal,
    tracked_entry,
    untracked_entry,
)
from tests._git_helpers import (
    clone_local_remote,
    clone_repo,
    commit_file,
    git_fails,
    git_lines,
    git_output,
    git_rev,
    git_run,
    init_repo,
    lines_of,
    worktree_files,
    write_file,
    write_script,
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

    write_file(repo, "mm.txt", lines_of("mm") + "staged\n")
    git_run(repo, ["add", "mm.txt"], env)
    write_file(repo, "mm.txt", lines_of("mm") + "staged\nunstaged\n")
    write_file(repo, "sm.txt", lines_of("sm") + "staged\n")
    git_run(repo, ["add", "sm.txt"], env)
    write_file(repo, "um.txt", lines_of("um") + "unstaged\n")
    git_run(repo, ["rm", "-q", "del-s.txt"], env)
    (repo / "del-u.txt").unlink()
    (repo / "ts.txt").unlink()
    os.symlink("README.md", repo / "ts.txt")
    git_run(repo, ["add", "ts.txt"], env)
    (repo / "tu.txt").unlink()
    os.symlink("README.md", repo / "tu.txt")
    write_file(repo, "sp ace.txt", lines_of("sp") + "unstaged\n")
    write_file(repo, "add.txt", lines_of("add"))
    git_run(repo, ["add", "add.txt"], env)
    git_run(repo, ["mv", "ren a.txt", "ren b.txt"], env)
    (repo / "ren-c.txt").rename(repo / "ren-d.txt")
    write_file(repo, "ren-d.txt", lines_of("ren-c") + "edited\n")
    git_run(repo, ["add", "-A", "ren-c.txt", "ren-d.txt"], env)
    write_file(repo, "cp-src.txt", lines_of("cp-src") + "edited\n")
    write_file(repo, "cp-dst.txt", lines_of("cp-src"))
    git_run(repo, ["add", "cp-src.txt", "cp-dst.txt"], env)

    out = run_git_program(
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
    )

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
    write_file(repo, "m.txt", "theirs\n")
    write_file(repo, "aa.txt", "theirs\n")
    write_file(repo, "du.txt", "theirs\n")
    git_run(repo, ["rm", "-q", "ud.txt"], env)
    git_run(repo, ["mv", "f.txt", "h.txt"], env)
    git_run(repo, ["add", "-A"], env)
    git_run(repo, ["commit", "-qm", "other"], env)
    git_run(repo, ["checkout", "-q", "main"], env)
    write_file(repo, "m.txt", "ours\n")
    write_file(repo, "aa.txt", "ours\n")
    git_run(repo, ["rm", "-q", "du.txt"], env)
    write_file(repo, "ud.txt", "ours\n")
    git_run(repo, ["mv", "f.txt", "g.txt"], env)
    git_run(repo, ["add", "-A"], env)
    git_run(repo, ["commit", "-qm", "main"], env)
    git_fails(repo, ["merge", "other"], env)

    out = run_git_program(
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
    )

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
    write_file(repo, "tracked.txt", "changed\n")
    (repo / "dir").mkdir()
    write_file(repo, "dir/inner.txt", "x")
    write_file(repo, "new file.txt", "x")
    write_file(repo, "a  b\nc.txt", "x")
    write_file(repo, "a.log", "x")
    (repo / "build").mkdir()
    write_file(repo, "build/out.o", "x")
    git_run(repo, ["config", "status.showUntrackedFiles", "no"], env)

    tracked = tracked_entry("tracked.txt", unstaged="Modified")
    untracked = [
        untracked_entry(name) for name in ("a  b\\nc.txt", "dir/inner.txt", "new file.txt")
    ]
    ignored = ['StatusEntry("a.log", Ignored)', 'StatusEntry("build/", Ignored)']
    out = run_git_program(
        agl_check("both", 'git::status(repo = "repo").entries', agl_array(tracked, *untracked))
        + agl_check("none", 'git::status(false, false, "repo").entries', agl_array(tracked))
        + agl_check(
            "all",
            'git::status(true, true, "repo").entries',
            agl_array(tracked, *untracked, *ignored),
        )
        + agl_check(
            "ignored", 'git::status(false, true, "repo").entries', agl_array(tracked, *ignored)
        )
        + agl_check(
            "from-subdir",
            'git::status(repo = "repo/dir").entries',
            agl_array(tracked, *untracked),
        ),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"] * 5


def test_status_branch_upstream_and_divergence(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, repo = clone_local_remote(tmp_path, env, branches=["gone"])
    clone_repo(source, tmp_path / "synced", env)
    clone_repo(source, tmp_path / "local", env)
    git_run(tmp_path / "local", ["checkout", "-qb", "topic"], env)
    git_run(None, ["clone", "-q", "-b", "gone", str(source), str(tmp_path / "gone")], env)
    git_run(source, ["branch", "-qD", "gone"], env)
    git_run(tmp_path / "gone", ["fetch", "-q", "--prune"], env)
    commit_file(repo, env, name="local.txt", message="local")
    commit_file(source, env, name="remote.txt", message="remote")
    commit_file(source, env, name="remote2.txt", message="remote 2")
    git_run(repo, ["fetch", "-q"], env)
    heads = {name: git_rev(tmp_path / name, env) for name in ("repo", "synced", "local", "gone")}

    def state(name: str, branch: str, upstream: str, divergence: str) -> str:
        expected = status_literal(
            heads[name], branch=branch, upstream=upstream, divergence=divergence
        )
        return agl_check(name, f'git::status(repo = "{name}")', expected)

    out = run_git_program(
        state("repo", "main", 'Some("origin/main")', "Some(Divergence(1, 2))")
        + state("synced", "main", 'Some("origin/main")', "Some(Divergence(0, 0))")
        + state("local", "topic", "None", "None")
        + state("gone", "gone", 'Some("origin/gone")', "None"),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"] * 4


def test_status_of_unborn_and_detached_head(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_run(None, ["init", "-q", "-b", "trunk", str(tmp_path / "unborn")], env)
    write_file(tmp_path, "unborn/new.txt", "x")
    git_run(tmp_path / "unborn", ["add", "new.txt"], env)
    detached = init_repo(tmp_path / "detached", env)
    git_run(detached, ["checkout", "-q", "--detach"], env)
    head = git_rev(detached, env)

    out = run_git_program(
        agl_check(
            "unborn",
            'git::status(repo = "unborn")',
            'Status(None, Some("trunk"), None, None, '
            f"[{tracked_entry('new.txt', staged='Added')}])",
        )
        + agl_check(
            "detached",
            'git::status(repo = "detached")',
            f'Status(Some("{head}"), None, None, None, [])',
        ),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"] * 2


def test_status_of_branch_named_detached_sentinel(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env, branch="(detached)")
    head = git_rev(repo, env)

    out = run_git_program(
        agl_check(
            "status",
            'git::status(repo = "repo")',
            status_literal(head, branch="(detached)"),
        ),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"]


def test_status_of_branch_named_with_leading_parenthesis(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "paren", env, branch="(x")
    head = git_rev(repo, env)

    out = run_git_program(
        agl_check(
            "paren",
            'git::status(repo = "paren")',
            status_literal(head, branch="(x"),
        ),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"]


def test_status_ignores_submodule_ignore_configuration(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    head = git_rev(repo, env)
    git_run(repo, ["update-index", "--add", "--cacheinfo", f"160000,{head},sub"], env)
    git_run(repo, ["commit", "-qm", "gitlink"], env)
    git_run(repo, ["config", "diff.ignoreSubmodules", "all"], env)
    nested = init_repo(repo / "sub", env)
    commit_file(nested, env, name="more.txt")

    out = run_git_program(
        agl_check(
            "entries",
            'git::status(repo = "repo").entries',
            f"[{tracked_entry('sub', unstaged='Modified')}]",
        ),
        tmp_path,
        monkeypatch,
        capsys,
    )

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
    write_file(ignored, "a.log", "x")
    write_file(tmp_path, "untracked/new.txt", "x")
    modified = init_repo(tmp_path / "modified", env)
    write_file(modified, "README.md", "changed\n")
    staged = init_repo(tmp_path / "staged", env)
    write_file(staged, "new.txt", "x")
    git_run(staged, ["add", "new.txt"], env)

    out = run_git_program(
        """
for name in ["clean", "ignored", "untracked", "modified", "staged"] do
  print("%{name} %{git::is-clean(true, name)} %{git::is-clean(false, name)}")
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == [
        "clean true true",
        "ignored true true",
        "untracked false true",
        "modified false false",
        "staged false false",
    ]


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
        write_file(literal, name, "x")
    write_file(dot, "new.txt", "x")
    (dot / "gone.txt").unlink()

    run_git_program(
        """
git::add(["a*", "-dash"], "literal")
git::add(["."], "dot/sub")
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

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
    write_file(repo, "dirty.txt", "edited\n")
    git_run(repo, ["add", "dirty.txt"], env)
    write_file(repo, "dirty.txt", "edited again\n")

    out = run_git_program(
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

    assert out.splitlines() == ["raised"]
    assert git_lines(repo, ["ls-files"], env) == ["README.md", "ab"]
    assert sorted(worktree_files(repo)) == ["README.md", "ab", "kept.txt"]


def test_move_renames_root_relative_paths_from_a_subdirectory(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    (repo / "sub").mkdir()
    for name in ("old.txt", "-dash", "dir-file.txt"):
        write_file(repo, name, "x")
    git_run(repo, ["add", "--", "old.txt", "-dash", "dir-file.txt"], env)
    git_run(repo, ["commit", "-qm", "files"], env)

    out = run_git_program(
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
    write_file(repo, "tracked.txt", "v1\n")
    git_run(repo, ["add", "tracked.txt"], env)
    git_run(repo, ["commit", "-qm", "track"], env)
    write_file(repo, "tracked.txt", "v2\n")
    write_script(repo / ".git" / "hooks", "pre-commit", "#!/bin/sh\nexit 1\n")

    out = run_git_program(
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
    head = git_rev(repo, env)
    first = git_output(repo, ["rev-parse", "HEAD~1"], env)

    def step(index: int, target: str, branch: str | None, commit: str) -> str:
        shown = "None" if branch is None else f'Some("{branch}")'
        return (
            f'git::checkout("{target}", "repo")\n'
            + agl_check(f"b{index}", 'git::status(repo = "repo").branch', shown)
            + agl_check(f"h{index}", 'git::status(repo = "repo").head', f'Some("{commit}")')
        )

    out = run_git_program(
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
        write_file(repo, name, "staged\n")
        git_run(repo, ["add", name], env)
        write_file(repo, name, "worktree\n")
    write_file(repo, "from-head.txt", "worktree\n")
    git_run(repo, ["add", "from-head.txt"], env)

    run_git_program(
        """
git::checkout-files(["from-index.txt", "a*"], repo = "repo/sub")
git::checkout-files(["from-head.txt"], Some("HEAD"), "repo")
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

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
    write_file(repo, "k.txt", "local\n")
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
    run_git_program(
        "".join(f'git::reset("HEAD~1", {mode}, "{mode.lower()}")\n' for mode in modes)
        + 'git::reset(repo = "default")\n',
        tmp_path,
        monkeypatch,
        capsys,
    )

    for mode, repo in repos.items():
        assert git_rev(repo, env) == targets[mode], mode
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
    write_file(repo, target, "x")

    run_git_program('git::reset("HEAD~1", repo = "repo")', tmp_path, monkeypatch, capsys)

    assert git_rev(repo, env) == target


def test_reset_keep_refuses_to_lose_local_changes(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _reset_repo(tmp_path / "repo", env)
    head = git_rev(repo, env)

    out = run_git_program(
        agl_raises('git::reset("HEAD~3", Keep, "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["raised"]
    assert git_rev(repo, env) == head
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
        write_file(repo, name, "staged\n")
        git_run(repo, ["add", name], env)
    everything = init_repo(tmp_path / "everything", env)
    write_file(everything, "n.txt", "x")
    git_run(everything, ["add", "n.txt"], env)

    run_git_program(
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
        write_file(repo, name, "x")
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
    write_file(repos["paths"], "sub/ud/x.txt", "x")

    run_git_program("\n".join(scenarios.values()), tmp_path, monkeypatch, capsys)

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
    assert {name: sorted(worktree_files(repo)) for name, repo in repos.items()} == expected


def test_operand_style_functions_with_no_paths_run_no_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = run_git_program(
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

    assert out.splitlines() == ["done"]
