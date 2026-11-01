"""``deliver-plan-parallel`` of the ``devel`` package on real git, with a scripted mock agent.

Forked task workers run in their own processes, so the mock is a command agent acting on the files
of a scenario: ``tasks.json`` describes each task, and everything it observes lands in ``log/``.
``std/ps::fork`` needs a single-threaded process, so each delivery runs in the isolated runner.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests._devel_helpers import run_devel_program
from tests._git_helpers import commit_file, git_lines, git_output, init_repo, write_script
from tests.agl.isolated_runner import run_callable

Tasks = dict[str, dict[str, object]]

_CONFLICTING: Tasks = {
    "one": {"files": {"shared.txt": "one\n"}, "meet": ["two"]},
    "two": {"files": {"shared.txt": "two\n"}, "meet": ["one"]},
}

_MOCK_AGENT = f"""#!{sys.executable}
import json, re, subprocess, sys, time
from pathlib import Path

scenario, prompt = Path(sys.argv[1]), Path(sys.argv[2]).read_text()
tasks = json.loads((scenario / "tasks.json").read_text())
log = scenario / "log"


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


def commit_all(message):
    git("add", "-A")
    git("commit", "-q", "-m", message)


def wait_for(path):
    deadline = time.monotonic() + 20
    while not path.exists():
        if time.monotonic() > deadline:
            sys.exit(1)
        time.sleep(0.01)


def select():
    if done := re.search(r"Mark (\\S+) as done", prompt):
        (log / f"done-{{Path(done[1]).stem}}").touch()
    running = {{Path(line[2:]).stem for line in prompt.splitlines() if line.startswith("- /")}}
    finished = {{p.name.removeprefix("done-") for p in log.glob("done-*")}}
    ready = [
        str(scenario / "tasks" / f"{{name}}.md")
        for name, task in tasks.items()
        if name not in finished | running and finished >= set(task.get("after", []))
    ]
    print(json.dumps(ready))


def implement(name):
    task = tasks[name]
    (log / f"base-{{name}}").write_text(" ".join(sorted(p.name for p in Path().iterdir())))
    if "Reason task is not done" in prompt:
        (log / f"revised-{{name}}").touch()
    for peer in task.get("meet", []):
        (log / f"at-{{name}}").touch()
        wait_for(log / f"at-{{peer}}")
    (log / f"active-{{name}}").touch()
    (log / f"peak-{{name}}").write_text(str(len(list(log.glob("active-*")))))
    time.sleep(task.get("hold", 0))
    (log / f"active-{{name}}").unlink()
    if "raises" in task:
        sys.exit(task["raises"])
    for file, content in task["files"].items():
        Path(file).write_text(content)
    if not task.get("uncommitted"):
        commit_all(f"implement {{name}}")


def verify(name):
    if tasks[name].get("unfinished_once") and not (log / f"revised-{{name}}").exists():
        print(json.dumps({{"$case": "Incomplete", "reason": "not yet"}}))
    else:
        print(json.dumps({{"$case": "Complete", "next-task-file": {{"$case": "None"}}}}))


if implementation := re.search(r"Implement the task described in @(\\S+)\\.", prompt):
    implement(Path(implementation[1]).stem)
elif verification := re.search(r"Verify if (\\S+) is completed", prompt):
    verify(Path(verification[1]).stem)
elif "Fix all conflicts" in prompt:
    (log / "conflict-fixed").touch()
    if not (scenario / "skip-conflict-fix").exists():
        Path("shared.txt").write_text("both\\n")
        git("add", "shared.txt")
        git("commit", "-q", "--no-edit")
elif "Commit changes on the current branch" in prompt:
    commit_all("commit leftovers")
elif prompt.startswith("Review "):
    print(json.dumps({{"issues": []}}))
else:
    select()
"""


def _run_delivery(workdir: Path, body: str) -> None:
    """Run *body* in ``workdir/repo``, called in the isolated process."""
    with pytest.MonkeyPatch.context() as patch:
        imports = ("std/config", "devel/deliver")
        run_devel_program(body, workdir / "repo", patch, imports=imports)


def _deliver(
    tmp_path: Path,
    tasks: Tasks,
    *,
    jobs: int = 2,
    verify: bool = True,
    max_refine_steps: int = 0,
    save_reviews: bool = False,
) -> str:
    """Run ``deliver-plan-parallel`` on *tasks* in ``tmp_path/repo``; return its outcome."""
    scenario = tmp_path / "scenario"
    (scenario / "log").mkdir(parents=True, exist_ok=True)
    (scenario / "tasks.json").write_text(json.dumps(tasks))
    agent = write_script(scenario, "agent", _MOCK_AGENT)
    command = f"{agent} {scenario} \\%{{PROMPT_FILE}} \\%{{SESSION_ID}}"
    body = f"""
std/config::sandbox := Disabled
let agent = AgentCommand("{command}")
let session = Session::open(agent)
try
  session.deliver-plan-parallel(
    "plan.md", "{scenario}/tasks", agent, agent, agent,
    {str(verify).lower()}, {max_refine_steps}, {str(save_reviews).lower()}, {jobs}
  )
  print("delivered")
catch _ =>
  print("raised")
