"""History functions of ``std/git``: ``log``, ``show``, ``read-file``, ancestry, diffs."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._agl_git_helpers import agl_check, agl_raises, run_git_program
from tests._git_helpers import (
    commit_file,
    conflicted_merge,
    git_head,
    git_lines,
    git_output,
    git_run,
    init_repo,
    lines_of,
)

_IMPORTS = ("std/git::*", "std/text::join")

# Author and committer differ in name, time and offset, so a mix-up of the two is visible.
_PINNED = {
    "GIT_AUTHOR_NAME": "Ann Author",
    "GIT_AUTHOR_EMAIL": "ann@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0200",
    "GIT_COMMITTER_NAME": "Zoë Committer",
    "GIT_COMMITTER_EMAIL": "zoe@example.com",
    "GIT_COMMITTER_DATE": "1700003600 -0530",
}


def _hashes(call: str) -> str:
    """Return AgL printing the space-joined hashes of the commits *call* returns."""

    return f'print(join({call}.map(fn(c: Commit) => c.hash), " "))\n'


def _rev(repo: Path, env: dict[str, str], rev: str) -> str:
    return git_output(repo, ["rev-parse", rev], env)


def _revs(repo: Path, env: dict[str, str], *args: str) -> str:
    """Return the space-joined hashes `git log` lists for *args*."""

    return " ".join(git_lines(repo, ["log", "--format=%H", *args], env))


def _c1_commit(c1: str, initial: str) -> str:
    """Return AgL for the `Commit` that `_history_repo` makes as `c1`."""

    return f"""Commit("{c1}", ["{initial}"],
  Signature("Ann Author", "ann@example.com", 1700000000, "+0200"),
  Signature("Zoë Committer", "zoe@example.com", 1700003600, "-0530"),
  "add a", "first line\\nsecond line\\n\\nlast paragraph")"""


def _history_repo(tmp_path: Path, env: dict[str, str]) -> Path:
    """Build `initial <- c1 <- c2 <- merge <- c3`, with `side` (one commit) merged at `merge`.

    `c1` has a multi-line body and pinned identities; the rest use the default identity.
    """

    repo = init_repo(tmp_path / "repo", env)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_run(repo, ["add", "a.txt"], env)
    pinned = {**env, **_PINNED}
    git_run(
        repo,
        ["commit", "-q", "-m", "add a", "-m", "first line\nsecond line", "-m", "last paragraph"],
        pinned,
    )
    git_run(repo, ["checkout", "-qb", "side"], env)
    commit_file(repo, env, name="side.txt", message="side work")
    git_run(repo, ["checkout", "-q", "main"], env)
    (repo / "sub").mkdir()
    commit_file(repo, env, name="sub/b.txt", message="add b")
    git_run(repo, ["merge", "-q", "--no-ff", "-m", "merge side", "side"], env)
    commit_file(repo, env, name="a.txt", content="two\n", message="edit a")
    return repo


def test_log_selects_commits_by_revisions_paths_count_and_first_parent(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _history_repo(tmp_path, env)

    body = "".join(
        _hashes(call)
        for call in (
            'git::log(repo = "repo")',
            'git::log(["side"], repo = "repo")',
            'git::log(["main"], ["side"], repo = "repo")',
            'git::log(["main", "side"], ["main~2"], repo = "repo")',
            'git::log(paths = ["a.txt"], repo = "repo")',
            'git::log(paths = ["sub"], repo = "repo/sub")',
            'git::log(paths = ["."], repo = "repo/sub")',
            'git::log([], repo = "repo")',
            'git::log(max-count = Some(2), repo = "repo")',
            'git::log(first-parent = true, repo = "repo")',
            'git::log(["main", "side"], ["main"], repo = "repo")',
        )
    )
    body += agl_raises('git::log(["no-such-ref"], repo = "repo")')
    body += agl_raises('git::log(["HEAD^{tree}"], repo = "repo")')
    body += agl_raises('git::log(["HEAD"], ["HEAD^{tree}"], repo = "repo")')
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == [
        _revs(repo, env),
        _revs(repo, env, "side"),
        _revs(repo, env, "main", "^side"),
        _revs(repo, env, "main", "side", "^main~2"),
        _revs(repo, env, "--", "a.txt"),
        _revs(repo, env, "--", "sub"),
        _revs(repo, env, "--", ":(top)"),
        _revs(repo, env),
        _revs(repo, env, "-2"),
        _revs(repo, env, "--first-parent"),
        "",
        "raised",
        "raised",
        "raised",
    ]
    assert len(_revs(repo, env, "--first-parent").split()) < len(_revs(repo, env).split())


def test_log_parses_signatures_parents_subject_and_multi_line_bodies(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _history_repo(tmp_path, env)
    initial = _rev(repo, env, "main~4")
    c1 = _rev(repo, env, "main~3")
    side = _rev(repo, env, "side")
    c2 = _rev(repo, env, "main~2")

    body = agl_check(
        "pinned",
        'git::log(["main~3"], max-count = Some(1), repo = "repo")[0]',
        _c1_commit(c1, initial),
    )
    body += agl_check("root", 'git::log(["main~4"], repo = "repo")[0].parents.is-empty()', "true")
    body += agl_check(
        "merge", 'git::log(["main~1"], repo = "repo")[0].parents', f'["{c2}", "{side}"]'
    )
    body += agl_check("subject", 'git::log(["main~1"], repo = "repo")[0].subject', '"merge side"')
    body += agl_check("empty", 'git::log(["main~1"], repo = "repo")[0].body', '""')
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 5


def test_log_body_drops_only_trailing_newlines(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git_run(repo, ["add", "a.txt"], env)
    git_run(repo, ["commit", "-q", "--cleanup=verbatim", "-m", "subject\n\nbody  \t\n\n"], env)

    body = agl_check("body", 'git::show(repo = "repo").body', '"body  \\t"')
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"]


def test_log_ignores_user_configuration(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _history_repo(tmp_path, env)
    c1 = _rev(repo, env, "main~3")
    initial = _rev(repo, env, "main~4")
    settings = {
        "log.showSignature": "true",
        "log.follow": "true",
        "log.date": "iso",
        "i18n.logOutputEncoding": "ISO-8859-1",
    }
    sign = shutil.which("ssh-keygen") is not None  # Signature output is the only part needing it.
    if sign:
        key = tmp_path / "key"
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q"], check=True)
        settings |= {"gpg.format": "ssh", "user.signingkey": str(tmp_path / "key.pub")}
    for name, value in settings.items():
        git_run(repo, ["config", name, value], env)
    git_run(repo, ["mv", "sub/b.txt", "sub/c.txt"], env)
    commit_env = {**env, "SSH_AUTH_SOCK": ""}  # Never reach the developer's ssh agent.
    git_run(repo, ["commit", "-q", *(["-S"] if sign else []), "-m", "rename b"], commit_env)

    body = agl_check("renamed", 'git::log(paths = ["sub/c.txt"], repo = "repo").size()', "1")
    body += agl_check("signed", 'git::log(repo = "repo")[0].hash', f'"{_rev(repo, env, "HEAD")}"')
    body += agl_check(
        "c",
        f'git::log(["{c1}"], paths = ["a.txt"], max-count = Some(1), repo = "repo")[0]',
        _c1_commit(c1, initial),
    )
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 3


def test_show_returns_one_commit_without_walking_history(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _history_repo(tmp_path, env)
    git_run(repo, ["tag", "-a", "-m", "annotated", "v1", "main~3"], env)

    body = agl_check(
        "head", 'git::show(repo = "repo")', 'git::log(max-count = Some(1), repo = "repo")[0]'
    )
    body += agl_check(
        "side", 'git::show("side", "repo/sub")', 'git::log(["side"], repo = "repo")[0]'
    )
    body += agl_check("tag", 'git::show("v1", "repo")', 'git::log(["main~3"], repo = "repo")[0]')
    body += agl_check("parent", 'git::show("main~1^2", "repo").subject', '"side work"')
    body += agl_raises('git::show("no-such-ref", "repo")')
    body += agl_raises('git::show("--all", "repo")')
    body += agl_raises('git::show("HEAD^{tree}", "repo")')
    body += agl_raises('git::show("HEAD:a.txt", "repo")')
    body += agl_raises('git::show("^HEAD", "repo")')
    body += agl_raises('git::show("main~2..main", "repo")')
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 4 + ["raised"] * 6


def test_read_file_returns_blob_content_at_a_revision(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _history_repo(tmp_path, env)
    merge = _rev(repo, env, "main~1")
    commit_file(repo, env, name="sub/ünï.txt", content="no trailing newline", message="unicode")
    commit_file(repo, env, name="blank.txt", content="text\n\n\n", message="blank lines")
    (repo / "a.txt").write_text("worktree only\n", encoding="utf-8")

    body = agl_check("head", 'git::read-file("a.txt", repo = "repo")', '"two\\n"')
    body += agl_check("old", f'git::read-file("a.txt", "{merge}", "repo")', '"one\\n"')
    body += agl_check("sub", 'git::read-file("sub/b.txt", repo = "repo/sub")', '"content\\n"')
    body += agl_check(
        "raw", 'git::read-file("sub/ünï.txt", repo = "repo")', '"no trailing newline"'
    )
    body += agl_check("blank", 'git::read-file("blank.txt", repo = "repo")', '"text\\n\\n\\n"')
    body += agl_check("side", 'git::read-file("side.txt", "side", "repo")', '"content\\n"')
    body += agl_raises('git::read-file("missing.txt", repo = "repo")')
    body += agl_raises('git::read-file("a.txt", "no-such-ref", "repo")')
    body += agl_raises('git::read-file("sub", repo = "repo")')
    body += agl_raises('git::read-file("a.txt", "--help", "repo")')
    body += agl_raises('git::read-file("../a.txt", repo = "repo/sub")')
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 6 + ["raised"] * 5


def test_merge_base_and_is_ancestor_answer_for_related_and_unrelated_histories(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _history_repo(tmp_path, env)
    git_run(repo, ["checkout", "-q", "--orphan", "unrelated"], env)
    git_run(repo, ["commit", "-q", "--allow-empty", "-m", "unrelated root"], env)
    git_run(repo, ["checkout", "-q", "main"], env)
    fork = _rev(repo, env, "main~3")

    body = agl_check("fork", 'git::merge-base("main~2", "side", "repo")', f'Some("{fork}")')
    body += agl_check("swap", 'git::merge-base("side", "main~2", "repo")', f'Some("{fork}")')
    body += agl_check(
        "same", 'git::merge-base("side", "side", "repo/sub")', f'Some("{_rev(repo, env, "side")}")'
    )
    body += agl_check("none", 'git::merge-base("main", "unrelated", "repo")', "None")
    body += agl_raises('git::merge-base("main", "no-such-ref", "repo")')
    body += agl_raises('git::merge-base("--all", "main", "repo")')
    body += agl_check("parent", 'git::is-ancestor("main~2", "main", "repo")', "true")
    body += agl_check("self", 'git::is-ancestor("side", "side", "repo/sub")', "true")
    body += agl_check("child", 'git::is-ancestor("main", "main~2", "repo")', "false")
    body += agl_check("diverged", 'git::is-ancestor("side", "main~2", "repo")', "false")
    body += agl_check("orphan", 'git::is-ancestor("unrelated", "main", "repo")', "false")
    body += agl_raises('git::is-ancestor("no-such-ref", "main", "repo")')
    body += agl_raises('git::is-ancestor("--all", "main", "repo")')
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 4 + ["raised"] * 2 + ["ok"] * 5 + ["raised"] * 2


def test_resolve_returns_full_commit_hashes_and_resolve_option_none_for_unknown_refs(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="second.txt")
    head = git_head(repo, env)
    parent = git_output(repo, ["rev-parse", "HEAD~1"], env)

    out, result = run_git_program(
        """
