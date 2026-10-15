"""Working-tree and index functions of ``std/git``: ``status`` and ``is-clean``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests._agl_git_helpers import agl_check, run_git_program
from tests._git_helpers import clone_local_remote, commit_file, git_output, git_run, init_repo


def _lines(name: str) -> str:
    """Return 20 lines unique to *name*, so rename detection pairs only near-equal files."""

    return "".join(f"{name} line {n}\n" for n in range(20))


def test_status_reports_every_change_letter_with_rename_source_and_no_copies(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["config", "status.renames", "copies"], env)
    for name in ("mm", "sm", "um", "del-s", "del-u", "ts", "tu", "cp-src", "ren a", "ren-c"):
        commit_file(repo, env, name=f"{name}.txt", content=_lines(name), message=name)
    commit_file(repo, env, name="sp ace.txt", content=_lines("sp"), message="sp")

    (repo / "mm.txt").write_text(_lines("mm") + "staged\n", encoding="utf-8")
    git_run(repo, ["add", "mm.txt"], env)
    (repo / "mm.txt").write_text(_lines("mm") + "staged\nunstaged\n", encoding="utf-8")
    (repo / "sm.txt").write_text(_lines("sm") + "staged\n", encoding="utf-8")
    git_run(repo, ["add", "sm.txt"], env)
    (repo / "um.txt").write_text(_lines("um") + "unstaged\n", encoding="utf-8")
    git_run(repo, ["rm", "-q", "del-s.txt"], env)
    (repo / "del-u.txt").unlink()
    (repo / "ts.txt").unlink()
    os.symlink("README.md", repo / "ts.txt")
    git_run(repo, ["add", "ts.txt"], env)
    (repo / "tu.txt").unlink()
    os.symlink("README.md", repo / "tu.txt")
    (repo / "sp ace.txt").write_text(_lines("sp") + "unstaged\n", encoding="utf-8")
    (repo / "add.txt").write_text(_lines("add"), encoding="utf-8")
    git_run(repo, ["add", "add.txt"], env)
    git_run(repo, ["mv", "ren a.txt", "ren b.txt"], env)
    (repo / "ren-c.txt").rename(repo / "ren-d.txt")
    (repo / "ren-d.txt").write_text(_lines("ren-c") + "edited\n", encoding="utf-8")
    git_run(repo, ["add", "-A", "ren-c.txt", "ren-d.txt"], env)
    (repo / "cp-src.txt").write_text(_lines("cp-src") + "edited\n", encoding="utf-8")
    (repo / "cp-dst.txt").write_text(_lines("cp-src"), encoding="utf-8")
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
    commit_file(repo, env, name="f.txt", content=_lines("f"))
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
    merge = subprocess.run(["git", "merge", "other"], cwd=repo, env=env, capture_output=True)
    assert merge.returncode != 0

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
    heads = {
        name: git_output(tmp_path / name, ["rev-parse", "HEAD"], env)
        for name in ("repo", "synced", "local", "gone")
    }

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
    head = git_output(detached, ["rev-parse", "HEAD"], env)

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
    head = git_output(repo, ["rev-parse", "HEAD"], env)

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
    head = git_output(repo, ["rev-parse", "HEAD"], env)
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
