"""Family scopes of ``std/git``: branch, tag, stash, worktree, remote and config."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests._agl_git_helpers import (
    agl_array,
    agl_check,
    agl_raises,
    run_git_program,
    tracked_entry,
    untracked_entry,
)
from tests._git_helpers import (
    add_linked_worktree,
    clone_bare_remote,
    clone_local_remote,
    commit_file,
    forge_object,
    git_fails,
    git_lines,
    git_output,
    git_refs,
    git_rev,
    git_run,
    global_git_config,
    init_repo,
    record_rerere_resolution,
    write_file,
    write_script,
)


def _branch(name: str, commit: str, upstream: str | None = None, current: bool = False) -> str:
    """Return the AgL ``Branch`` literal."""
    tracked = "None" if upstream is None else f'Some("{upstream}")'
    return f'Branch("{name}", "{commit}", {tracked}, {str(current).lower()})'


def _tracking_clone(tmp_path: Path, env: dict[str, str]) -> Path:
    """Return a clone with local branches tracking a remote, a local, and a vanished branch."""
    _, repo = clone_local_remote(tmp_path, env, branches=["feat", "gone"])
    git_run(repo, ["branch", "feat", "origin/feat"], env)
    git_run(repo, ["branch", "gone", "origin/gone"], env)
    git_run(repo, ["update-ref", "-d", "refs/remotes/origin/gone"], env)
    git_run(repo, ["branch", "--no-track", "solo"], env)
    git_run(repo, ["branch", "loc"], env)
    git_run(repo, ["branch", "--set-upstream-to=main", "loc"], env)
    git_run(repo, ["symbolic-ref", "refs/heads/alias", "refs/heads/main"], env)
    return repo


def test_branch_list_current_and_upstream(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _tracking_clone(tmp_path, env)
    # Configuration that changes the output of `git branch` but not of `for-each-ref`.
    for key, value in (("branch.sort", "-refname"), ("color.ui", "always")):
        git_run(repo, ["config", key, value], env)
    main = git_rev(repo, env, "main")
    feat = git_rev(repo, env, "feat")
    gone = git_rev(repo, env, "gone")

    def local(current: str | None) -> str:
        return agl_array(
            _branch("feat", feat, "origin/feat", current == "feat"),
            _branch("gone", gone, "origin/gone", current == "gone"),
            _branch("loc", main, "main", current == "loc"),
            _branch("main", main, "origin/main", current == "main"),
            _branch("solo", main, None, current == "solo"),
        )

    remote = agl_array(_branch("origin/feat", feat), _branch("origin/main", main))
    git_run(tmp_path, ["init", "-q", "-b", "trunk", "fresh"], env)

    out = run_git_program(
        agl_check("local", 'git::branch::list(repo = "repo")', local("main"))
        + agl_check("remote", 'git::branch::list(true, "repo")', remote)
        + agl_check("current", 'git::branch::current("repo")', 'Some("main")')
        + agl_check("unborn", 'git::branch::current("fresh")', 'Some("trunk")')
        + agl_check("empty", 'git::branch::list(repo = "fresh").size()', "0")
        + agl_check("up-remote", 'git::branch::upstream("gone", "repo")', 'Some("origin/gone")')
        + agl_check("up-local", 'git::branch::upstream("loc", "repo")', 'Some("main")')
        + agl_check("up-none", 'git::branch::upstream("solo", "repo")', "None")
        + agl_check("up-missing", 'git::branch::upstream("nope", "repo")', "None")
        + 'git::checkout("feat", "repo")\n'
        + agl_check("on-feat", 'git::branch::list(repo = "repo")', local("feat"))
        + 'git::checkout("HEAD", "repo")\n'
        + agl_check("detached", 'git::branch::current("repo")', "None")
        + agl_check("none-current", 'git::branch::list(repo = "repo")', local(None))
        + agl_raises('git::branch::current("absent")')
        + agl_raises('git::branch::list(repo = "absent")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"] * 12 + ["raised", "raised"]


def test_branch_create_delete_rename_and_upstream(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, repo = clone_bare_remote(init_repo(tmp_path / "source", env), tmp_path, env)
    published = git_rev(repo, env)
    commit_file(repo, env, name="more.txt", message="more")
    tip = git_rev(repo, env)
    git_run(repo, ["branch", "keep"], env)
    git_run(repo, ["checkout", "-qb", "unmerged"], env)
    commit_file(repo, env, name="unmerged.txt", message="unmerged")
    git_run(repo, ["checkout", "-q", "main"], env)
    # Configuration that would make `branch` track its start point.
    git_run(repo, ["config", "branch.autoSetupMerge", "always"], env)
    git_run(repo, ["config", "branch.autoSetupRebase", "always"], env)

    out = run_git_program(
        'git::branch::create("a", repo = "repo")\n'
        'git::branch::create("b", "origin/main", repo = "repo")\n'
        + agl_raises('git::branch::create("a", "origin/main", repo = "repo")')
        + 'git::branch::create("a", "origin/main", true, "repo")\n'
        'git::branch::delete("a", repo = "repo")\n'
        + agl_raises('git::branch::delete("unmerged", repo = "repo")')
        + agl_raises('git::branch::delete("nope", true, "repo")')
        + 'git::branch::delete("unmerged", true, "repo")\n'
        'git::branch::rename("b", "c", repo = "repo")\n'
        + agl_raises('git::branch::rename("c", "keep", repo = "repo")')
        + 'git::branch::rename("c", "keep", true, "repo")\n'
        + agl_check("untracked", 'git::branch::upstream("keep", "repo")', "None")
        + 'git::branch::set-upstream("keep", "origin/main", "repo")\n'
        + agl_check("tracked", 'git::branch::upstream("keep", "repo")', 'Some("origin/main")')
        + 'git::branch::set-upstream("keep", "main", "repo")\n'
        + agl_check("local", 'git::branch::upstream("keep", "repo")', 'Some("main")')
        + 'git::branch::unset-upstream("keep", "repo")\n'
        + agl_check("unset", 'git::branch::upstream("keep", "repo")', "None")
        + agl_raises('git::branch::unset-upstream("keep", "repo")')
        + agl_raises('git::branch::set-upstream("keep", "nope", "repo")')
        + agl_raises('git::branch::set-upstream("nope", "main", "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == [
        "raised",
        "raised",
        "raised",
        "raised",
        "ok",
        "ok",
        "ok",
        "ok",
        "raised",
        "raised",
        "raised",
    ]
    assert git_refs(repo, env, "heads") == {"main": tip, "keep": published}
    for key in ("remote", "merge", "rebase"):
        git_fails(repo, ["config", "--get", f"branch.keep.{key}"], env)


def test_branch_create_ignores_submodule_branch_propagation(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    sub = init_repo(tmp_path / "sub", env)
    git_run(
        repo, ["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "s"], env
    )
    git_run(repo, ["commit", "-qm", "add submodule"], env)
    for key in ("submodule.recurse", "submodule.propagateBranches"):
        git_run(repo, ["config", key, "true"], env)

    run_git_program(
        'git::branch::create("topic", repo = "repo")',
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert git_refs(repo, env, "heads").keys() == {"main", "topic"}
    assert git_refs(repo / "s", env, "heads").keys() == {"main"}


def test_branch_names_may_look_like_other_namespaces(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    main = git_rev(repo, env)
    git_run(repo, ["branch", "refs/remotes/x"], env)
    git_run(repo, ["branch", "--track", "y", "refs/heads/refs/remotes/x"], env)
    git_run(repo, ["symbolic-ref", "HEAD", "refs/heads/refs/remotes/x"], env)
    expected = agl_array(
        _branch("main", main),
        _branch("refs/remotes/x", main, None, True),
        _branch("y", main, "refs/remotes/x"),
    )

    out = run_git_program(
        agl_check("all", 'git::branch::list(repo = ".")', expected)
        + agl_check("current", 'git::branch::current(".")', 'Some("refs/remotes/x")')
        + agl_check("upstream", 'git::branch::upstream("y", ".")', 'Some("refs/remotes/x")'),
        repo,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"] * 3


def test_branch_operands_are_never_options(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, repo = clone_local_remote(tmp_path, env)
    # On `work`, which tracks `origin/main`, `--unset-upstream` and `--set-upstream-to=main`
    # would act on the current branch if their operand were read as an option.
    git_run(repo, ["checkout", "-qb", "work", "origin/main"], env)
    before = (git_refs(repo, env, "heads"), git_output(repo, ["config", "--list", "--local"], env))
    calls = [
        'create("--force", repo = "repo")',
        'create("new", "--force", repo = "repo")',
        'delete("--list", repo = "repo")',
        'delete("--force", true, "repo")',
        'rename("--force", "x", repo = "repo")',
        'rename("main", "--force", repo = "repo")',
        'set-upstream("work", "--force", "repo")',
        'set-upstream("--force", "main", "repo")',
        'unset-upstream("--force", "repo")',
        'compare("--branches=x", "main", "repo")',
        'compare("main", "--all", "repo")',
    ]

    out = run_git_program(
        "".join(agl_raises(f"git::branch::{call}") for call in calls),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["raised"] * len(calls)
    after = (git_refs(repo, env, "heads"), git_output(repo, ["config", "--list", "--local"], env))
    assert after == before


def test_branch_compare_counts_commits_on_each_side(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["tag", "base"], env)
    git_run(repo, ["checkout", "-qb", "topic"], env)
    commit_file(repo, env, name="t1.txt", message="t1")
    commit_file(repo, env, name="t2.txt", message="t2")
    git_run(repo, ["checkout", "-q", "main"], env)
    commit_file(repo, env, name="m1.txt", message="m1")
    git_run(repo, ["checkout", "-q", "--detach", "base"], env)

    out = run_git_program(
        agl_check("ahead", 'git::branch::compare("topic", "main", "repo")', "Divergence(2, 1)")
        + agl_check("behind", 'git::branch::compare("main", "topic", "repo")', "Divergence(1, 2)")
        + agl_check("same", 'git::branch::compare("main", "main", "repo")', "Divergence(0, 0)")
        + agl_check("head", 'git::branch::compare("HEAD", "topic", "repo")', "Divergence(0, 2)")
        + agl_raises('git::branch::compare("topic..main", "main", "repo")')
        + agl_raises('git::branch::compare("topic", "nope", "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok", "ok", "ok", "ok", "raised", "raised"]


def _tag(name: str, target: str, message: str | None = None) -> str:
    """Return the AgL ``Tag`` literal; *message* is AgL string-literal content."""
    annotation = "None" if message is None else f'Some("{message}")'
    return f'Tag("{name}", "{target}", {annotation})'


def _forge_signed_tag(repo: Path, env: dict[str, str], name: str, commit: str) -> None:
    """Create the tag *name* of *commit* whose message ends in a (fake) PGP signature block."""
    signature = "-----BEGIN PGP SIGNATURE-----\n\nAAAA\n-----END PGP SIGNATURE-----\n"
    content = (
        f"object {commit}\ntype commit\ntag {name}\ntagger T <t@example.com> 1700000000 +0000\n"
        f"\nsigned\n\nmessage\n{signature}"
    )
    forge_object(repo, env, "tag", content, f"refs/tags/{name}")


def test_tag_create_list_and_delete(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    first = git_rev(repo, env)
    commit_file(repo, env, name="more.txt", message="a\n\nbody of the commit")
    second = git_rev(repo, env)
    git_run(repo, ["tag", "-a", "inner", "-m", "inner message"], env)
    git_run(repo, ["tag", "-a", "outer", "-m", "outer message", "inner"], env)
    # A second nested tag, of the older commit, shows the peeled targets stay in step.
    git_run(repo, ["tag", "-a", "old", "-m", "old message", "HEAD~1"], env)
    git_run(repo, ["tag", "-a", "wrapped", "-m", "wrapped message", "old"], env)
    _forge_signed_tag(repo, env, "signed", second)
    tree = git_rev(repo, env, "HEAD^{tree}")
    git_run(repo, ["tag", "tree", "HEAD^{tree}"], env)
    # Signing is requested by configuration, but no key exists; sorting must not matter.
    for key, value in (
        ("tag.gpgSign", "true"),
        ("tag.forceSignAnnotated", "true"),
        ("tag.sort", "-refname"),
    ):
        git_run(repo, ["config", key, value], env)
    multi = "subject\\n\\nbody # kept\\n# also kept\\n\\nlast"

    def listing(name: str, *tags: str) -> str:
        return agl_check(name, 'git::tag::list("repo")', agl_array(*tags))

    expected_before = [
        _tag("inner", second, "inner message"),
        _tag("old", first, "old message"),
        _tag("outer", second, "outer message"),
        _tag("signed", second, "signed\\n\\nmessage"),
        _tag("tree", tree),
        _tag("wrapped", first, "wrapped message"),
    ]
    expected_after = [
        _tag("ann-multi", first, multi),
        _tag("ann1", second, "single"),
        _tag("empty", second, ""),
        _tag("inner", second, "inner message"),
        _tag("light", first),
        _tag("old", first, "old message"),
        _tag("outer", second, "outer message"),
        _tag("signed", second, "signed\\n\\nmessage"),
        _tag("tree", tree),
        _tag("wrapped", first, "wrapped message"),
    ]

    out = run_git_program(
        listing("before", *expected_before)
        + 'git::tag::create("light", repo = "repo")\n'
        + agl_raises('git::tag::create("light", repo = "repo")')
        + 'git::tag::create("light", "HEAD~1", None, true, "repo")\n'
        + 'git::tag::create("tmp", repo = "repo")\n'
        + 'git::tag::create("ann1", message = Some("single"), repo = "repo")\n'
        + 'git::tag::create("empty", message = Some(""), repo = "repo")\n'
        + 'git::tag::create("ann-multi", "HEAD~1", Some("%s"), repo = "repo")\n' % multi
        + agl_raises('git::tag::create("ann1", message = Some("again"), repo = "repo")')
        + 'git::tag::delete("tmp", "repo")\n'
        + agl_raises('git::tag::delete("tmp", "repo")')
        + listing("after", *expected_after)
        + agl_raises('git::tag::list("absent")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok", "raised", "raised", "raised", "ok", "raised"]
    types = {name: git_output(repo, ["cat-file", "-t", name], env) for name in ("light", "ann1")}
    assert types == {"light": "commit", "ann1": "tag"}


def test_tag_force_and_operands_are_never_options(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="more.txt", message="more")
    second = git_rev(repo, env)
    git_run(repo, ["tag", "light", "HEAD~1"], env)
    git_run(repo, ["tag", "-a", "ann", "-m", "old", "HEAD~1"], env)
    calls = [
        'create("--force", repo = "repo")',
        'create("--force", message = Some("m"), repo = "repo")',
        'create("new", "--force", repo = "repo")',
        'delete("--force", "repo")',
        'delete("-i", "repo")',
    ]

    out = run_git_program(
        'git::tag::create("light", "HEAD", None, true, "repo")\n'
        'git::tag::create("ann", "HEAD", Some("new"), true, "repo")\n'
        + "".join(agl_raises(f"git::tag::{call}") for call in calls)
        + agl_check(
            "tags",
            'git::tag::list("repo")',
            agl_array(_tag("ann", second, "new"), _tag("light", second)),
        ),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["raised"] * len(calls) + ["ok"]
    assert git_refs(repo, env, "tags").keys() == {"light", "ann"}


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

    out = run_git_program(
        """