print(git::resolve("HEAD", "repo"))
print(git::resolve("main~1", "repo"))
print(git::resolve("HEAD", "repo/.").size())
print(git::resolve?("HEAD", "repo").unwrap())
print(git::resolve?("no-such-ref", "repo").is-none())
print(git::resolve?("^HEAD", "repo").is-none())
print(git::resolve?("HEAD~1..HEAD", "repo").is-none())
print(git::resolve?("HEAD^{tree}", "repo").is-none())
try
  print(git::resolve("no-such-ref", "repo"))
catch GitError as e =>
  print(e.exit-code != 0)
try
  print(git::resolve("^HEAD", "repo"))
catch GitError as e =>
  print(e.exit-code != 0)
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert result.ok, result.error
    assert out.splitlines() == [head, parent, "40", head, *["true"] * 6]


def _edit(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")


def _diff_repo(tmp_path: Path, env: dict[str, str]) -> Path:
    """Build two commits (`HEAD~1` -> `HEAD`) plus staged and unstaged changes on top.

    `HEAD~1` -> `HEAD` adds, deletes, modifies, renames and type-changes a file, changes a binary
    file and adds paths containing a tab and a newline. Above `HEAD`, `staged.txt` is staged,
    `both.txt` is staged and then edited again, and `unstaged.txt`, `sub/inner.txt`, `star*.txt`
    and `starx.txt` are edited in the work tree only.
    """

    repo = init_repo(tmp_path / "repo", env)
    (repo / "sub").mkdir()
    names = (
        "mod",
        "del",
        "ren",
        "kind",
        "staged",
        "both",
        "unstaged",
        "star*",
        "starx",
        "sub/inner",
    )
    for name in names:
        commit_file(repo, env, name=f"{name}.txt", content=lines_of(name), message=name)
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    git_run(repo, ["add", "blob.bin"], env)
    git_run(repo, ["commit", "-qm", "blob"], env)

    # No trailing newline, so patches carry git's "no newline" marker.
    _edit(repo, "mod.txt", lines_of("mod") + "more")
    git_run(repo, ["rm", "-q", "del.txt"], env)
    git_run(repo, ["mv", "ren.txt", "renamed.txt"], env)
    _edit(repo, "renamed.txt", lines_of("ren") + "edited\n")
    (repo / "kind.txt").unlink()
    os.symlink("README.md", repo / "kind.txt")
    _edit(repo, "added.txt", lines_of("added"))
    _edit(repo, "tab\tname.txt", "tab\n")
    _edit(repo, "new\nline.txt", "newline\n")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    git_run(repo, ["add", "-A"], env)
    git_run(repo, ["commit", "-qm", "changes"], env)

    _edit(repo, "staged.txt", lines_of("staged") + "staged\n")
    _edit(repo, "both.txt", lines_of("both") + "staged\n")
    git_run(repo, ["add", "staged.txt", "both.txt"], env)
    _edit(repo, "both.txt", lines_of("both") + "staged\nunstaged\n")
    for name in ("unstaged.txt", "sub/inner.txt", "star*.txt", "starx.txt"):
        _edit(repo, name, (repo / name).read_text(encoding="utf-8") + "unstaged\n")
    return repo


_COMMITTED_CHANGES = """[
  FileChange("added.txt", Added),
  FileChange("blob.bin", Modified),
  FileChange("del.txt", Deleted),
  FileChange("kind.txt", TypeChanged),
  FileChange("mod.txt", Modified),
  FileChange("new\\nline.txt", Added),
  FileChange("renamed.txt", Renamed("ren.txt", 97)),
  FileChange("tab\\tname.txt", Added),
]"""

_UNCOMMITTED_PATHS = ("both.txt", "star*.txt", "starx.txt", "sub/inner.txt", "unstaged.txt")


def _modified(*paths: str) -> str:
    """Return the AgL array of `Modified` changes of *paths*."""

    return "[" + ", ".join(f'FileChange("{p}", Modified)' for p in paths) + "]"


def test_diff_lists_file_changes_for_every_diff_member(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _diff_repo(tmp_path, env)

    body = agl_check("worktree", 'git::diff(repo = "repo")', _modified(*_UNCOMMITTED_PATHS))
    body += agl_check(
        "staged",
        'git::diff(CommitToIndex(), repo = "repo")',
        _modified("both.txt", "staged.txt"),
    )
    body += agl_check(
        "staged-old",
        'git::diff(CommitToIndex("HEAD~1"), ["added.txt", "staged.txt"], repo = "repo")',
        '[FileChange("added.txt", Added), FileChange("staged.txt", Modified)]',
    )
    body += agl_check(
        "head",
        'git::diff(CommitToWorktree(), repo = "repo")',
        _modified("both.txt", "staged.txt", *_UNCOMMITTED_PATHS[1:]),
    )
    body += agl_check(
        "head-old",
        'git::diff(CommitToWorktree("HEAD~1"), ["added.txt", "unstaged.txt"], repo = "repo")',
        '[FileChange("added.txt", Added), FileChange("unstaged.txt", Modified)]',
    )
    body += agl_check(
        "commits", 'git::diff(CommitToCommit("HEAD~1", "HEAD"), repo = "repo")', _COMMITTED_CHANGES
    )
    body += agl_check(
        "reverse",
        'git::diff(CommitToCommit("HEAD", "HEAD~1"), ["renamed.txt", "ren.txt"], repo = "repo")',
        '[FileChange("ren.txt", Renamed("renamed.txt", 97))]',
    )
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 7


def test_diff_renames_can_be_turned_off_and_need_both_paths_in_the_filter(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _diff_repo(tmp_path, env)
    commits = 'CommitToCommit("HEAD~1", "HEAD")'

    body = agl_check(
        "off",
        f'git::diff({commits}, ["ren.txt", "renamed.txt"], renames = false, repo = "repo")',
        '[FileChange("ren.txt", Deleted), FileChange("renamed.txt", Added)]',
    )
    body += agl_check(
        "on",
        f'git::diff({commits}, ["ren.txt", "renamed.txt"], repo = "repo")',
        '[FileChange("renamed.txt", Renamed("ren.txt", 97))]',
    )
    body += agl_check(
        "half",
        f'git::diff({commits}, ["renamed.txt"], repo = "repo")',
        '[FileChange("renamed.txt", Added)]',
    )
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 3


def test_diff_paths_are_root_relative_literal_and_filter_from_a_subdirectory(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _diff_repo(tmp_path, env)

    body = agl_check(
        "sub", 'git::diff(paths = ["sub"], repo = "repo/sub")', _modified("sub/inner.txt")
    )
    body += agl_check("root", 'git::diff(repo = "repo/sub")', _modified(*_UNCOMMITTED_PATHS))
    body += agl_check(
        "dot", 'git::diff(paths = ["."], repo = "repo/sub")', _modified(*_UNCOMMITTED_PATHS)
    )
    body += agl_check(
        "literal", 'git::diff(paths = ["star*.txt"], repo = "repo")', _modified("star*.txt")
    )
    body += agl_check(
        "many",
        'git::diff(paths = ["sub", "starx.txt", "no-such"], repo = "repo/sub")',
        _modified("starx.txt", "sub/inner.txt"),
    )
    body += agl_check("none", 'git::diff(paths = ["no-such"], repo = "repo").is-empty()', "true")
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 6


def test_diff_and_diff_stat_report_unmerged_paths_during_a_conflict(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conflicted_merge(tmp_path / "repo", env, name="m.txt")
    unmerged = 'FileChange("m.txt", Unmerged)'
    unmerged_stat = 'FileStat("m.txt", Some(0), Some(0))'

    body = agl_check("index", 'git::diff(CommitToIndex(), repo = "repo")', f"[{unmerged}]")
    body += agl_check(
        "worktree",
        'git::diff(repo = "repo")',
        f'[{unmerged}, FileChange("m.txt", Modified)]',
    )
    body += agl_check(
        "index-stat", 'git::diff-stat(CommitToIndex(), repo = "repo")', f"[{unmerged_stat}]"
    )
    body += agl_check(
        "worktree-stat",
        'git::diff-stat(repo = "repo")',
        f'[{unmerged_stat}, FileStat("m.txt", Some(4), Some(0))]',
    )
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 4


def test_diff_operands_cannot_act_as_options_and_must_name_commits(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _diff_repo(tmp_path, env)

    body = "".join(
        agl_raises(call)
        for call in (
            'git::diff(CommitToWorktree("--output=pwned"), repo = "repo")',
            'git::diff(CommitToWorktree("--cached"), repo = "repo")',
            'git::diff(CommitToIndex("no-such-ref"), repo = "repo")',
            'git::diff(CommitToCommit("HEAD", "--stat"), repo = "repo")',
            'git::diff(CommitToWorktree("HEAD~1..HEAD"), repo = "repo")',
            'git::diff(CommitToCommit("HEAD^{tree}", "HEAD"), repo = "repo")',
            'git::diff-stat(CommitToWorktree("--output=pwned"), repo = "repo")',
            'git::patch(CommitToWorktree("--output=pwned"), repo = "repo")',
        )
    )
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["raised"] * 8
    assert not (repo / "pwned").exists()


def test_diff_stat_counts_lines_for_every_diff_member(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _diff_repo(tmp_path, env)

    body = agl_check(
        "worktree",
        'git::diff-stat(repo = "repo")',
        """[
  FileStat("both.txt", Some(1), Some(0)),
  FileStat("star*.txt", Some(1), Some(0)),
  FileStat("starx.txt", Some(1), Some(0)),
  FileStat("sub/inner.txt", Some(1), Some(0)),
  FileStat("unstaged.txt", Some(1), Some(0)),
]""",
    )
    body += agl_check(
        "staged",
        'git::diff-stat(CommitToIndex(), repo = "repo")',
        '[FileStat("both.txt", Some(1), Some(0)), FileStat("staged.txt", Some(1), Some(0))]',
    )
    body += agl_check(
        "head",
        'git::diff-stat(CommitToWorktree(), ["both.txt", "staged.txt"], repo = "repo")',
        '[FileStat("both.txt", Some(2), Some(0)), FileStat("staged.txt", Some(1), Some(0))]',
    )
    body += agl_check(
        "head-old",
        'git::diff-stat(CommitToWorktree("HEAD~1"), ["added.txt", "mod.txt"], repo = "repo")',
        '[FileStat("added.txt", Some(20), Some(0)), FileStat("mod.txt", Some(1), Some(0))]',
    )
    body += agl_check(
        "commits",
        'git::diff-stat(CommitToCommit("HEAD~1", "HEAD"), repo = "repo")',
        """[
  FileStat("added.txt", Some(20), Some(0)),
  FileStat("blob.bin", None, None),
  FileStat("del.txt", Some(0), Some(20)),
  FileStat("kind.txt", Some(1), Some(20)),
  FileStat("mod.txt", Some(1), Some(0)),
  FileStat("new\\nline.txt", Some(1), Some(0)),
  FileStat("renamed.txt", Some(1), Some(0)),
  FileStat("tab\\tname.txt", Some(1), Some(0)),
]""",
    )
    body += agl_check(
        "root",
        'git::diff-stat(CommitToCommit("HEAD~1", "HEAD"), ["sub", "mod.txt"], repo = "repo/sub")',
        '[FileStat("mod.txt", Some(1), Some(0))]',
    )
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 6


def test_diff_stat_reports_binary_files_and_renames_with_or_without_detection(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _diff_repo(tmp_path, env)
    pair = '["ren.txt", "renamed.txt", "blob.bin"]'
    commits = 'CommitToCommit("HEAD~1", "HEAD")'

    body = agl_check(
        "on",
        f'git::diff-stat({commits}, {pair}, repo = "repo")',
        '[FileStat("blob.bin", None, None), FileStat("renamed.txt", Some(1), Some(0))]',
    )
    body += agl_check(
        "off",
        f'git::diff-stat({commits}, {pair}, renames = false, repo = "repo")',
        """[
  FileStat("blob.bin", None, None),
  FileStat("ren.txt", Some(0), Some(20)),
  FileStat("renamed.txt", Some(21), Some(0)),
]""",
    )
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 2


_PATCH_SEPARATOR = "=" * 8


def _print_patches(*calls: str) -> str:
    """Return AgL printing the text each of *calls* returns, each followed by a separator line.

    `print` adds a newline, so a patch ending in one is followed by an empty line.
    """

    return "".join(f'print({call})\nprint("{_PATCH_SEPARATOR}")\n' for call in calls)


def _git_patches(repo: Path, env: dict[str, str], *arg_lists: list[str]) -> list[str]:
    """Return the `git diff` lines of each of *arg_lists*, in the form `_print_patches` prints."""

    lines: list[str] = []
    for args in arg_lists:
        lines += [*git_lines(repo, ["diff", "--no-color", "-M", *args], env), "", _PATCH_SEPARATOR]
    return lines


def test_patch_returns_git_diff_text_for_every_diff_member_and_filter(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _diff_repo(tmp_path, env)
    commits = 'CommitToCommit("HEAD~1", "HEAD")'
    star = ":(top,literal)star*.txt"

    body = _print_patches(
        'git::patch(repo = "repo")',
        'git::patch(CommitToIndex(), repo = "repo")',
        'git::patch(CommitToIndex("HEAD~1"), repo = "repo")',
        'git::patch(CommitToWorktree(), repo = "repo")',
        'git::patch(CommitToWorktree("HEAD~1"), repo = "repo")',
        f'git::patch({commits}, repo = "repo")',
        f'git::patch({commits}, context = 0, repo = "repo")',
        f'git::patch({commits}, context = -1, repo = "repo")',
        f'git::patch({commits}, renames = false, repo = "repo")',
        f'git::patch({commits}, ["ren.txt", "blob.bin"], repo = "repo/sub")',
        'git::patch(paths = ["sub", "star*.txt"], repo = "repo/sub")',
        'git::patch(paths = ["no-such"], repo = "repo")',
    )
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    expected = _git_patches(
        repo,
        env,
        [],
        ["--cached", "HEAD"],
        ["--cached", "HEAD~1"],
        ["HEAD"],
        ["HEAD~1"],
        ["HEAD~1", "HEAD"],
        ["-U0", "HEAD~1", "HEAD"],
        ["-U0", "HEAD~1", "HEAD"],
        ["--no-renames", "HEAD~1", "HEAD"],
        ["HEAD~1", "HEAD", "--", ":(top,literal)ren.txt", ":(top,literal)blob.bin"],
        ["--", ":(top,literal)sub", star],
        ["--", ":(top,literal)no-such"],
    )
    assert out.splitlines() == expected
    assert "No newline at end of file" in "\n".join(expected)
    assert "Binary files" in "\n".join(expected)


def test_diffs_ignore_user_configuration(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    (repo / "sub").mkdir()
    for name in ("old.txt", "sub/inner.txt"):
        commit_file(repo, env, name=name, content=lines_of(name), message=name)
    commit_file(repo, env, name=".gitattributes", content="*.conv diff=up\n")
    commit_file(repo, env, name="doc.conv", content="lower\n")
    head = git_head(repo, env)
    git_run(repo, ["update-index", "--add", "--cacheinfo", f"160000,{head},lib"], env)
    git_run(repo, ["commit", "-qm", "gitlink"], env)

    git_run(repo, ["mv", "old.txt", "new.txt"], env)
    inner = (repo / "sub/inner.txt").read_text(encoding="utf-8")
    _edit(repo, "sub/copy.txt", inner)
    git_run(repo, ["add", "sub/copy.txt"], env)
    _edit(repo, "sub/inner.txt", inner.replace("line 10", "LINE 10"))
    _edit(repo, "doc.conv", "changed\n")
    commit_file(init_repo(repo / "lib", env), env, name="more.txt")
    expected_patch = _git_patches(repo, env, ["HEAD"])

    external = tmp_path / "external.sh"
    external.write_text("#!/bin/sh\necho EXTERNAL\n", encoding="utf-8")
    external.chmod(0o755)
    order = tmp_path / "order"
    order.write_text("sub/inner.txt\nsub/copy.txt\nnew.txt\nlib\ndoc.conv\n", encoding="utf-8")
    settings = {
        "diff.orderFile": str(order),
        "diff.submodule": "log",
        "diff.renames": "copies",
        "diff.relative": "true",
        "diff.noprefix": "true",
        "diff.context": "7",
        "diff.external": str(external),
        "diff.up.textconv": "tr a-z A-Z <",
        "diff.ignoreSubmodules": "all",
        "color.ui": "always",
        "color.diff": "always",
    }
    for name, value in settings.items():
        git_run(repo, ["config", name, value], env)

    monkeypatch.setenv("GIT_DIFF_OPTS", "-u5")
    side = 'CommitToWorktree("HEAD")'
    body = agl_check(
        "changes",
        f'git::diff({side}, repo = "repo/sub")',
        """[
  FileChange("doc.conv", Modified),
  FileChange("lib", Modified),
  FileChange("new.txt", Renamed("old.txt", 100)),
  FileChange("sub/copy.txt", Added),
  FileChange("sub/inner.txt", Modified),
]""",
    )
    body += agl_check(
        "stats",
        f'git::diff-stat({side}, repo = "repo/sub")',
        """[
  FileStat("doc.conv", Some(1), Some(1)),
  FileStat("lib", Some(1), Some(1)),
  FileStat("new.txt", Some(0), Some(0)),
  FileStat("sub/copy.txt", Some(20), Some(0)),
  FileStat("sub/inner.txt", Some(1), Some(1)),
]""",
    )
    body += _print_patches(f'git::patch({side}, repo = "repo/sub")')
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=_IMPORTS)

    assert result.ok, result.error
    assert out.splitlines() == ["ok", "ok", *expected_patch]
