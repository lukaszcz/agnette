"""Integration of ``std/git``: merge, rebase, cherry-pick, revert, operation state, and remotes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._agl_git_helpers import agl_check, agl_raises, run_git_program
from tests._git_helpers import (
    add_linked_worktree,
    clone_bare_remote,
    commit_file,
    conflicted_merge,
    conflicting_branches,
    git_fails,
    git_head,
    git_lines,
    git_output,
    git_refs,
    git_rev,
    git_run,
    init_repo,
)

Setup = Callable[[Path, dict[str, str]], None]


def _parent_count(repo: Path, env: dict[str, str]) -> int:
    return len(git_output(repo, ["rev-list", "--parents", "-n1", "HEAD"], env).split()) - 1


def _merge_branches(repo: Path, env: dict[str, str]) -> str:
    """Create ``div`` (diverged from ``main``), ``ff`` and ``ff2`` (ahead of it), on ``main``.

    Return the tip of ``ff``.
    """
    git_run(repo, ["checkout", "-qb", "div"], env)
    commit_file(repo, env, name="div.txt", message="div")
    git_run(repo, ["checkout", "-q", "main"], env)
    commit_file(repo, env, name="m.txt", message="m")
    git_run(repo, ["checkout", "-qb", "ff"], env)
    commit_file(repo, env, name="ff.txt", message="ff")
    tip = git_head(repo, env)
    git_run(repo, ["checkout", "-qb", "ff2"], env)
    commit_file(repo, env, name="ff2.txt", message="ff2")
    git_run(repo, ["checkout", "-q", "main"], env)
    return tip


def _completed(label: str, call: str) -> str:
    """Return AgL printing the new ``HEAD`` when *call* is ``Completed``, else ``not completed``."""
    return f"""
let {label} = {call}
case {label} of
  | Completed(head) => print(head)
  | _ => print("not completed")
"""


def _resolve(repo: str, content: str = "resolved") -> str:
    """Return AgL resolving the conflict in ``f.txt`` of *repo* to *content* and staging it."""
    return f"""
fs::write("{repo}/f.txt", "{content}\\n")
git::add(["f.txt"], "{repo}")
"""


@dataclass(frozen=True)
class Scenario:
    """A repository where ``start(ref)`` stops on a conflict in ``f.txt``."""

    state: str
    setup: Setup
    start: Callable[[str], str]
    ref: str
    parents: int


def _setup_merge(path: Path, env: dict[str, str]) -> None:
    conflicting_branches(init_repo(path, env), env)


def _setup_rebase(path: Path, env: dict[str, str]) -> None:
    repo = init_repo(path, env)
    conflicting_branches(repo, env)
    git_run(repo, ["checkout", "-q", "other"], env)


def _setup_revert(path: Path, env: dict[str, str]) -> None:
    repo = init_repo(path, env)
    for content in ("base", "one", "two"):
        commit_file(repo, env, name="f.txt", content=f"{content}\n", message=content)


def _setup_sequence(path: Path, env: dict[str, str]) -> Path:
    """Leave ``main`` conflicting with ``src~1``, the middle one of ``src``'s three commits."""
    repo = init_repo(path, env)
    commit_file(repo, env, name="f.txt", content="base\n", message="base")
    git_run(repo, ["checkout", "-qb", "src"], env)
    commit_file(repo, env, name="a.txt", message="c1")
    commit_file(repo, env, name="f.txt", content="theirs\n", message="c2")
    commit_file(repo, env, name="b.txt", message="c3")
    git_run(repo, ["checkout", "-q", "main"], env)
    commit_file(repo, env, name="f.txt", content="ours\n", message="ours")
    return repo


SEQUENCE = 'git::cherry-pick(["src~2", "src~1", "src"], "{}")'


SCENARIOS = {
    "merge": Scenario(
        "Merging", _setup_merge, lambda r: f'git::merge("{r}", repo = "repo")', "other", 2
    ),
    "rebase": Scenario(
        "Rebasing", _setup_rebase, lambda r: f'git::rebase("{r}", repo = "repo")', "main", 1
    ),
    "cherry-pick": Scenario(
        "CherryPicking",
        _setup_merge,
        lambda r: f'git::cherry-pick(["{r}"], repo = "repo")',
        "other",
        1,
    ),
    "revert": Scenario(
        "Reverting", _setup_revert, lambda r: f'git::revert(["{r}"], repo = "repo")', "HEAD~1", 1
    ),
}
CONFLICTED = 'Conflicted(["f.txt"])'


