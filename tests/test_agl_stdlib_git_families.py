"""Family scopes of ``std/git``: ``branch`` and ``tag``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._agl_git_helpers import agl_check, agl_raises, run_git_program
from tests._git_helpers import (
    clone_bare_remote,
    clone_local_remote,
    commit_file,
    git_fails,
    git_output,
    git_refs,
    git_rev,
    git_run,
    init_repo,
)

_IMPORTS = ("std/git::*",)


def _branch(name: str, commit: str, upstream: str | None = None, current: bool = False) -> str:
    """Return the AgL ``Branch`` literal."""
    tracked = "None" if upstream is None else f'Some("{upstream}")'
    return f'Branch("{name}", "{commit}", {tracked}, {str(current).lower()})'


def _agl_array(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


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
        return _agl_array(
            [
                _branch("feat", feat, "origin/feat", current == "feat"),
                _branch("gone", gone, "origin/gone", current == "gone"),
                _branch("loc", main, "main", current == "loc"),
                _branch("main", main, "origin/main", current == "main"),
                _branch("solo", main, None, current == "solo"),
            ]
        )

    remote = _agl_array([_branch("origin/feat", feat), _branch("origin/main", main)])
    git_run(tmp_path, ["init", "-q", "-b", "trunk", "fresh"], env)

    out, result = run_git_program(
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
        imports=_IMPORTS,
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 12 + ["raised", "raised"]


def test_branch_create_delete_rename_and_upstream(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, repo = clone_bare_remote(init_repo(tmp_path / "source", env), tmp_path, env)
    published = git_rev(repo, env, "HEAD")
    commit_file(repo, env, name="more.txt", message="more")
    tip = git_rev(repo, env, "HEAD")
    git_run(repo, ["branch", "keep"], env)
    git_run(repo, ["checkout", "-qb", "unmerged"], env)
    commit_file(repo, env, name="unmerged.txt", message="unmerged")
    git_run(repo, ["checkout", "-q", "main"], env)
    # Configuration that would make `branch` track its start point.
    git_run(repo, ["config", "branch.autoSetupMerge", "always"], env)
    git_run(repo, ["config", "branch.autoSetupRebase", "always"], env)

    out, result = run_git_program(
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
        imports=_IMPORTS,
    )

    assert result.ok, result.error
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

    _, result = run_git_program(
        'git::branch::create("topic", repo = "repo")',
        tmp_path,
        monkeypatch,
        capsys,
        imports=_IMPORTS,
    )

    assert result.ok, result.error
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
    expected = _agl_array(
        [
            _branch("main", main),
            _branch("refs/remotes/x", main, None, True),
            _branch("y", main, "refs/remotes/x"),
        ]
    )

    out, result = run_git_program(
        agl_check("all", 'git::branch::list(repo = ".")', expected)
        + agl_check("current", 'git::branch::current(".")', 'Some("refs/remotes/x")')
        + agl_check("upstream", 'git::branch::upstream("y", ".")', 'Some("refs/remotes/x")'),
        repo,
        monkeypatch,
        capsys,
        imports=_IMPORTS,
    )

    assert result.ok, result.error
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

    out, result = run_git_program(
        "".join(agl_raises(f"git::branch::{call}") for call in calls),
        tmp_path,
        monkeypatch,
        capsys,
        imports=_IMPORTS,
    )

    assert result.ok, result.error
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

    out, result = run_git_program(
        agl_check("ahead", 'git::branch::compare("topic", "main", "repo")', "Divergence(2, 1)")
        + agl_check("behind", 'git::branch::compare("main", "topic", "repo")', "Divergence(1, 2)")
        + agl_check("same", 'git::branch::compare("main", "main", "repo")', "Divergence(0, 0)")
        + agl_check("head", 'git::branch::compare("HEAD", "topic", "repo")', "Divergence(0, 2)")
        + agl_raises('git::branch::compare("topic..main", "main", "repo")')
        + agl_raises('git::branch::compare("topic", "nope", "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=_IMPORTS,
    )

    assert result.ok, result.error
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
    args = ["hash-object", "-t", "tag", "-w", "--stdin", "--literally"]
    tag = git_output(repo, args, env, stdin=content)
    git_run(repo, ["update-ref", f"refs/tags/{name}", tag], env)


def test_tag_create_list_and_delete(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    first = git_rev(repo, env, "HEAD")
    commit_file(repo, env, name="more.txt", message="a\n\nbody of the commit")
    second = git_rev(repo, env, "HEAD")
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
        return agl_check(name, 'git::tag::list("repo")', _agl_array(list(tags)))

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

    out, result = run_git_program(
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
        imports=_IMPORTS,
    )

    assert result.ok, result.error
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
    second = git_rev(repo, env, "HEAD")
    git_run(repo, ["tag", "light", "HEAD~1"], env)
    git_run(repo, ["tag", "-a", "ann", "-m", "old", "HEAD~1"], env)
    calls = [
        'create("--force", repo = "repo")',
        'create("--force", message = Some("m"), repo = "repo")',
        'create("new", "--force", repo = "repo")',
        'delete("--force", "repo")',
        'delete("-i", "repo")',
    ]

    out, result = run_git_program(
        'git::tag::create("light", "HEAD", None, true, "repo")\n'
        'git::tag::create("ann", "HEAD", Some("new"), true, "repo")\n'
        + "".join(agl_raises(f"git::tag::{call}") for call in calls)
        + agl_check(
            "tags",
            'git::tag::list("repo")',
            _agl_array([_tag("ann", second, "new"), _tag("light", second)]),
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=_IMPORTS,
    )

    assert result.ok, result.error
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