for name in ["main", "feature/x", "a-tag", "feat", "origin/feat", "--force", "nope"] do
  print("%{name} %{git::branch::exists(name, "repo")}")
"""
        + agl_raises('git::branch::exists("main", "plain")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

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


_FS_IMPORTS = ("std/fs",)


def _stash_entry(index: int, message: str) -> str:
    """Return the AgL ``StashEntry`` literal of ``stash@{index}`` in ``repo``."""
    name = f"stash@{{{index}}}"
    return f'StashEntry({index}, "{name}", git::resolve("{name}", "repo"), "{message}")'


def _modified(path: str) -> str:
    return tracked_entry(path, unstaged="Modified")


def _staged(path: str) -> str:
    return tracked_entry(path, staged="Modified")


def _repo_status(name: str, *entries: str) -> str:
    """Return AgL checking that the status entries of ``repo`` are exactly *entries*."""
    if not entries:
        return agl_check(name, 'git::status(repo = "repo").entries.size()', "0")
    return agl_check(name, 'git::status(repo = "repo").entries', agl_array(*entries))


def _stash_list(repo: Path, env: dict[str, str]) -> list[str]:
    return git_lines(repo, ["stash", "list", "--format=%gs"], env)


def test_stash_push_list_and_drop(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="a.txt", content="a\n")
    commit_file(repo, env, name="b.txt", content="b\n")
    # The default message of a push: the branch, the abbreviated `HEAD` and its subject.
    wip = f"WIP on main: {git_output(repo, ['rev-parse', '--short', 'HEAD'], env)} commit"
    # Output in a non-UTF-8 encoding would not decode.
    git_run(repo, ["config", "i18n.logOutputEncoding", "latin1"], env)

    out = run_git_program(
        agl_check("nothing", 'git::stash::push(repo = "repo")', "None::[StashEntry]")
        + agl_check("no-entries", 'git::stash::list("repo").size()', "0")
        + 'fs::write("repo/a.txt", "one\\n")\n'
        + 'fs::write("repo/b.txt", "two\\n")\n'
        + 'fs::write("repo/u.txt", "new\\n")\n'
        + agl_check(
            "paths",
            'git::stash::push(Some("café"), ["a.txt"], repo = "repo")',
            f"Some({_stash_entry(0, 'On main: café')})",
        )
        + _repo_status("after-paths", _modified("b.txt"), untracked_entry("u.txt"))
        + agl_check("only-one", 'git::stash::list("repo").size()', "1")
        + agl_check(
            "untracked",
            'git::stash::push(None, [], true, false, "repo")',
            f"Some({_stash_entry(0, wip)})",
        )
        + _repo_status("after-untracked")
        + agl_check("u-gone", 'fs::exists("repo/u.txt")', "false")
        + agl_check("older", 'git::stash::list("repo")[1]', _stash_entry(1, "On main: café"))
        + agl_check("newer", 'git::stash::list("repo")[0].name', '"stash@{0}"')
        + 'fs::write("repo/a.txt", "three\\n")\n'
        + 'git::add(["a.txt"], "repo")\n'
        + 'fs::write("repo/b.txt", "four\\n")\n'
        + agl_check(
            "kept",
            'git::stash::push(None, [], false, true, "repo")',
            f"Some({_stash_entry(0, wip)})",
        )
        + _repo_status("after-keep", _staged("a.txt"))
        + agl_check("three", 'git::stash::list("repo").size()', "3")
        + 'git::stash::drop("stash@{1}", "repo")\n'
        + agl_check("dropped", 'git::stash::list("repo")[1]', _stash_entry(1, "On main: café"))
        + 'git::stash::drop(repo = "repo")\n'
        + agl_check(
            "default", 'git::stash::list("repo")', agl_array(_stash_entry(0, "On main: café"))
        )
        + 'git::stash::clear("repo")\n'
        + agl_check("cleared", 'git::stash::list("repo").size()', "0")
        + agl_raises('git::stash::drop("stash@{0}", "repo")')
        + agl_raises('git::stash::push(None, ["nope.txt"], repo = "repo")')
        + agl_raises('git::stash::push(repo = "absent")')
        + agl_raises('git::stash::list("absent")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=_FS_IMPORTS,
    )

    assert out.splitlines() == ["ok"] * 16 + ["raised"] * 4


_RESET_HARD = 'git::reset("HEAD", Hard, "repo")\n'


def test_stash_apply_and_pop(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="a.txt", content="a\n")
    write_file(repo, "a.txt", "old\n")
    git_run(repo, ["stash", "push", "-q", "-m", "old"], env)
    write_file(repo, "a.txt", "new\n")
    git_run(repo, ["add", "a.txt"], env)
    git_run(repo, ["stash", "push", "-q", "-m", "new"], env)

    out = run_git_program(
        agl_check("old", 'git::stash::apply("stash@{1}", repo = "repo")', "Applied")
        + _repo_status("old-status", _modified("a.txt"))
        + agl_check("old-text", 'fs::read("repo/a.txt")', '"old\\n"')
        + agl_check("kept", 'git::stash::list("repo").size()', "2")
        + _RESET_HARD
        + agl_check("plain", 'git::stash::apply(repo = "repo")', "Applied")
        + _repo_status("plain-status", _modified("a.txt"))
        + _RESET_HARD
        + agl_check("indexed", 'git::stash::apply("stash@{0}", true, "repo")', "Applied")
        + _repo_status("indexed-status", _staged("a.txt"))
        + _RESET_HARD
        + agl_check("popped", 'git::stash::pop(repo = "repo")', "Applied")
        + agl_check("new-text", 'fs::read("repo/a.txt")', '"new\\n"')
        + agl_check(
            "one-left", 'git::stash::list("repo")', agl_array(_stash_entry(0, "On main: old"))
        )
        + _RESET_HARD
        + agl_check("popped-old", 'git::stash::pop("stash@{0}", false, "repo")', "Applied")
        + agl_check("none-left", 'git::stash::list("repo").size()', "0")
        + agl_raises('git::stash::apply(repo = "repo")')
        + agl_raises('git::stash::pop(repo = "repo")')
        + agl_raises('git::stash::apply(repo = "absent")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=_FS_IMPORTS,
    )

    assert out.splitlines() == ["ok"] * 13 + ["raised"] * 3


def test_stash_conflicts_keep_the_entry(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    record_rerere_resolution(repo, env)
    git_run(repo, ["checkout", "-qb", "stasher", "main~1"], env)
    write_file(repo, "f.txt", "theirs\n")
    git_run(repo, ["stash", "push", "-q", "-m", "theirs"], env)
    git_run(repo, ["checkout", "-q", "main"], env)

    out = run_git_program(
        'fs::write("repo/f.txt", "dirty\\n")\n'
        + agl_raises('git::stash::apply(repo = "repo")')
        + agl_raises('git::stash::pop(repo = "repo")')
        + _RESET_HARD
        + agl_check("popped", 'git::stash::pop(repo = "repo")', 'Conflicted(["f.txt"])')
        + agl_check("kept", 'git::stash::list("repo").size()', "1")
        + agl_check("status", 'git::status(repo = "repo").entries.size()', "1")
        + agl_raises('git::stash::apply(repo = "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=_FS_IMPORTS,
    )

    assert out.splitlines() == ["raised", "raised", "ok", "ok", "ok", "raised"]
    assert git_output(repo, ["status", "--porcelain"], env) == "UU f.txt"
    assert _stash_list(repo, env) == ["On stasher: theirs"]


def test_stash_pop_of_a_commit_hash_changes_nothing(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="a.txt", content="a\n")
    write_file(repo, "a.txt", "stashed\n")
    git_run(repo, ["stash", "push", "-q", "-m", "kept"], env)
    entry = git_rev(repo, env, "stash@{0}")

    out = run_git_program(
        agl_raises(f'git::stash::pop("{entry}", repo = "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=_FS_IMPORTS,
    )

    assert out.splitlines() == ["raised"]
    assert git_output(repo, ["status", "--porcelain"], env) == ""
    assert (repo / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert _stash_list(repo, env) == ["On main: kept"]


def test_stash_push_identical_to_the_newest_entry_returns_none(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="a.txt", content="a\n")
    # With both dates fixed, the same changes stash to the same commit, as within one second.
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2020-01-01T00:00:00Z")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2020-01-01T00:00:00Z")

    out = run_git_program(
        'fs::write("repo/a.txt", "one\\n")\n'
        + agl_check(
            "first",
            'git::stash::push(Some("m"), repo = "repo")',
            f"Some({_stash_entry(0, 'On main: m')})",
        )
        + 'fs::write("repo/a.txt", "one\\n")\n'
        + agl_check("same", 'git::stash::push(Some("m"), repo = "repo")', "None::[StashEntry]"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=_FS_IMPORTS,
    )

    assert out.splitlines() == ["ok", "ok"]
    # The changes were stashed all the same: git logs no second entry for an unchanged ref.
    assert _stash_list(repo, env) == ["On main: m"]
    assert git_output(repo, ["status", "--porcelain"], env) == ""


def test_stash_operands_are_never_options(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="a.txt", content="a\n")
    write_file(repo, "a.txt", "stashed\n")
    git_run(repo, ["stash", "push", "-q"], env)
    write_file(repo, "u.txt", "untracked\n")
    # Read as options, `--quiet` would drop `stash@{0}` and `--index` would apply it.
    calls = [
        'drop("--quiet", repo = "repo")',
        'apply("--index", repo = "repo")',
        'pop("--index", repo = "repo")',
        'push(None, ["--all"], repo = "repo")',
    ]

    out = run_git_program(
        "".join(agl_raises(f"git::stash::{call}") for call in calls)
        + 'fs::write("repo/a.txt", "changed\\n")\n'
        + agl_check(
            "message",
            'git::stash::push(Some("--include-untracked"), repo = "repo")',
            f"Some({_stash_entry(0, 'On main: --include-untracked')})",
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=_FS_IMPORTS,
    )

    assert out.splitlines() == ["raised"] * len(calls) + ["ok"]
    messages = _stash_list(repo, env)
    assert len(messages) == 2
    assert messages[0] == "On main: --include-untracked"
    assert (repo / "u.txt").exists()


def _worktree(
    path: Path,
    head: str | None,
    branch: str | None,
    *,
    bare: bool = False,
    locked: str | None = None,
) -> str:
    """Return the AgL ``Worktree`` literal; *head* is an AgL ``ref`` expression, or ``None``."""
    head_ = "None" if head is None else f"Some({head})"
    branch_ = "None" if branch is None else f'Some("{branch}")'
    locked_ = "None" if locked is None else f'Some("{locked}")'
    return f'Worktree("{path}", {head_}, {branch_}, {str(bare).lower()}, {locked_}, None)'


def _find_worktree(path: Path, repo: str = "repo") -> str:
    """Return the AgL expression finding the work tree of *repo* at *path*."""
    return f'git::worktree::list("{repo}").find(fn(w: Worktree) => w.path == "{path}")'


def _worktree_at(name: str, path: Path, expected: str, repo: str = "repo") -> str:
    """Return AgL checking that the work tree at *path* is exactly *expected*."""
    return agl_check(name, _find_worktree(path, repo), f"Some({expected})")


def _no_worktree_at(name: str, path: Path, repo: str = "repo") -> str:
    return agl_check(name, _find_worktree(path, repo), "None::[Worktree]")


def test_worktree_add_move_lock_and_remove(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, repo = clone_local_remote(tmp_path, env, branches=["feat"])
    git_run(repo, ["branch", "other"], env)
    # Configuration that would make a new branch track its start point, or guess a remote.
    for key, value in (
        ("branch.autoSetupMerge", "always"),
        ("branch.autoSetupRebase", "always"),
        ("worktree.guessRemote", "true"),
    ):
        git_run(repo, ["config", key, value], env)
    root = tmp_path.resolve()
    head = 'git::resolve("HEAD", "repo")'
    feat = 'git::resolve("origin/feat", "repo")'
    detached = _worktree(root / "wt-det", head, None)
    topic = _worktree(root / "wt-new", head, "topic")
    moved = _worktree(root / "wt-moved", head, "topic")
    locked = _worktree(root / "wt-moved", head, "topic", locked="why not\\nsecond")
    other = _worktree(root / "wt-other", head, "other", locked="")
    lock_only = _worktree(root / "wt-det", head, None, locked="")
    duplicate = _worktree(root / "wt-dup", head, "other")
    remote = _worktree(root / "wt-remote", feat, None)
    guarded = [
        'add("wt-x", "-f", repo = "repo")',
        'add("wt-y", "--detach", repo = "repo")',
        'add("wt-z", "HEAD", Some("--force"), repo = "repo")',
        # Only `origin/feat` exists: without `--detach`, git would create a tracking `feat`.
        'add("wt-g", "feat", repo = "repo")',
    ]

    out = run_git_program(
        agl_check(
            "initial",
            'git::worktree::list("repo")',
            agl_array(_worktree(root / "repo", head, "main")),
        )
        + 'git::worktree::add("wt-det", repo = "repo")\n'
        + _worktree_at("detached", root / "wt-det", detached)
        + 'git::worktree::add("wt-new", "origin/main", Some("topic"), repo = "repo")\n'
        + _worktree_at("new-branch", root / "wt-new", topic)
        + 'git::worktree::add("wt-other", "other", repo = "repo")\n'
        + agl_raises('git::worktree::add("wt-dup", "other", repo = "repo")')
        + 'git::worktree::add("wt-dup", "other", None, true, "repo")\n'
        + _worktree_at("duplicate", root / "wt-dup", duplicate)
        + 'git::worktree::add("wt-remote", "origin/feat", repo = "repo")\n'
        + _worktree_at("remote", root / "wt-remote", remote)
        + agl_check("six", 'git::worktree::list("repo").size()', "6")
        + "".join(agl_raises(f"git::worktree::{call}") for call in guarded)
        + agl_check("still-six", 'git::worktree::list("repo").size()', "6")
        + 'git::worktree::move("wt-new", "wt-moved", "repo")\n'
        + _no_worktree_at("old-path", root / "wt-new")
        + _worktree_at("moved", root / "wt-moved", moved)
        + agl_raises('git::worktree::move("repo", "wt-main", "repo")')
        + 'git::worktree::lock("./wt-moved", Some("why not\\nsecond"), "repo")\n'
        + 'git::worktree::lock("wt-other", Some(""), "repo")\n'
        + 'git::worktree::lock("wt-det", repo = "repo")\n'
        + agl_raises('git::worktree::lock("wt-det", repo = "repo")')
        + _worktree_at("reason", root / "wt-moved", locked)
        + _worktree_at("empty-reason", root / "wt-other", other)
        + _worktree_at("no-reason", root / "wt-det", lock_only)
        + agl_raises('git::worktree::remove("wt-det", repo = "repo")')
        + agl_raises('git::worktree::remove("wt-det", true, "repo")')
        + agl_raises('git::worktree::move("wt-det", "wt-gone", "repo")')
        + 'git::worktree::unlock("./wt-det", "repo")\n'
        + _worktree_at("unlocked", root / "wt-det", detached)
        + agl_raises('git::worktree::unlock("wt-det", "repo")')
        + 'git::worktree::remove("./wt-det", repo = "repo")\n'
        + _no_worktree_at("removed", root / "wt-det")
        + 'fs::write("wt-remote/extra.txt", "untracked\\n")\n'
        + agl_raises('git::worktree::remove("wt-remote", repo = "repo")')
        + 'git::worktree::remove("wt-remote", true, "repo")\n'
        + _no_worktree_at("forced", root / "wt-remote")
        + agl_raises('git::worktree::remove("wt-nope", repo = "repo")')
        + agl_raises('git::worktree::list("absent")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=_FS_IMPORTS,
    )

    assert out.splitlines() == (
        ["ok"] * 3
        + ["raised"]
        + ["ok"] * 3
        + ["raised"] * 4
        + ["ok"] * 3
        + ["raised"] * 2
        + ["ok"] * 3
        + ["raised"] * 3
        + ["ok", "raised", "ok", "raised", "ok"]
        + ["raised"] * 2
    )
    assert "feat" not in git_refs(repo, env, "heads")
    for key in ("remote", "merge", "rebase"):
        git_fails(repo, ["config", "--get", f"branch.topic.{key}"], env)


def test_worktree_move_into_an_existing_directory(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    add_linked_worktree(repo, tmp_path / "wt-a", env, branch="a")
    add_linked_worktree(repo, tmp_path / "wt-b", env, branch="b")
    (tmp_path / "dir").mkdir()
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "wt-b").mkdir()
    write_file(tmp_path, "taken/wt-b/file", "x\n")
    root = tmp_path.resolve()
    head = 'git::resolve("HEAD", "repo")'

    out = run_git_program(
        'git::worktree::move("wt-a", "dir", "repo")\n'
        + _worktree_at("inside", root / "dir" / "wt-a", _worktree(root / "dir" / "wt-a", head, "a"))
        + agl_raises('git::worktree::move("wt-b", "taken", "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok", "raised"]
    assert (tmp_path / "wt-b").is_dir()


def test_worktree_list_bare_prunable_and_unborn(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bare, _ = clone_bare_remote(init_repo(tmp_path / "source", env), tmp_path, env)
    add_linked_worktree(bare, tmp_path / "wt-live", env, branch="live")
    add_linked_worktree(bare, tmp_path / "wt-stale", env, branch="stale")
    shutil.rmtree(tmp_path / "wt-stale")
    git_run(tmp_path, ["init", "-q", "-b", "fresh", "unborn"], env)
    root = tmp_path.resolve()
    name = bare.name
    live = _worktree(root / "wt-live", f'git::resolve("live", "{name}")', "live")
    entry = _worktree(root / name, None, None, bare=True)
    stale = _find_worktree(root / "wt-stale", name)

    out = run_git_program(
        agl_check(
            "bare",
            f'git::worktree::list("{name}").find(fn(w: Worktree) => w.bare)',
            f"Some({entry})",
        )
        + _worktree_at("live", root / "wt-live", live, name)
        + agl_check("listed", f'git::worktree::list("{name}").size()', "3")
        + agl_check(
            "prunable",
            stale + ".map(fn(w: Worktree) => w.prunable.is-some())",
            "Some(true)",
        )
        + f'git::worktree::prune("{name}")\n'
        + _no_worktree_at("pruned", root / "wt-stale", name)
        + agl_check("remaining", f'git::worktree::list("{name}").size()', "2")
        + agl_check(
            "unborn",
            'git::worktree::list("unborn")',
            agl_array(_worktree(root / "unborn", None, "fresh")),
        )
        + agl_raises('git::worktree::prune("absent")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"] * 7 + ["raised"]


def test_worktree_add_ignores_submodule_branch_propagation(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    sub = init_repo(tmp_path / "sub", env)
    git_run(
        repo, ["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "s"], env
    )
    git_run(repo, ["commit", "-qm", "add submodule"], env)
    for key in ("submodule.recurse", "submodule.propagateBranches"):
        git_run(repo, ["config", key, "true"], env)

    run_git_program(
        'git::worktree::add("wt", "HEAD", Some("topic"), repo = "repo")',
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert git_refs(repo, env, "heads").keys() == {"main", "topic"}
    assert git_refs(repo / "s", env, "heads").keys() == {"main"}


def _remote(name: str, fetch: str, push: str | None = None) -> str:
    """Return the AgL ``Remote`` literal; the push URL defaults to the fetch URL."""
    return f'Remote("{name}", "{fetch}", "{fetch if push is None else push}")'


def _remote_list(name: str, *remotes: str, repo: str = "repo") -> str:
    """Return AgL checking that ``remote::list`` of *repo* is exactly *remotes*."""
    return agl_check(name, f'git::remote::list("{repo}")', agl_array(*remotes))


def test_remote_add_set_url_rename_and_remove(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    head = git_rev(repo, env)
    git_run(repo, ["config", "url.ssh://x/.insteadOf", "/srv/a/"], env)
    git_run(repo, ["config", "url.ssh://p/.pushInsteadOf", "/srv/c/"], env)
    for name in ("up", "pushy"):
        git_run(repo, ["update-ref", f"refs/remotes/{name}/main", head], env)
    plain = tmp_path / "plain"
    plain.mkdir()

    out = run_git_program(
        agl_check("none", 'git::remote::list("repo").size()', "0")
        + 'git::remote::add("origin", "/srv/o", "repo")\n'
        'git::remote::add("up", "/srv/u", "repo")\n'
        'git::remote::add("mirror", "/srv/a/m", "repo")\n'
        'git::remote::add("pushy", "/srv/c/q", "repo")\n'
        + _remote_list(
            "added",
            _remote("mirror", "ssh://x/m"),
            _remote("origin", "/srv/o"),
            _remote("pushy", "/srv/c/q", "ssh://p/q"),
            _remote("up", "/srv/u"),
        )
        + 'git::remote::set-url("origin", "/srv/o2", repo = "repo")\n'
        'git::remote::set-url("mirror", "/srv/a/m2", false, "repo")\n'
        + _remote_list(
            "urls",
            _remote("mirror", "ssh://x/m2"),
            _remote("origin", "/srv/o2"),
            _remote("pushy", "/srv/c/q", "ssh://p/q"),
            _remote("up", "/srv/u"),
        )
        + 'git::remote::set-url("origin", "/srv/push", true, "repo")\n'
        + agl_check(
            "push-only",
            'git::remote::list("repo")[1]',
            _remote("origin", "/srv/o2", "/srv/push"),
        )
        + 'git::remote::rename("up", "down", "repo")\n'
        + agl_check(
            "renamed",
            'git::remote::list("repo").map(fn(r: Remote) => r.name)',
            '["down", "mirror", "origin", "pushy"]',
        )
        + 'git::remote::remove("pushy", "repo")\n'
        + agl_check(
            "removed",
            'git::remote::list("repo").map(fn(r: Remote) => r.name)',
            '["down", "mirror", "origin"]',
        )
        + agl_raises('git::remote::add("origin", "/srv/other", "repo")')
        + agl_raises('git::remote::remove("nope", "repo")')
        + agl_raises('git::remote::rename("nope", "other", "repo")')
        + agl_raises('git::remote::rename("origin", "mirror", "repo")')
        + agl_raises('git::remote::set-url("nope", "/srv/x", false, "repo")')
        + agl_raises('git::remote::set-url("nope", "/srv/x", true, "repo")')
        + agl_raises('git::remote::list("plain")')
        + agl_raises('git::remote::add("a", "/srv/a", "plain")')
        + agl_raises('git::remote::list("absent")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"] * 6 + ["raised"] * 9
    # Stored URLs stay as given: `insteadOf` rewriting applies only when reading them.
    assert git_lines(repo, ["config", "--get-all", "remote.mirror.url"], env) == ["/srv/a/m2"]
    assert git_lines(repo, ["config", "--get-all", "remote.origin.pushurl"], env) == ["/srv/push"]
    # Renaming moved the remote-tracking refs and the fetch refspec; removing deleted its refs.
    assert git_refs(repo, env, "remotes") == {"down/main": head}
    fetch = git_output(repo, ["config", "--get", "remote.down.fetch"], env)
    assert fetch == "+refs/heads/*:refs/remotes/down/*"
    for name in ("up", "pushy"):
        git_fails(repo, ["config", "--get-all", f"remote.{name}.url"], env)


def test_remote_operands_are_never_options(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    # Git accepts these names and URLs, so read as options they would act or fail: `--all` and
    # `--tags` are options of `get-url` and `add`, `--push` of `set-url`.
    git_run(repo, ["config", "remote.--all.url", "/srv/o"], env)

    out = run_git_program(
        _remote_list("listed", _remote("--all", "/srv/o"))
        + 'git::remote::add("--tags", "-u", "repo")\n'
        + _remote_list("added", _remote("--all", "/srv/o"), _remote("--tags", "-u"))
        + 'git::remote::set-url("--tags", "--push", true, "repo")\n'
        + _remote_list("push", _remote("--all", "/srv/o"), _remote("--tags", "-u", "--push"))
        + 'git::remote::set-url("--all", "--mirror", false, "repo")\n'
        + 'git::remote::rename("--tags", "--new", "repo")\n'
        + _remote_list("renamed", _remote("--all", "--mirror"), _remote("--new", "-u", "--push"))
        + 'git::remote::remove("--all", "repo")\n'
        + _remote_list("removed", _remote("--new", "-u", "--push")),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"] * 5
    # `--tags` was a name, not the option that would have set `tagopt`.
    git_fails(repo, ["config", "--get", "remote.--new.tagopt"], env)
    git_fails(repo, ["config", "--get", "remote.--all.url"], env)


def test_remote_remove_avoids_unsupported_end_of_options(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["remote", "add", "--end-of-options", "--all", "/srv/o"], env)
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_script(
        bin_dir,
        "git",
        f"""#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
