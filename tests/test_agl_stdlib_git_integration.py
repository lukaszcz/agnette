"""Local integration of ``std/git``: merge, rebase, cherry-pick, revert, and operation state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._agl_git_helpers import agl_check, agl_raises, run_git_program
from tests._git_helpers import (
    add_linked_worktree,
    commit_file,
    conflicted_merge,
    conflicting_branches,
    git_fails,
    git_head,
    git_lines,
    git_output,
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
def test_operation_in_progress_refuses_other_calls_and_option_like_refs(
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