def test_merge_reports_up_to_date_fast_forward_and_completed(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    tip = _merge_branches(repo, env)
    git_run(repo, ["config", "commit.cleanup", "strip"], env)
    git_run(repo, ["config", "merge.log", "true"], env)

    out, result = run_git_program(
        agl_check("up", 'git::merge("HEAD~1", repo = "repo")', "UpToDate")
        + agl_check("ff", 'git::merge("ff", repo = "repo")', f'FastForwarded("{tip}")')
        + _completed(
            "merged", 'git::merge("div", message = Some("# custom subject"), repo = "repo")'
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok", "ok", git_head(repo, env)]
    assert _parent_count(repo, env) == 2
    assert git_output(repo, ["log", "-1", "--format=%B"], env) == "# custom subject"


def test_merge_fast_forward_modes(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    tip = _merge_branches(repo, env)

    out, result = run_git_program(
        agl_raises('git::merge("div", Only, repo = "repo")')
        + agl_check("only", 'git::merge("ff", Only, repo = "repo")', f'FastForwarded("{tip}")')
        + _completed("never", 'git::merge("ff2", Never, repo = "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised", "ok", git_head(repo, env)]
    assert git_output(repo, ["rev-parse", "HEAD^1"], env) == tip
    assert _parent_count(repo, env) == 2


def test_merge_pins_fast_forward_mode_against_configuration(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tips = {}
    for name, setting in (("no-ff", "false"), ("ff-only", "only")):
        repo = init_repo(tmp_path / name, env)
        tips[name] = _merge_branches(repo, env)
        git_run(repo, ["config", "merge.ff", setting], env)
        git_run(repo, ["config", "merge.log", "true"], env)

    out, result = run_git_program(
        agl_check("ff", 'git::merge("ff", repo = "no-ff")', f'FastForwarded("{tips["no-ff"]}")')
        + _completed("merged", 'git::merge("div", repo = "ff-only")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok", git_head(tmp_path / "ff-only", env)]
    assert _parent_count(tmp_path / "ff-only", env) == 2
    assert git_output(tmp_path / "ff-only", ["log", "-1", "--format=%B"], env) == (
        "Merge branch 'div'"
    )


def test_conflict_paths_are_root_relative_from_a_subdirectory(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    (repo / "sub").mkdir()
    names = ("a.txt", "gone.txt", "sub/b.txt")
    for name in names:
        (repo / name).write_text("base\n", encoding="utf-8")
    git_run(repo, ["add", "-A"], env)
    git_run(repo, ["commit", "-qm", "base"], env)
    git_run(repo, ["checkout", "-qb", "other"], env)
    for name in names:
        (repo / name).write_text("theirs\n", encoding="utf-8")
    git_run(repo, ["commit", "-qam", "theirs"], env)
    git_run(repo, ["checkout", "-q", "main"], env)
    for name in ("a.txt", "sub/b.txt"):
        (repo / name).write_text("ours\n", encoding="utf-8")
    git_run(repo, ["rm", "-q", "gone.txt"], env)
    git_run(repo, ["commit", "-qam", "ours"], env)
    git_run(repo, ["config", "diff.relative", "true"], env)

    out, result = run_git_program(
        agl_check(
            "paths",
            'git::merge("other", repo = "repo/sub")',
            'Conflicted(["a.txt", "gone.txt", "sub/b.txt"])',
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"]


def test_cherry_pick_and_revert_complete_cleanly(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["checkout", "-qb", "feat"], env)
    (repo / "feat.txt").write_text("content\n", encoding="utf-8")
    git_run(repo, ["add", "feat.txt"], env)
    git_run(repo, ["commit", "-q", "--cleanup=verbatim", "-m", "# feat\n\nbody  "], env)
    message = git_output(repo, ["log", "-1", "--format=%B"], env)
    git_run(repo, ["checkout", "-q", "main"], env)
    git_run(repo, ["config", "commit.cleanup", "strip"], env)
    git_run(repo, ["config", "revert.reference", "true"], env)
    main = git_head(repo, env)
    (tmp_path / "plain").mkdir()

    out, result = run_git_program(
        agl_check("none", 'git::cherry-pick([], "plain")', "UpToDate")
        + _completed("picked", 'git::cherry-pick(["feat"], "repo")')
        + _completed("reverted", 'git::revert(["HEAD"], "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    picked = git_output(repo, ["rev-parse", "HEAD~1"], env)
    assert out.splitlines() == ["ok", picked, git_head(repo, env)]
    assert git_output(repo, ["rev-parse", "HEAD~2"], env) == main
    assert git_output(repo, ["log", "-1", "--format=%B", "HEAD~1"], env) == message
    assert not (repo / "feat.txt").exists()
    assert git_output(repo, ["log", "-1", "--format=%B"], env).startswith('Revert "')


def test_cherry_pick_sequence_resumes_with_the_remaining_commits(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _setup_sequence(tmp_path / "repo", env)

    out, result = run_git_program(
        agl_check("started", SEQUENCE.format("repo"), CONFLICTED)
        + agl_check("state", 'git::operation("repo")', "Some::[Operation](CherryPicking)")
        + _resolve("repo")
        + _completed("resumed", 'git::resume(CherryPicking, "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*", "std/fs"),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok", "ok", git_head(repo, env)]
    assert git_lines(repo, ["log", "--format=%s", "-4"], env) == ["c3", "c2", "c1", "ours"]
    assert (repo / "a.txt").exists()
    assert (repo / "b.txt").exists()


def test_cherry_pick_of_an_applied_commit_is_an_error_that_leaves_the_pick_open(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["checkout", "-qb", "feat"], env)
    commit_file(repo, env, name="feat.txt", message="feat")
    git_run(repo, ["checkout", "-q", "main"], env)
    git_run(repo, ["cherry-pick", "feat"], env)
    before = git_head(repo, env)

    out, result = run_git_program(
        agl_raises('git::cherry-pick(["feat"], "repo")')
        + agl_check("state", 'git::operation("repo")', "Some::[Operation](CherryPicking)")
        + 'git::abort(CherryPicking, "repo")\n'
        + agl_check("idle", 'git::operation("repo")', "None::[Operation]"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised", "ok", "ok"]
    assert git_head(repo, env) == before


def test_rebase_reports_every_outcome(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["branch", "old"], env)
    commit_file(repo, env, name="m.txt", message="m")
    git_run(repo, ["checkout", "-qb", "dup", "old"], env)
    commit_file(repo, env, name="dd.txt", message="dd")
    git_run(repo, ["checkout", "-q", "main"], env)
    git_run(repo, ["cherry-pick", "dup"], env)
    git_run(repo, ["checkout", "-qb", "feat"], env)
    commit_file(repo, env, name="f1.txt", message="f1")
    tip = git_head(repo, env)
    git_run(repo, ["checkout", "-qb", "topic", "old"], env)
    commit_file(repo, env, name="t1.txt", message="# t1  ")
    git_run(repo, ["checkout", "-q", "feat"], env)
    git_run(repo, ["config", "commit.cleanup", "strip"], env)

    out, result = run_git_program(
        agl_check("up", 'git::rebase("main", "repo")', "UpToDate")
        + """
git::checkout("main", "repo")
"""
        + agl_check("ff", 'git::rebase("feat", "repo")', f'FastForwarded("{tip}")')
        + 'git::checkout("topic", "repo")'
        + _completed("replayed", 'git::rebase("main", "repo")')
        + 'git::checkout("dup", "repo")'
        + agl_check("dropped", 'git::rebase("main", "repo")', f'FastForwarded("{tip}")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    topic = git_output(repo, ["rev-parse", "topic"], env)
    assert out.splitlines() == ["ok", "ok", topic, "ok"]
    assert git_output(repo, ["rev-parse", "topic^"], env) == tip
    # Replayed messages keep their `#` lines, whatever `commit.cleanup` says.
    assert git_output(repo, ["log", "-1", "--format=%B", "topic"], env) == "# t1"


@pytest.mark.parametrize("name", SCENARIOS)
def test_conflict_stops_the_operation_until_resolved_and_resumed(
    name: str,
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario = SCENARIOS[name]
    scenario.setup(tmp_path / "repo", env)
    repo = tmp_path / "repo"
    state = scenario.state
    # Merge and rebase resumption open the editor unless git is told not to.
    monkeypatch.setenv("GIT_EDITOR", "false")
    git_run(repo, ["config", "core.editor", "false"], env)

    out, result = run_git_program(
        agl_check("idle", 'git::operation("repo")', "None::[Operation]")
        + agl_check("started", scenario.start(scenario.ref), CONFLICTED)
        + agl_check("state", 'git::operation("repo")', f"Some::[Operation]({state})")
        + agl_check("again", f'git::resume({state}, "repo")', CONFLICTED)
        + _resolve("repo")
        + _completed("resumed", f'git::resume({state}, "repo")')
        + agl_check("finished", 'git::operation("repo")', "None::[Operation]"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*", "std/fs"),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 4 + [git_head(repo, env), "ok"]
    assert git_output(repo, ["show", "HEAD:f.txt"], env) == "resolved"
    assert git_output(repo, ["status", "--porcelain"], env) == ""
    assert _parent_count(repo, env) == scenario.parents


@pytest.mark.parametrize("name", SCENARIOS)
def test_abort_restores_the_state_before_the_operation(
    name: str,
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario = SCENARIOS[name]
    repo = tmp_path / "repo"
    scenario.setup(repo, env)
    before = git_head(repo, env)
    branch = git_output(repo, ["symbolic-ref", "HEAD"], env)
    content = (repo / "f.txt").read_text(encoding="utf-8")

    out, result = run_git_program(
        agl_check("started", scenario.start(scenario.ref), CONFLICTED)
        + f'git::abort({scenario.state}, "repo")'
        + agl_check("idle", 'git::operation("repo")', "None::[Operation]")
        + agl_check("clean", 'git::is-clean(repo = "repo")', "true"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 3
    assert git_head(repo, env) == before
    assert git_output(repo, ["symbolic-ref", "HEAD"], env) == branch
    assert (repo / "f.txt").read_text(encoding="utf-8") == content


@pytest.mark.parametrize("name", SCENARIOS)
def test_operation_in_progress_refuses_other_calls_and_option_likegit_refs(
    name: str,
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario = SCENARIOS[name]
    scenario.setup(tmp_path / "repo", env)
    state = scenario.state
    # Neither is a sequencer-sharing operation, so git rejects both outright.
    other = "Rebasing" if state != "Rebasing" else "Merging"
    refused = [scenario.start(scenario.ref)] + [
        scenario.start(ref) for ref in ("--abort", "--continue", "--quit")
    ]
    refused += [f'git::resume({other}, "repo")', f'git::abort({other}, "repo")']
    # Starting a different operation is refused as well, whatever it would do.
    refused += [
        other_scenario.start("HEAD~1")
        for other_name, other_scenario in SCENARIOS.items()
        if other_name != name
    ]

    out, result = run_git_program(
        agl_check("started", scenario.start(scenario.ref), CONFLICTED)
        + "".join(agl_raises(call) for call in refused)
        + agl_check("state", 'git::operation("repo")', f"Some::[Operation]({state})"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] + ["raised"] * len(refused) + ["ok"]
    assert git_output(tmp_path / "repo", ["diff", "--name-only", "--diff-filter=U"], env) == "f.txt"


def test_resume_abort_and_start_without_an_operation_raise(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_repo(tmp_path / "idle", env)
    stale = init_repo(tmp_path / "stale", env)
    commit_file(stale, env, name="f.txt", content="base\n", message="base")
    (stale / "f.txt").write_text("stashed\n", encoding="utf-8")
    git_run(stale, ["stash", "push", "-q"], env)
    commit_file(stale, env, name="f.txt", content="other\n", message="other")
    git_fails(stale, ["stash", "apply"], env)
    assert git_output(stale, ["diff", "--name-only", "--diff-filter=U"], env) == "f.txt"

    body = "let ops: array[Operation] = [Merging, Rebasing, CherryPicking, Reverting]"
    for repo in ("idle", "stale"):
        for call in ("resume", "abort"):
            body += f"""
for op in ops do
  try
    let _ = git::{call}(op, "{repo}")
    print("returned")
  catch GitError =>
    print("raised")
"""
    # The unmerged paths of a failed `stash apply` are not an operation, so starting one is an
    # error, not a conflict.
    starts = [
        'git::merge("HEAD~1", repo = "stale")',
        'git::rebase("HEAD~1", repo = "stale")',
        'git::cherry-pick(["HEAD~1"], repo = "stale")',
        'git::revert(["HEAD"], repo = "stale")',
    ]
    body += "".join(agl_raises(call) for call in starts)
    body += agl_check("idle", 'git::operation("stale")', "None::[Operation]")
    out, result = run_git_program(body, tmp_path, monkeypatch, capsys, imports=("std/git::*",))

    assert result.ok, result.error
    assert out.splitlines() == ["raised"] * 16 + ["raised"] * len(starts) + ["ok"]


def test_dirty_tree_refusals_are_errors_even_with_autostash_configured(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="f.txt", content="base\n", message="f")
    commit_file(repo, env, name="g.txt", content="base\n", message="g")
    git_run(repo, ["checkout", "-qb", "other"], env)
    commit_file(repo, env, name="f.txt", content="other\n", message="other")
    git_run(repo, ["checkout", "-q", "main"], env)
    commit_file(repo, env, name="g.txt", content="main\n", message="main")
    (repo / "f.txt").write_text("dirty f\n", encoding="utf-8")
    (repo / "g.txt").write_text("dirty g\n", encoding="utf-8")
    for key in ("merge.autoStash", "rebase.autoStash"):
        git_run(repo, ["config", key, "true"], env)
    before = git_head(repo, env)

    out, result = run_git_program(
        "".join(
            agl_raises(call)
            for call in (
                'git::merge("other", repo = "repo")',
                'git::rebase("other", repo = "repo")',
                'git::cherry-pick(["other"], repo = "repo")',
                'git::revert(["HEAD"], repo = "repo")',
            )
        )
        + agl_check("idle", 'git::operation("repo")', "None::[Operation]"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised"] * 4 + ["ok"]
    assert git_head(repo, env) == before
    assert (repo / "f.txt").read_text(encoding="utf-8") == "dirty f\n"
    assert (repo / "g.txt").read_text(encoding="utf-8") == "dirty g\n"
    assert git_lines(repo, ["stash", "list"], env) == []


def test_operands_must_name_a_single_commit(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["checkout", "-qb", "other"], env)
    commit_file(repo, env, name="o.txt", message="other")
    git_run(repo, ["checkout", "-q", "main"], env)
    before = git_head(repo, env)

    refs = ["HEAD^{tree}", "main..other", "--abort"]
    calls = [
        template.format(ref=ref)
        for template in (
            'git::merge("{ref}", repo = "repo")',
            'git::rebase("{ref}", repo = "repo")',
            'git::cherry-pick(["{ref}"], repo = "repo")',
            'git::revert(["{ref}"], repo = "repo")',
        )
        for ref in refs
    ]
    out, result = run_git_program(
        "".join(agl_raises(call) for call in calls)
        + agl_check("idle", 'git::operation("repo")', "None::[Operation]"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised"] * len(calls) + ["ok"]
    assert git_head(repo, env) == before
    assert git_output(repo, ["status", "--porcelain"], env) == ""


def test_operation_follows_the_priority_order(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_repo(tmp_path / "repo", env)

    # Sequencer todo files, as git writes them, are covered by the merge-commit workflow below.
    out, result = run_git_program(
        'fs::write("repo/.git/REVERT_HEAD", "")'
        + agl_check("r", 'git::operation("repo")', "Some::[Operation](Reverting)")
        + 'fs::write("repo/.git/CHERRY_PICK_HEAD", "")'
        + agl_check("c", 'git::operation("repo")', "Some::[Operation](CherryPicking)")
        + 'fs::write("repo/.git/MERGE_HEAD", "")'
        + agl_check("m", 'git::operation("repo")', "Some::[Operation](Merging)")
        + 'fs::mkdir("repo/.git/rebase-apply")'
        + agl_check("a", 'git::operation("repo")', "Some::[Operation](Rebasing)")
        + 'fs::remove("repo/.git/rebase-apply")'
        + agl_check("n", 'git::operation("repo")', "Some::[Operation](Merging)")
        + 'fs::mkdir("repo/.git/rebase-merge")'
        + agl_check("b", 'git::operation("repo")', "Some::[Operation](Rebasing)"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*", "std/fs"),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 6


def test_operation_state_is_per_linked_worktree(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    conflicting_branches(repo, env)
    add_linked_worktree(repo, tmp_path / "wt", env, branch="wt")

    out, result = run_git_program(
        agl_check("started", 'git::merge("other", repo = "wt")', CONFLICTED)
        + agl_check("linked", 'git::operation("wt")', "Some::[Operation](Merging)")
        + agl_check("primary", 'git::operation("repo")', "None::[Operation]")
        + 'git::abort(Merging, "wt")'
        + agl_check("idle", 'git::operation("wt")', "None::[Operation]"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 4


def test_conflict_resume_messages_ignore_cleanup_configuration(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    merge = init_repo(tmp_path / "merge", env)
    conflicting_branches(merge, env)
    rebase = init_repo(tmp_path / "rebase", env)
    conflicting_branches(rebase, env)
    git_run(rebase, ["checkout", "-q", "other"], env)
    git_run(rebase, ["commit", "-q", "--amend", "--cleanup=verbatim", "-m", "# topic"], env)
    for repo in (merge, rebase):
        git_run(repo, ["config", "commit.cleanup", "verbatim"], env)

    out, result = run_git_program(
        agl_check(
            "merge", 'git::merge("other", message = Some("# subject"), repo = "merge")', CONFLICTED
        )
        + _resolve("merge")
        + _completed("merged", 'git::resume(Merging, "merge")')
        + agl_check("rebase", 'git::rebase("main", "rebase")', CONFLICTED)
        + _resolve("rebase")
        + _completed("rebased", 'git::resume(Rebasing, "rebase")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*", "std/fs"),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok", git_head(merge, env), "ok", git_head(rebase, env)]
    # Git's conflict comments never reach the message, whatever `commit.cleanup` says.
    assert git_output(merge, ["log", "-1", "--format=%B"], env) == "# subject"
    assert git_output(rebase, ["log", "-1", "--format=%B"], env) == "# topic"


def test_resume_truncates_at_the_scissors_line_and_keeps_foreign_conflict_comments(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scissors = "# ------------------------ >8 ------------------------"
    merge = init_repo(tmp_path / "merge", env)
    conflicting_branches(merge, env)
    rebase = init_repo(tmp_path / "rebase", env)
    conflicting_branches(rebase, env)
    git_run(rebase, ["checkout", "-q", "other"], env)
    message = f"topic\n{scissors}\ntail"
    git_run(rebase, ["commit", "-q", "--amend", "--cleanup=verbatim", "-m", message], env)
    foreign = conflicted_merge(tmp_path / "foreign", env)
    merge_message = f"subject\\n{scissors}\\ntail"

    out, result = run_git_program(
        agl_check(
            "merge",
            f'git::merge("other", message = Some("{merge_message}"), repo = "merge")',
            CONFLICTED,
        )
        + _resolve("merge")
        + _completed("merged", 'git::resume(Merging, "merge")')
        + agl_check("rebase", 'git::rebase("main", "rebase")', CONFLICTED)
        + _resolve("rebase")
        + _completed("rebased", 'git::resume(Rebasing, "rebase")')
        + _resolve("foreign")
        + _completed("continued", 'git::resume(Merging, "foreign")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*", "std/fs"),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok", git_head(merge, env), "ok", git_head(rebase, env)] + [
        git_head(foreign, env)
    ]
    assert git_output(merge, ["log", "-1", "--format=%B"], env) == "subject"
    assert git_output(rebase, ["log", "-1", "--format=%B"], env) == "topic"
    # A merge that `merge` did not start has no scissors line, so git's own comments stay.
    assert "# Conflicts:" in git_output(foreign, ["log", "-1", "--format=%B"], env)


def test_recorded_resolutions_still_report_conflicts(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    conflicting_branches(repo, env)
    before = git_head(repo, env)
    git_run(repo, ["config", "rerere.enabled", "true"], env)
    git_run(repo, ["config", "rerere.autoUpdate", "true"], env)
    # Record a resolution, which `rerere.autoUpdate` would then stage on a replayed conflict.
    git_fails(repo, ["merge", "other"], env)
    (repo / "f.txt").write_text("resolved\n", encoding="utf-8")
    git_run(repo, ["add", "f.txt"], env)
    git_run(repo, ["commit", "-q", "--no-edit"], env)
    git_run(repo, ["reset", "-q", "--hard", before], env)

    out, result = run_git_program(
        agl_check("merge", 'git::merge("other", repo = "repo")', CONFLICTED)
        + 'git::abort(Merging, "repo")'
        + agl_check("pick", 'git::cherry-pick(["other"], repo = "repo")', CONFLICTED)
        + 'git::abort(CherryPicking, "repo")\n'
        + 'git::checkout("other", "repo")\n'
        + agl_check("rebase", 'git::rebase("main", repo = "repo")', CONFLICTED)
        + 'git::abort(Rebasing, "repo")',
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok"] * 3


def test_cherry_picking_and_reverting_share_the_sequencer(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    done = _setup_sequence(tmp_path / "done", env)
    stopped = _setup_sequence(tmp_path / "stopped", env)
    before = git_head(stopped, env)
    SCENARIOS["cherry-pick"].setup(tmp_path / "pick", env)
    SCENARIOS["revert"].setup(tmp_path / "revert", env)

    out, result = run_git_program(
        # Several pending commits can be resumed only as the operation `operation` reports.
        agl_check("seq", SEQUENCE.format("done"), CONFLICTED)
        + _resolve("done")
        + agl_raises('git::resume(Reverting, "done")')
        + agl_check("state", 'git::operation("done")', "Some::[Operation](CherryPicking)")
        + _completed("sequence", 'git::resume(CherryPicking, "done")')
        # Either operation aborts the sequence.
        + agl_check("stopped", SEQUENCE.format("stopped"), CONFLICTED)
        + 'git::abort(Reverting, "stopped")'
        + agl_check("idle", 'git::operation("stopped")', "None::[Operation]")
        # A single pending commit can be completed by either.
        + agl_check("pick", 'git::cherry-pick(["other"], repo = "pick")', CONFLICTED)
        + _resolve("pick")
        + _completed("reverting", 'git::resume(Reverting, "pick")')
        + agl_check("revert", 'git::revert(["HEAD~1"], repo = "revert")', CONFLICTED)
        + _resolve("revert")
        + _completed("picking", 'git::resume(CherryPicking, "revert")')
        + agl_check("done-idle", 'git::operation("revert")', "None::[Operation]"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*", "std/fs"),
    )

    assert result.ok, result.error
    assert out.splitlines() == [
        *["ok", "raised", "ok", git_head(done, env), "ok", "ok"],
        *["ok", git_head(tmp_path / "pick", env), "ok", git_head(tmp_path / "revert", env), "ok"],
    ]
    assert git_lines(done, ["log", "--format=%s", "-4"], env) == ["c3", "c2", "c1", "ours"]
    assert git_head(stopped, env) == before
    assert git_output(stopped, ["status", "--porcelain"], env) == ""


def test_resumed_revert_sequence_pins_reference_configuration(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name="f.txt", content="one\n", message="one")
    commit_file(repo, env, name="g.txt", message="g")
    commit_file(repo, env, name="f.txt", content="two\n", message="two")
    git_run(repo, ["config", "revert.reference", "true"], env)

    out, result = run_git_program(
        agl_check("started", 'git::revert(["HEAD~2", "HEAD~1"], repo = "repo")', CONFLICTED)
        + _resolve("repo")
        + _completed("resumed", 'git::resume(Reverting, "repo")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*", "std/fs"),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok", git_head(repo, env)]
    for rev in ("HEAD", "HEAD~1"):
        assert git_output(repo, ["log", "-1", "--format=%B", rev], env).startswith('Revert "')


def test_merge_commits_cannot_be_picked_or_reverted(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["branch", "pick"], env)
    git_run(repo, ["checkout", "-qb", "side"], env)
    commit_file(repo, env, name="s.txt", message="s")
    git_run(repo, ["checkout", "-q", "main"], env)
    commit_file(repo, env, name="a.txt", message="a")
    commit_file(repo, env, name="b.txt", message="b")
    git_run(repo, ["merge", "-q", "--no-ff", "-m", "merge", "side"], env)
    tip = git_head(repo, env)
    pick_tip = git_output(repo, ["rev-parse", "pick"], env)
    none = "None::[Operation]"

    # A merge after the first commit of a sequence stops it with only the sequencer left behind.
    out, result = run_git_program(
        agl_raises('git::revert(["main"], repo = "repo")')
        + agl_check("idle-1", 'git::operation("repo")', none)
        + agl_raises('git::revert(["main~2", "main"], repo = "repo")')
        + agl_check("reverting", 'git::operation("repo")', "Some::[Operation](Reverting)")
        + 'git::abort(Reverting, "repo")'
        + agl_check("idle-2", 'git::operation("repo")', none)
        + 'git::checkout("pick", "repo")'
        + agl_raises('git::cherry-pick(["main"], repo = "repo")')
        + agl_check("idle-3", 'git::operation("repo")', none)
        + agl_raises('git::cherry-pick(["main~2", "main"], repo = "repo")')
        + agl_check("picking", 'git::operation("repo")', "Some::[Operation](CherryPicking)")
        + 'git::abort(CherryPicking, "repo")\n'
        + agl_check("idle-4", 'git::operation("repo")', none),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised", "ok", "raised", "ok", "ok", "raised", "ok", "raised"] + [
        "ok",
        "ok",
    ]
    assert git_output(repo, ["rev-parse", "main"], env) == tip
    assert git_head(repo, env) == pick_tip


def test_resume_that_empties_a_commit_completes(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    SCENARIOS["rebase"].setup(tmp_path / "repo", env)
    main = git_output(tmp_path / "repo", ["rev-parse", "main"], env)

    out, result = run_git_program(
        agl_check("started", 'git::rebase("main", repo = "repo")', CONFLICTED)
        # Resolving to `main`'s content leaves nothing to commit, so the commit is dropped.
        + _resolve("repo", "ours")
        + agl_check("dropped", 'git::resume(Rebasing, "repo")', f'Completed("{main}")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*", "std/fs"),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok", "ok"]
    assert git_head(tmp_path / "repo", env) == main


def test_resume_that_empties_a_sequencer_commit_fails_and_leaves_it_in_progress(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    SCENARIOS["cherry-pick"].setup(tmp_path / "pick", env)
    SCENARIOS["revert"].setup(tmp_path / "revert", env)
    cases = [
        ("pick", "CherryPicking", 'git::cherry-pick(["other"], repo = "pick")', "ours"),
        ("revert", "Reverting", 'git::revert(["HEAD~1"], repo = "revert")', "two"),
    ]

    out, result = run_git_program(
        "".join(
            agl_check(f"started-{repo}", start, CONFLICTED)
            # The resolution equals what `HEAD` already has.
            + _resolve(repo, content)
            + agl_raises(f'git::resume({state}, "{repo}")')
            + agl_check(f"state-{repo}", f'git::operation("{repo}")', f"Some::[Operation]({state})")
            + f'git::abort({state}, "{repo}")\n'
            + agl_check(f"idle-{repo}", f'git::operation("{repo}")', "None::[Operation]")
            for repo, state, start, content in cases
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*", "std/fs"),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok", "raised", "ok", "ok"] * 2


def test_rebase_pins_configuration_that_changes_its_meaning(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    refs = init_repo(tmp_path / "refs", env)
    git_run(refs, ["checkout", "-qb", "topic"], env)
    commit_file(refs, env, name="t1.txt", message="t1")
    git_run(refs, ["branch", "mid"], env)
    commit_file(refs, env, name="t2.txt", message="t2")
    git_run(refs, ["checkout", "-q", "main"], env)
    commit_file(refs, env, name="m.txt", message="m")
    git_run(refs, ["checkout", "-q", "topic"], env)
    git_run(refs, ["config", "rebase.updateRefs", "true"], env)
    mid = git_output(refs, ["rev-parse", "mid"], env)

    merges = init_repo(tmp_path / "merges", env)
    git_run(merges, ["checkout", "-qb", "side"], env)
    commit_file(merges, env, name="s.txt", message="s")
    git_run(merges, ["checkout", "-q", "main"], env)
    git_run(merges, ["checkout", "-qb", "topic"], env)
    commit_file(merges, env, name="t.txt", message="t")
    git_run(merges, ["merge", "-q", "--no-ff", "-m", "merge", "side"], env)
    git_run(merges, ["checkout", "-q", "main"], env)
    commit_file(merges, env, name="m.txt", message="m")
    git_run(merges, ["checkout", "-q", "topic"], env)
    git_run(merges, ["config", "rebase.rebaseMerges", "true"], env)

    backend = init_repo(tmp_path / "backend", env)
    conflicting_branches(backend, env)
    git_run(backend, ["checkout", "-q", "other"], env)
    git_run(backend, ["config", "rebase.backend", "apply"], env)

    out, result = run_git_program(
        _completed("refs", 'git::rebase("main", repo = "refs")')
        + _completed("merges", 'git::rebase("main", repo = "merges")')
        + agl_check("backend", 'git::rebase("main", repo = "backend")', CONFLICTED),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == [git_head(refs, env), git_head(merges, env), "ok"]
    assert git_output(refs, ["rev-parse", "mid"], env) == mid
    assert git_lines(merges, ["rev-list", "--merges", "main..HEAD"], env) == []
    assert (backend / ".git" / "rebase-merge").is_dir()
    assert not (backend / ".git" / "rebase-apply").exists()


def test_operation_detects_an_apply_backend_rebase(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    conflicting_branches(repo, env)
    git_run(repo, ["checkout", "-q", "other"], env)
    git_fails(repo, ["rebase", "--apply", "main"], env)
    assert (repo / ".git" / "rebase-apply").is_dir()

    out, result = run_git_program(
        agl_check("state", 'git::operation("repo")', "Some::[Operation](Rebasing)")
        + 'git::abort(Rebasing, "repo")'
        + agl_check("idle", 'git::operation("repo")', "None::[Operation]"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["ok", "ok"]


def test_error_after_the_operation_started_leaves_it_in_progress(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["checkout", "-qb", "topic"], env)
    commit_file(repo, env, name="x.txt", message="add x")
    git_run(repo, ["rm", "-q", "x.txt"], env)
    git_run(repo, ["commit", "-qm", "drop x"], env)
    git_run(repo, ["checkout", "-q", "main"], env)
    commit_file(repo, env, name="m.txt", message="m")
    git_run(repo, ["checkout", "-q", "topic"], env)
    # Replaying "add x" would overwrite this file.
    (repo / "x.txt").write_text("untracked\n", encoding="utf-8")

    out, result = run_git_program(
        agl_raises('git::rebase("main", repo = "repo")')
        + agl_check("state", 'git::operation("repo")', "Some::[Operation](Rebasing)")
        + 'git::abort(Rebasing, "repo")',
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised", "ok"]


# Remote synchronization: `fetch`, `pull` and `push` against local bare remotes.


def _clone_of(tmp_path: Path, env: dict[str, str], bare: Path, name: str) -> Path:
    git_run(None, ["clone", "-q", str(bare), str(tmp_path / name)], env)
    return tmp_path / name


def _peer(tmp_path: Path, env: dict[str, str], name: str) -> Path:
    """Clone the bare remote of ``name`` as its collaborator ``<name>-peer``."""
    return _clone_of(tmp_path, env, tmp_path / f"{name}-origin.git", f"{name}-peer")


def _remote_pair(
    tmp_path: Path,
    env: dict[str, str],
    name: str,
    *,
    ahead: int = 0,
    behind: int = 0,
    conflict: bool = False,
) -> tuple[Path, Path]:
    """Create bare remote ``<name>-origin.git`` and clone ``name``, then diverge them.

    The clone gets ``ahead`` local commits, the remote ``behind`` commits pushed by a peer. With
    ``conflict`` (at most one commit each) both change ``f.txt``. Returns remote and clone.
    """
    source = init_repo(tmp_path / f"{name}-source", env)
    if conflict:
        commit_file(source, env, name="f.txt", content="base\n", message="base")
    bare, repo = clone_bare_remote(source, tmp_path, env, name=name)
    for n in range(ahead):
        commit_file(
            repo,
            env,
            name="f.txt" if conflict else f"local{n}.txt",
            content=f"ours {n}\n",
            message=f"local {n}",
        )
    if behind:
        peer = _peer(tmp_path, env, name)
        for n in range(behind):
            commit_file(
                peer,
                env,
                name="f.txt" if conflict else f"remote{n}.txt",
                content=f"theirs {n}\n",
                message=f"remote {n}",
            )
        git_run(peer, ["push", "-q", "origin", "main"], env)
    return bare, repo


def _parents(repo: Path, env: dict[str, str], rev: str = "HEAD") -> list[str]:
    return git_output(repo, ["rev-list", "--parents", "-n1", rev], env).split()[1:]


def test_fetch_updates_the_refs_of_each_source(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = init_repo(tmp_path / "source", env)
    origin, _ = clone_bare_remote(source, tmp_path, env, name="up")
    fork = tmp_path / "fork.git"
    git_run(None, ["clone", "--bare", "-q", str(origin), str(fork)], env)
    names = ("up", "from", "spec", "all")
    for name in names[1:]:
        _clone_of(tmp_path, env, origin, name)
    for name in names:
        git_run(tmp_path / name, ["remote", "add", "fork", str(fork)], env)
    # Since git 2.44 this would make a plain fetch fetch every remote.
    git_run(tmp_path / "up", ["config", "fetch.all", "true"], env)
    old = git_rev(origin, env, "main")
    peer = _peer(tmp_path, env, "up")
    commit_file(peer, env, name="new.txt", message="new")
    git_run(peer, ["push", "-q", "origin", "main"], env)
    git_run(peer, ["checkout", "-qb", "feat"], env)
    commit_file(peer, env, name="feat.txt", message="feat")
    git_run(peer, ["push", "-q", "origin", "feat"], env)
    git_run(peer, ["remote", "add", "fork", str(fork)], env)
    git_run(peer, ["checkout", "-qb", "ffeat", "main"], env)
    commit_file(peer, env, name="ffeat.txt", message="ffeat")
    git_run(peer, ["push", "-q", "fork", "ffeat"], env)
    new = git_rev(origin, env, "main")
    feat = git_rev(origin, env, "feat")
    ffeat = git_rev(fork, env, "ffeat")

    out, result = run_git_program(
        'git::fetch(repo = "up")\n'
        'git::fetch(FetchFrom("fork"), repo = "from")\n'
        'git::fetch(FetchFrom("origin", ["refs/heads/feat:refs/remotes/origin/feat"]), '
        'repo = "spec")\n'
        'git::fetch(AllRemotes, repo = "all")',
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out == ""
    assert git_refs(tmp_path / "up", env, "remotes") == {"origin/main": new, "origin/feat": feat}
    assert git_refs(tmp_path / "from", env, "remotes") == {
        "origin/main": old,
        "fork/main": old,
        "fork/ffeat": ffeat,
    }
    assert git_refs(tmp_path / "spec", env, "remotes") == {"origin/main": old, "origin/feat": feat}
    assert git_refs(tmp_path / "all", env, "remotes") == {
        "origin/main": new,
        "origin/feat": feat,
        "fork/main": old,
        "fork/ffeat": ffeat,
    }
    # `fetch` never touches the work tree or the local branches.
    assert git_rev(tmp_path / "all", env, "main") == old
    assert git_output(tmp_path / "all", ["status", "--porcelain"], env) == ""


def test_fetch_prune_and_tags(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = init_repo(tmp_path / "source", env)
    git_run(source, ["branch", "gone"], env)
    origin, _ = clone_bare_remote(source, tmp_path, env, name="keep")
    names = ("keep", "prune", "both", "plain", "tagged")
    for name in names[1:]:
        _clone_of(tmp_path, env, origin, name)
    git_run(origin, ["branch", "-qD", "gone"], env)
    # A tag on a commit no branch reaches is fetched only on request.
    peer = _peer(tmp_path, env, "keep")
    git_run(peer, ["checkout", "-q", "--detach"], env)
    commit_file(peer, env, name="tagged.txt", message="tagged")
    git_run(peer, ["tag", "-a", "remote-tag", "-m", "remote"], env)
    git_run(peer, ["push", "-q", "origin", "remote-tag"], env)
    # Configuration that would make a plain fetch prune, or `--prune` also prune tags.
    for name in ("keep", "prune", "both"):
        repo = tmp_path / name
        git_run(repo, ["config", "fetch.pruneTags", "true"], env)
        git_run(repo, ["tag", "local-tag"], env)
    git_run(tmp_path / "keep", ["config", "fetch.prune", "true"], env)
    git_run(tmp_path / "keep", ["config", "remote.origin.prune", "true"], env)

    out, result = run_git_program(
        'git::fetch(repo = "keep")\n'
        'git::fetch(prune = true, repo = "prune")\n'
        'git::fetch(prune = true, tags = true, repo = "both")\n'
        'git::fetch(repo = "plain")\n'
        'git::fetch(tags = true, repo = "tagged")',
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    stale = {"origin/main", "origin/gone"}
    expected = {
        "keep": (stale, ["local-tag"]),
        "prune": ({"origin/main"}, ["local-tag"]),
        "both": ({"origin/main"}, ["local-tag", "remote-tag"]),
        "plain": (stale, []),
        "tagged": (stale, ["remote-tag"]),
    }
    for name, (branches, tags) in expected.items():
        repo = tmp_path / name
        assert (set(git_refs(repo, env, "remotes")), git_lines(repo, ["tag"], env)) == (
            branches,
            tags,
        )


def _with_submodule(tmp_path: Path, env: dict[str, str], name: str) -> tuple[Path, Path, Path]:
    """Create a bare remote whose ``main`` has submodule ``sub``, and a clone with it checked out.

    Returns the remote, the clone and the submodule's source repository.
    """
    allow = ["-c", "protocol.file.allow=always"]
    submodule = init_repo(tmp_path / f"{name}-sub", env)
    source = init_repo(tmp_path / f"{name}-source", env)
    git_run(source, [*allow, "submodule", "add", "-q", str(submodule), "sub"], env)
    git_run(source, ["commit", "-qm", "add sub"], env)
    bare, repo = clone_bare_remote(source, tmp_path, env, name=name)
    git_run(repo, [*allow, "submodule", "update", "--init", "-q"], env)
    return bare, repo, submodule


def _allow_file_submodules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the git that AgL runs clone and fetch submodules from local paths."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


def test_fetch_ignores_configuration_that_changes_what_it_fetches(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _allow_file_submodules(monkeypatch)
    bare, fetcher, submodule = _with_submodule(tmp_path, env, "fetcher")
    commit_file(submodule, env, name="s2.txt", message="s2")
    inner = git_head(submodule, env)
    peer = _peer(tmp_path, env, "fetcher")
    git_run(peer, ["update-index", "--add", "--cacheinfo", f"160000,{inner},sub"], env)
    git_run(peer, ["commit", "-qm", "bump sub"], env)
    git_run(peer, ["push", "-q", "origin", "main"], env)
    git_run(fetcher, ["config", "fetch.recurseSubmodules", "true"], env)

    out, result = run_git_program(
        'git::fetch(repo = "fetcher")', tmp_path, monkeypatch, capsys, imports=("std/git::*",)
    )

    assert result.ok, result.error
    assert git_refs(fetcher, env, "remotes") == {"origin/main": git_rev(bare, env, "main")}
    git_fails(fetcher / "sub", ["cat-file", "-e", inner], env)


def test_pull_integrates_per_mode_ignoring_pull_configuration(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shapes = {
        "utd": (0, 0),
        "ff": (0, 2),
        "ffr": (0, 2),
        "never": (0, 1),
        "only": (1, 1),
        "div": (1, 1),
        "divr": (1, 1),
    }
    bares = {}
    for name, (ahead, behind) in shapes.items():
        bares[name], repo = _remote_pair(tmp_path, env, name, ahead=ahead, behind=behind)
        # `pull` follows `mode`, whatever `git pull` would be configured to do.
        merging = name in ("div", "only")
        git_run(repo, ["config", "pull.rebase", "true" if merging else "false"], env)
        git_run(repo, ["config", "pull.ff", "only" if merging else "false"], env)
        git_run(repo, ["config", "branch.main.rebase", "true" if merging else "false"], env)
    tips = {name: git_rev(bare, env, "main") for name, bare in bares.items()}
    heads = {name: git_head(tmp_path / name, env) for name in shapes}

    out, result = run_git_program(
        agl_check("utd", 'git::pull(repo = "utd")', "UpToDate")
        + agl_check("utd-rebase", 'git::pull(mode = Rebase, repo = "utd")', "UpToDate")
        + agl_check("ff", 'git::pull(repo = "ff")', f'FastForwarded("{tips["ff"]}")')
        + agl_check(
            "ffr", 'git::pull(mode = Rebase, repo = "ffr")', f'FastForwarded("{tips["ffr"]}")'
        )
        + _completed("never", 'git::pull(mode = Merge(Never), repo = "never")')
        + agl_raises('git::pull(mode = Merge(Only), repo = "only")')
        + _completed("div", 'git::pull(repo = "div")')
        + _completed("divr", 'git::pull(mode = Rebase, repo = "divr")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == [
        "ok",
        "ok",
        "ok",
        "ok",
        git_head(tmp_path / "never", env),
        "raised",
        git_head(tmp_path / "div", env),
        git_head(tmp_path / "divr", env),
    ]
    assert git_head(tmp_path / "utd", env) == heads["utd"]
    assert git_head(tmp_path / "ff", env) == tips["ff"]
    assert git_head(tmp_path / "ffr", env) == tips["ffr"]
    assert _parents(tmp_path / "never", env) == [heads["never"], tips["never"]]
    assert git_head(tmp_path / "only", env) == heads["only"]
    assert _parents(tmp_path / "div", env) == [heads["div"], tips["div"]]
    assert _parents(tmp_path / "divr", env) == [tips["divr"]]


def test_pull_conflicts_stop_the_operation_and_refusals_are_errors(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("merge", "rebase"):
        _remote_pair(tmp_path, env, name, ahead=1, behind=1, conflict=True)
    for name in ("dirty", "dirty-rebase"):
        _, repo = _remote_pair(tmp_path, env, name, behind=1)
        # The incoming commit would overwrite this file.
        (repo / "remote0.txt").write_text("mine\n", encoding="utf-8")
    _, solo = _remote_pair(tmp_path, env, "solo")
    git_run(solo, ["checkout", "-qb", "lonely"], env)
    _, detached = _remote_pair(tmp_path, env, "detached")
    git_run(detached, ["checkout", "-q", "--detach"], env)
    rebase_head = git_head(tmp_path / "rebase", env)
    heads = {name: git_head(tmp_path / name, env) for name in ("dirty", "dirty-rebase", "solo")}
    heads["detached"] = git_head(detached, env)

    out, result = run_git_program(
        agl_check("merge", 'git::pull(repo = "merge")', CONFLICTED)
        + agl_check("merging", 'git::operation("merge")', "Some::[Operation](Merging)")
        + _resolve("merge")
        + _completed("resumed", 'git::resume(Merging, "merge")')
        + agl_check("rebase", 'git::pull(mode = Rebase, repo = "rebase")', CONFLICTED)
        + agl_check("rebasing", 'git::operation("rebase")', "Some::[Operation](Rebasing)")
        + 'git::abort(Rebasing, "rebase")'
        + agl_raises('git::pull(repo = "dirty")')
        + agl_raises('git::pull(mode = Rebase, repo = "dirty-rebase")')
        + agl_raises('git::pull(repo = "solo")')
        + agl_raises('git::pull(mode = Rebase, repo = "solo")')
        + agl_raises('git::pull(repo = "detached")')
        + agl_check("idle", 'git::operation("dirty")', "None::[Operation]")
        + agl_check("idle-rebase", 'git::operation("dirty-rebase")', "None::[Operation]"),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*", "std/fs"),
    )

    assert result.ok, result.error
    assert out.splitlines() == [
        "ok",
        "ok",
        git_head(tmp_path / "merge", env),
        "ok",
        "ok",
        *["raised"] * 5,
        "ok",
        "ok",
    ]
    assert git_output(tmp_path / "merge", ["show", "HEAD:f.txt"], env) == "resolved"
    assert _parent_count(tmp_path / "merge", env) == 2
    assert git_head(tmp_path / "rebase", env) == rebase_head
    assert git_output(tmp_path / "rebase", ["status", "--porcelain"], env) == ""
    for name, head in heads.items():
        assert git_head(tmp_path / name, env) == head
    # A missing upstream is found before anything is fetched.
    for name in ("solo", "detached"):
        assert not (tmp_path / name / ".git" / "FETCH_HEAD").exists()
    assert (tmp_path / "dirty" / "remote0.txt").read_text(encoding="utf-8") == "mine\n"


def _with_feature(
    tmp_path: Path, env: dict[str, str], name: str, *, ahead: int = 0
) -> tuple[Path, Path, str]:
    """Create a remote and clone ``name`` as in :func:`_remote_pair`, then publish ``feat``.

    ``feat`` has two commits on top of ``main`` and an annotated tag on its tip, which a fetch
    follows into ``FETCH_HEAD`` as a not-for-merge entry. Returns the remote, the clone, and the
    tip of ``feat``.
    """
    bare, repo = _remote_pair(tmp_path, env, name, ahead=ahead)
    peer = _peer(tmp_path, env, name)
    git_run(peer, ["checkout", "-qb", "feat", "main"], env)
    for n in range(2):
        commit_file(peer, env, name=f"feat{n}.txt", message=f"feat {n}")
    git_run(peer, ["tag", "-a", "feat-tag", "-m", "feat"], env)
    git_run(peer, ["push", "-q", "origin", "feat", "feat-tag"], env)
    return bare, repo, git_rev(bare, env, "feat")


def test_pull_from_integrates_the_named_branch(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, named, tip = _with_feature(tmp_path, env, "named")
    _, rebased, rebased_tip = _with_feature(tmp_path, env, "rebased", ahead=1)
    bare, url, url_tip = _with_feature(tmp_path, env, "url")
    git_run(named, ["config", "remote.origin.tagOpt", "--tags"], env)
    named_head = git_head(named, env)
    url_head = git_head(url, env)

    out, result = run_git_program(
        agl_raises('git::pull(PullFrom("origin", "nope"), repo = "named")')
        + agl_raises('git::pull(PullFrom("origin", "main:other"), repo = "named")')
        + agl_raises('git::pull(PullFrom("origin", "refs/heads/*:refs/heads/*"), repo = "named")')
        + agl_check(
            "named-up-to-date", 'git::pull(PullFrom("origin", "main"), repo = "named")', "UpToDate"
        )
        + agl_check(
            "named",
            'git::pull(PullFrom("origin", "feat"), repo = "named")',
            f'FastForwarded("{tip}")',
        )
        + _completed("rebased", 'git::pull(PullFrom("origin", "feat"), Rebase, "rebased")')
        + _completed("url", f'git::pull(PullFrom("{bare}", "feat"), Merge(Never), "url")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == [
        "raised",
        "raised",
        "raised",
        "ok",
        "ok",
        git_head(rebased, env),
        git_head(url, env),
    ]
    # The remote's tag policy lists tags next to the branch in `FETCH_HEAD`; only the branch merges.
    assert "not-for-merge" in (named / ".git" / "FETCH_HEAD").read_text(encoding="utf-8")
    # A branch name that is a refspec is rejected before git sees it, so no branch is created.
    assert set(git_refs(named, env, "heads")) == {"main"}
    assert git_head(named, env) == tip != named_head
    assert _parents(rebased, env) == [rebased_tip]
    assert _parents(url, env) == [url_head, url_tip]
    # Pulling a branch does not change what the current branch tracks.
    assert git_output(named, ["rev-parse", "--abbrev-ref", "main@{upstream}"], env) == "origin/main"


def test_push_sends_to_each_target(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bares = {}
    for name in ("up", "topic", "to", "refs", "setup", "unset", "solo", "triangle", "gone", "url"):
        bares[name], repo = _remote_pair(tmp_path, env, name, ahead=1)
        # Git's own `push` would push every matching branch.
        git_run(repo, ["config", "push.default", "matching"], env)
    base = {name: git_rev(bare, env, "main") for name, bare in bares.items()}
    # `Upstream` pushes to the upstream branch, whatever its name.
    topic = tmp_path / "topic"
    git_run(topic, ["checkout", "-q", "-B", "topic", "--track", "origin/main"], env)
    git_run(topic, ["config", "push.default", "simple"], env)
    commit_file(topic, env, name="topic.txt", message="topic")
    # Pushing to a remote without refspecs pushes the current branch only.
    to = tmp_path / "to"
    git_run(to, ["branch", "side", "main~1"], env)
    git_run(to, ["push", "-q", "origin", "side"], env)
    git_run(to, ["checkout", "-q", "side"], env)
    commit_file(to, env, name="side.txt", message="side")
    git_run(to, ["checkout", "-q", "main"], env)
    commit_file(to, env, name="more.txt", message="more")
    side = git_rev(bares["to"], env, "side")
    refs = tmp_path / "refs"
    commit_file(refs, env, name="second.txt", message="second")
    for name in ("setup", "unset", "solo"):
        git_run(tmp_path / name, ["checkout", "-qb", "feat"], env)
    # Git would create the missing upstream of `solo`.
    git_run(tmp_path / "solo", ["config", "push.default", "simple"], env)
    git_run(tmp_path / "solo", ["config", "push.autoSetupRemote", "true"], env)
    # The upstream is configured, but its remote-tracking branch is gone; git recreates the branch.
    git_run(tmp_path / "gone", ["update-ref", "-d", "refs/remotes/origin/main"], env)
    # Git refuses to push the upstream branch to a remote that is not its upstream's.
    fork = tmp_path / "fork.git"
    git_run(None, ["clone", "--bare", "-q", str(bares["triangle"]), str(fork)], env)
    git_run(tmp_path / "triangle", ["remote", "add", "fork", str(fork)], env)
    git_run(tmp_path / "triangle", ["config", "remote.pushDefault", "fork"], env)

    out, result = run_git_program(
        'git::push(repo = "up")\n'
        'git::push(repo = "topic")\n'
        'git::push(PushTo("origin"), repo = "to")\n'
        'git::push(PushTo("origin", ["HEAD:refs/heads/new", "HEAD~1:refs/heads/old"]), '
        'repo = "refs")\n'
        'git::push(PushTo("origin", [":refs/heads/old"]), repo = "refs")\n'
        'git::push(PushTo("origin"), set-upstream = true, repo = "setup")\n'
        'git::push(PushTo("origin"), repo = "unset")\n'
        'git::push(repo = "gone")\n'
        f'git::push(PushTo("{bares["url"]}"), repo = "url")'
        + agl_raises('git::push(repo = "solo")')
        + agl_raises('git::push(repo = "triangle")'),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised"] * 2
    assert git_refs(bares["up"], env, "heads") == {"main": git_head(tmp_path / "up", env)}
    assert git_refs(bares["topic"], env, "heads") == {"main": git_head(topic, env)}
    assert git_refs(bares["to"], env, "heads") == {"main": git_head(to, env), "side": side}
    assert git_refs(bares["refs"], env, "heads") == {
        "main": base["refs"],
        "new": git_head(refs, env),
    }
    assert git_refs(bares["setup"], env, "heads") == {
        "main": base["setup"],
        "feat": git_head(tmp_path / "setup", env),
    }
    assert git_refs(bares["unset"], env, "heads") == {
        "main": base["unset"],
        "feat": git_head(tmp_path / "unset", env),
    }
    assert (
        git_output(tmp_path / "setup", ["rev-parse", "--abbrev-ref", "feat@{upstream}"], env)
        == "origin/feat"
    )
    git_fails(tmp_path / "unset", ["rev-parse", "--abbrev-ref", "feat@{upstream}"], env)
    for name in ("gone", "url"):
        assert git_refs(bares[name], env, "heads") == {"main": git_head(tmp_path / name, env)}
    assert git_refs(bares["solo"], env, "heads") == {"main": base["solo"]}
    assert git_refs(bares["triangle"], env, "heads") == {"main": base["triangle"]}
    assert git_refs(fork, env, "heads") == {"main": base["triangle"]}


def test_push_force_modes(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bares = {}
    for name in ("amend", "rejected", "fetched", "forced"):
        diverged = int(name != "amend")
        bares[name], _ = _remote_pair(tmp_path, env, name, ahead=diverged, behind=diverged)
    git_run(tmp_path / "amend", ["commit", "--amend", "-qm", "amended"], env)
    # Git would also refuse to overwrite a commit that this branch's history lacks.
    git_run(tmp_path / "amend", ["config", "push.useForceIfIncludes", "true"], env)
    # A lease holds for what the remote-tracking branch says, so a fetch renews it.
    git_run(tmp_path / "fetched", ["fetch", "-q"], env)
    theirs = {name: git_rev(bare, env, "main") for name, bare in bares.items()}

    out, result = run_git_program(
        agl_raises('git::push(repo = "amend")')
        + 'git::push(force = WithLease, repo = "amend")\n'
        + agl_raises('git::push(force = NoForce, repo = "rejected")')
        + agl_raises('git::push(force = WithLease, repo = "rejected")')
        + agl_raises('git::push(repo = "fetched")')
        + 'git::push(force = WithLease, repo = "fetched")\n'
        + 'git::push(force = Forced, repo = "forced")',
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised"] * 4
    assert git_refs(bares["rejected"], env, "heads") == {"main": theirs["rejected"]}
    for name in ("amend", "fetched", "forced"):
        assert git_refs(bares[name], env, "heads") == {"main": git_head(tmp_path / name, env)}
        assert theirs[name] not in git_lines(tmp_path / name, ["rev-list", "HEAD"], env)


def test_push_tags(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bares = {}
    for name in ("up", "to", "with-refs"):
        bares[name], repo = _remote_pair(tmp_path, env, name, ahead=1)
        git_run(repo, ["tag", f"light-{name}"], env)
        git_run(repo, ["tag", "-a", f"ann-{name}", "-m", "ann"], env)
    base = {name: git_rev(bare, env, "main") for name, bare in bares.items()}

    out, result = run_git_program(
        'git::push(tags = true, repo = "up")\n'
        'git::push(PushTo("origin"), tags = true, repo = "to")\n'
        'git::push(PushTo("origin", ["main"]), tags = true, repo = "with-refs")',
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    # Without refspecs, git pushes only the tags.
    for name in ("up", "to"):
        assert git_lines(bares[name], ["tag"], env) == [f"ann-{name}", f"light-{name}"]
        assert git_refs(bares[name], env, "heads") == {"main": base[name]}
    name = "with-refs"
    assert git_lines(bares[name], ["tag"], env) == [f"ann-{name}", f"light-{name}"]
    assert git_refs(bares[name], env, "heads") == {"main": git_head(tmp_path / name, env)}


def test_push_ignores_configuration_that_changes_what_it_sends(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _allow_file_submodules(monkeypatch)
    bares = {}
    for name in ("follow", "signed", "options"):
        bares[name], repo = _remote_pair(tmp_path, env, name, ahead=1)
    follow = tmp_path / "follow"
    git_run(follow, ["tag", "-a", "annotated", "-m", "annotated"], env)
    git_run(follow, ["config", "push.followTags", "true"], env)
    # The remote advertises no signed pushes, so a signed push is an error.
    git_run(tmp_path / "signed", ["config", "push.gpgSign", "true"], env)
    # The remote does not advertise push options, so sending one is an error.
    git_run(tmp_path / "options", ["config", "push.pushOption", "x"], env)
    # A superproject commit whose submodule commit is on no remote is refused when checked.
    bares["pusher"], pusher, _ = _with_submodule(tmp_path, env, "pusher")
    commit_file(pusher / "sub", env, name="s2.txt", message="s2")
    git_run(pusher, ["commit", "-qam", "bump sub"], env)
    git_run(pusher, ["config", "push.recurseSubmodules", "check"], env)

    out, result = run_git_program(
        "".join(
            f'git::push(repo = "{name}")\n' for name in ("follow", "signed", "options", "pusher")
        ),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert git_lines(bares["follow"], ["tag"], env) == []
    for name in ("follow", "signed", "options", "pusher"):
        assert git_refs(bares[name], env, "heads") == {"main": git_head(tmp_path / name, env)}


def test_option_like_operands_never_act_as_options(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bare, repo = _remote_pair(tmp_path, env, "repo", ahead=1)
    git_run(repo, ["branch", "extra"], env)
    marker = tmp_path / "marker"
    upload = f"--upload-pack=touch {marker}"
    receive = f"--receive-pack=touch {marker}"
    calls = [
        f'git::fetch(FetchFrom("{upload}"), repo = "repo")',
        f'git::fetch(FetchFrom("origin", ["{upload}"]), repo = "repo")',
        f'git::pull(PullFrom("{upload}", "main"), repo = "repo")',
        f'git::pull(PullFrom("origin", "{upload}"), repo = "repo")',
        'git::pull(PullFrom("origin", "--upload-pack=x"), repo = "repo")',
        f'git::push(PushTo("{receive}"), repo = "repo")',
        f'git::push(PushTo("--exec=touch {marker}"), repo = "repo")',
        f'git::push(PushTo("origin", ["{receive}"]), repo = "repo")',
        'git::push(PushTo("origin", ["--all"]), repo = "repo")',
    ]
    before = git_refs(bare, env, "heads")
    head = git_head(repo, env)

    out, result = run_git_program(
        "".join(agl_raises(call) for call in calls),
        tmp_path,
        monkeypatch,
        capsys,
        imports=("std/git::*",),
    )

    assert result.ok, result.error
    assert out.splitlines() == ["raised"] * len(calls)
    assert not marker.exists()
    assert git_refs(bare, env, "heads") == before
    assert git_head(repo, env) == head
    assert set(git_refs(repo, env, "heads")) == {"extra", "main"}