remote = args.index("remote") if "remote" in args else -1
if remote >= 0 and remote + 1 < len(args) and args[remote + 1] == "remove":
    if "--end-of-options" in args:
        raise SystemExit(129)
os.execv({real_git!r}, [{real_git!r}, *args])
""",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    out = run_git_program(
        'git::remote::remove("--all", "repo")\n'
        + agl_check("removed", 'git::remote::list("repo").size()', "0"),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"]


def test_remote_list_counts_only_remotes_the_repository_defines(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    # `git remote` names `origin` for these settings alone, though the repository lacks it.
    global_git_config("remote.origin.prune", "true")
    git_run(repo, ["config", "remote.only-push.pushurl", "/srv/push"], env)

    out = run_git_program(
        _remote_list("global-only", _remote("only-push", "only-push", "/srv/push"))
        + 'git::remote::add("up", "/srv/u", "repo")\n'
        + _remote_list(
            "local",
            _remote("only-push", "only-push", "/srv/push"),
            _remote("up", "/srv/u"),
        )
        + agl_raises('git::remote::remove("origin", "repo")')
        + agl_raises('git::remote::rename("origin", "other", "repo")')
        + agl_raises('git::remote::set-url("origin", "/srv/x", false, "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"] * 2 + ["raised"] * 3


def test_config_get_set_and_unset_by_scope(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    (tmp_path / "plain").mkdir()
    git_run(repo, ["config", "--add", "multi.key", "first"], env)
    git_run(repo, ["config", "--add", "multi.key", "last"], env)

    out = run_git_program(
        agl_check("missing", 'git::config::get?("test.key", "repo")', "None::[text]")
        + agl_raises('git::config::get("test.key", "repo")')
        + agl_raises('git::config::get?("nodots", "repo")')
        + agl_raises('git::config::get?("test.bad key", "repo")')
        + agl_raises('git::config::get("nodots", "repo")')
        + 'git::config::set("test.key", "global", Global, "repo")\n'
        + agl_check("global", 'git::config::get("test.key", "repo")', '"global"')
        + 'git::config::set("test.key", "local", repo = "repo")\n'
        + agl_check("local-wins", 'git::config::get("test.key", "repo")', '"local"')
        + agl_check("optional", 'git::config::get?("TEST.Key", "repo")', 'Some("local")')
        + agl_check("outside", 'git::config::get?("test.key", "plain")', 'Some("global")')
        + agl_raises('git::config::set("multi.key", "new", repo = "repo")')
        + agl_raises('git::config::unset("multi.key", repo = "repo")')
        + 'git::config::unset("test.key", repo = "repo")\n'
        + agl_check("unset-local", 'git::config::get("test.key", "repo")', '"global"')
        + agl_raises('git::config::unset("test.key", repo = "repo")')
        + 'git::config::unset("test.key", Global, "repo")\n'
        + agl_check("unset-global", 'git::config::get?("test.key", "repo")', "None::[text]")
        + agl_raises('git::config::unset("test.key", Global, "repo")')
        + agl_raises('git::config::unset("nodots", repo = "repo")')
        + 'git::config::set("test.home", "h", Global, "plain")\n'
        + agl_check("global-outside", 'git::config::get("test.home", "plain")', '"h"')
        + agl_raises('git::config::set("test.home", "l", Local, "plain")')
        + agl_raises('git::config::set("nodots", "v", repo = "repo")')
        + agl_raises('git::config::set("test.key", "v", repo = "absent")')
        + agl_raises('git::config::get("test.key", "absent")'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == (
        ["ok"]
        + ["raised"] * 4
        + ["ok"] * 4
        + ["raised"] * 2
        + ["ok", "raised", "ok"]
        + ["raised"] * 2
        + ["ok"]
        + ["raised"] * 4
    )
    # Each scope wrote its own file, and the failed multi-value writes changed nothing.
    assert global_git_config("--get", "test.home") == "h"
    assert git_lines(repo, ["config", "--local", "--get-all", "multi.key"], env) == [
        "first",
        "last",
    ]
    git_fails(repo, ["config", "--local", "--get", "test.home"], env)
    git_fails(repo, ["config", "--local", "--get", "test.key"], env)
    git_fails(repo, ["config", "--global", "--get", "test.key"], dict(os.environ))


def test_config_get_optional_error_reports_the_original_read(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_repo(tmp_path / "repo", env)

    out = run_git_program(
        """