"""
    out = run_callable(_run_delivery, tmp_path, {"body": body}).stdout
    return out.strip().splitlines()[-1]


def _log(tmp_path: Path, name: str) -> Path:
    return tmp_path / "scenario" / "log" / name


def _branches(repo: Path, env: dict[str, str]) -> list[str]:
    return git_lines(repo, ["for-each-ref", "--format=%(refname:short)", "refs/heads"], env)


def _worktrees(repo: Path, env: dict[str, str]) -> list[str]:
    lines = git_lines(repo, ["worktree", "list", "--porcelain"], env)
    return [line for line in lines if line.startswith("worktree ")]


def _file_at_head(repo: Path, name: str, env: dict[str, str]) -> str:
    return git_output(repo, ["show", f"HEAD:{name}"], env)


def test_ready_tasks_run_concurrently_and_dependents_build_on_their_merges(
    tmp_path: Path, env: dict[str, str]
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    tasks: Tasks = {
        "one": {"files": {"one.txt": "one\n"}, "meet": ["two"]},
        "two": {"files": {"two.txt": "two\n"}, "meet": ["one"]},
        "three": {"files": {"three.txt": "three\n"}, "after": ["one", "two"]},
    }

    assert _deliver(tmp_path, tasks) == "delivered"

    for name in ("one", "two", "three"):
        assert _file_at_head(repo, f"{name}.txt", env) == name
    base = _log(tmp_path, "base-three").read_text().split()
    assert {"one.txt", "two.txt"} <= set(base)
    assert _branches(repo, env) == ["main"]
    assert len(_worktrees(repo, env)) == 1
    assert git_output(repo, ["status", "--porcelain"], env) == ""


def test_at_most_jobs_tasks_run_at_once(tmp_path: Path, env: dict[str, str]) -> None:
    repo = init_repo(tmp_path / "repo", env)
    names = ("one", "two", "three", "four")
    tasks: Tasks = {name: {"files": {f"{name}.txt": "x\n"}, "hold": 0.05} for name in names}

    assert _deliver(tmp_path, tasks, jobs=2) == "delivered"

    assert max(int(_log(tmp_path, f"peak-{name}").read_text()) for name in names) <= 2
    assert all(_file_at_head(repo, f"{name}.txt", env) == "x" for name in names)


def test_conflicting_tasks_are_merged_by_the_agent(tmp_path: Path, env: dict[str, str]) -> None:
    repo = init_repo(tmp_path / "repo", env)

    assert _deliver(tmp_path, _CONFLICTING) == "delivered"

    assert _log(tmp_path, "conflict-fixed").exists()
    assert _file_at_head(repo, "shared.txt", env) == "both"
    assert _branches(repo, env) == ["main"]
    assert len(_worktrees(repo, env)) == 1


def test_a_merge_the_agent_leaves_unfinished_fails_and_keeps_the_branch(
    tmp_path: Path, env: dict[str, str]
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    (tmp_path / "scenario").mkdir()
    (tmp_path / "scenario" / "skip-conflict-fix").touch()

    assert _deliver(tmp_path, _CONFLICTING) == "raised"

    assert len(_branches(repo, env)) == 2
    assert len(_worktrees(repo, env)) == 1


def test_an_unfinished_task_is_revised_before_it_is_merged(
    tmp_path: Path, env: dict[str, str]
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    tasks: Tasks = {
        "one": {"files": {"one.txt": "revised\n"}, "unfinished_once": True, "uncommitted": True},
    }

    assert _deliver(tmp_path, tasks) == "delivered"

    assert _log(tmp_path, "revised-one").exists()
    assert _file_at_head(repo, "one.txt", env) == "revised"


def test_uncommitted_work_is_committed_before_it_is_merged(
    tmp_path: Path, env: dict[str, str]
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    tasks: Tasks = {
        "one": {"files": {"one.txt": "work\n"}, "uncommitted": True},
    }

    assert _deliver(tmp_path, tasks, verify=False) == "delivered"

    assert _file_at_head(repo, "one.txt", env) == "work"
    assert _branches(repo, env) == ["main"]


def test_a_failing_task_stops_the_rest_and_removes_their_worktrees(
    tmp_path: Path, env: dict[str, str]
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    head = git_output(repo, ["rev-parse", "HEAD"], env)
    tasks: Tasks = {
        "bad": {"files": {}, "raises": 3, "meet": ["slow"]},
        "slow": {"files": {"slow.txt": "x\n"}, "meet": ["bad"], "hold": 30},
    }

    assert _deliver(tmp_path, tasks) == "raised"

    assert git_output(repo, ["rev-parse", "HEAD"], env) == head
    assert len(_worktrees(repo, env)) == 1
    assert _branches(repo, env) == ["main"]


def test_reviews_saved_in_a_worktree_reach_the_main_repository(
    tmp_path: Path, env: dict[str, str]
) -> None:
    repo = init_repo(tmp_path / "repo", env)
    commit_file(repo, env, name=".gitignore", content=".agent-files/\n")
    tasks: Tasks = {"one": {"files": {"one.txt": "x\n"}}}

    delivered = _deliver(tmp_path, tasks, verify=False, max_refine_steps=1, save_reviews=True)

    assert delivered == "delivered"
    assert len(list((repo / ".agent-files" / "reviews").glob("review-*.json"))) == 1


def test_a_non_positive_job_count_is_rejected_before_any_work(
    tmp_path: Path, env: dict[str, str]
) -> None:
    init_repo(tmp_path / "repo", env)
    tasks: Tasks = {"one": {"files": {"one.txt": "x\n"}}}

    assert _deliver(tmp_path, tasks, jobs=0) == "raised"

    assert not _log(tmp_path, "base-one").exists()
