"""History functions of ``std/git``: ``log``, ``show``, ``read-file``, ``resolve``, ancestry."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._agl_git_helpers import agl_check, agl_raises, run_git_program
from tests._git_helpers import commit_file, git_lines, git_output, git_run, init_repo

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
    head = git_output(repo, ["rev-parse", "HEAD"], env)
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