try
  let _ = git::config::get?("nodots", "repo")
  print("returned")
catch GitError as e =>
  print(e.args == ["-C", "repo", "config", "-z", "--get", "--end-of-options", "nodots"])
""",
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["true"]


def test_config_value_forms(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    # Configuration the environment supplies is part of the effective value `get` reads.
    for name, value in (
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "env.key"),
        ("GIT_CONFIG_VALUE_0", "from env"),
    ):
        monkeypatch.setenv(name, value)
    included = tmp_path / "included.cfg"
    git_run(repo, ["config", "--file", str(included), "inc.key", "from include"], env)
    git_run(repo, ["config", "include.path", str(included)], env)
    # A key written without a value is read as empty text.
    with (repo / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write("[flag]\n\tbare\n")
    git_run(repo, ["config", "--add", "multi.key", "first"], env)
    git_run(repo, ["config", "--add", "multi.key", "last"], env)
    # A setting the library pins for its own git calls must not leak into the value read back.
    git_run(repo, ["config", "submodule.propagateBranches", "true"], env)
    text = '  quoted \\"q\\" \\\\ back ; # not a comment\\n\\nthird line'

    out = run_git_program(
        'git::config::set("test.empty", "", repo = "repo")\n'
        + agl_check("empty", 'git::config::get?("test.empty", "repo")', 'Some("")')
        + agl_check("flag", 'git::config::get("flag.bare", "repo")', '""')
        + f'git::config::set("test.text", "{text}", repo = "repo")\n'
        + agl_check("text", 'git::config::get("test.text", "repo")', f'"{text}"')
        + 'git::config::set("branch.feat/x.remote", "up", repo = "repo")\n'
        + agl_check("sub", 'git::config::get("branch.feat/x.remote", "repo")', '"up"')
        + agl_check("included", 'git::config::get("inc.key", "repo")', '"from include"')
        + agl_check("environment", 'git::config::get("env.key", "repo")', '"from env"')
        + agl_check("last", 'git::config::get("multi.key", "repo")', '"last"')
        + agl_check("pins", 'git::config::get("submodule.propagateBranches", "repo")', '"true"'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["ok"] * 8
    assert git_output(repo, ["config", "--local", "--get", "test.empty"], env) == ""


def test_config_operands_are_never_options(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["config", "test.keep", "kept"], env)
    before = git_output(repo, ["config", "--list", "--local"], env).splitlines()

    out = run_git_program(
        # Read as an option, the key `--unset` would delete `test.keep`.
        agl_raises('git::config::set("--unset", "test.keep", repo = "repo")')
        # `--x.y` is a valid key: as an option, each of these would fail instead.
        + 'git::config::set("--x.y", "v", repo = "repo")\n'
        + agl_check("get", 'git::config::get("--x.y", "repo")', '"v"')
        + agl_check("missing", 'git::config::get?("--x.z", "repo")', "None::[text]")
        + 'git::config::unset("--x.y", repo = "repo")\n'
        + 'git::config::set("test.opt", "--unset", repo = "repo")\n'
        + agl_check("value", 'git::config::get("test.opt", "repo")', '"--unset"')
        + 'git::config::set("test.dash", "-1", repo = "repo")\n'
        + agl_check("dash", 'git::config::get("test.dash", "repo")', '"-1"'),
        tmp_path,
        monkeypatch,
        capsys,
    )

    assert out.splitlines() == ["raised", "ok", "ok", "ok", "ok"]
    after = git_output(repo, ["config", "--list", "--local"], env).splitlines()
    assert after == [*before, "test.opt=--unset", "test.dash=-1"]
