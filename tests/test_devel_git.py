"""The ``devel`` package's agent-assisted merge on real git, with a mocked agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.pipeline import PipelineDriver
from agnette.agl.runtime.agents import value_driven_agent_factory
from tests._agl_git_helpers import run_git_program
from tests._agl_helpers import hermetic_get_sandbox_context, package_roots
from tests._git_helpers import (
    commit_file,
    conflicting_branches,
    git_output,
    git_parents,
    git_rev,
    git_run,
    init_repo,
    write_file,
)
from tests._package_helpers import package_info
from tests.conftest import FakeAgentTransport

_DEVEL_ROOT = Path(__file__).resolve().parents[1] / "packages" / "devel"

_BODY = """
try
  AgentClaude("sonnet", "high").merge("%s")
  print("merged")
catch GitError =>
  print("raised")
"""


def _run_merge(
    ref: str, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    """Run ``devel/git``'s ``Agent::merge(ref)`` in *repo*; return ``merged`` or ``raised``."""
    runtime = PipelineDriver(
        resolve_agent_spec=None,
        agent_dispatcher=value_driven_agent_factory(
            idle_timeout=None, get_sandbox_context=hermetic_get_sandbox_context()
        ),
        get_sandbox_context=None,
    )
    out = run_git_program(
        _BODY % ref,
        repo,
        monkeypatch,
        capsys,
        imports=("devel/git",),
        runtime=runtime,
        roots=package_roots(package_info(_DEVEL_ROOT), cwd=_DEVEL_ROOT),
    )
    return out.strip()


def test_conflicting_merge_asks_the_agent_to_finish_it(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_agent_transport: FakeAgentTransport,
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    conflicting_branches(repo, env)
    main = git_rev(repo, env)

    def resolve() -> object:
        write_file(repo, "f.txt", "both\n")
        git_run(repo, ["add", "f.txt"], env)
        git_run(repo, ["commit", "--no-edit", "-q"], env)
        return fake_agent_transport.success("done")

    fake_agent_transport.queue(resolve)

    assert _run_merge("other", repo, monkeypatch, capsys) == "merged"
    assert len(fake_agent_transport.calls) == 1
    assert git_parents(repo, env) == [
        main,
        git_rev(repo, env, "other"),
    ]
    assert git_output(repo, ["show", "HEAD:f.txt"], env) == "both"
    assert git_output(repo, ["status", "--porcelain"], env) == ""


def test_clean_merge_does_not_ask_the_agent(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_agent_transport: FakeAgentTransport,
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    git_run(repo, ["checkout", "-qb", "other"], env)
    commit_file(repo, env, name="theirs.txt")
    git_run(repo, ["checkout", "-q", "main"], env)
    commit_file(repo, env, name="ours.txt")
    main = git_rev(repo, env)

    assert _run_merge("other", repo, monkeypatch, capsys) == "merged"
    assert fake_agent_transport.calls == []
    assert git_parents(repo, env) == [
        main,
        git_rev(repo, env, "other"),
    ]


def test_failing_merge_raises_git_error_without_asking_the_agent(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_agent_transport: FakeAgentTransport,
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    head = git_rev(repo, env)

    assert _run_merge("no-such-ref", repo, monkeypatch, capsys) == "raised"
    assert fake_agent_transport.calls == []
    assert git_rev(repo, env) == head
