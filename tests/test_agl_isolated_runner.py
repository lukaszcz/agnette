"""The isolated runner: fresh single-threaded subprocess, children's output, coverage data."""

from __future__ import annotations

import inspect
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import coverage
import pytest

from agnette.agl.self_validation import self_validation_enabled
from agnette.core import http as core_http
from agnette.util.text import format_description_column
from tests.agl.isolated_runner import run_callable, run_scenario

FORK_PROGRAM = Path(__file__).parent / "agl" / "programs" / "externs" / "isolated_fork.agl"


def _report_environment(workdir: Path) -> dict[str, Any]:
    return {
        "threads": len(sys._current_frames()),
        "cwd": os.getcwd(),
        "coverage": os.environ.get("COVERAGE_PROCESS_START"),
        "workdir": str(workdir),
        "self_validation": self_validation_enabled(),
        "sigint_default": signal.getsignal(signal.SIGINT) is signal.default_int_handler,
    }


def _fail(workdir: Path) -> None:
    raise AssertionError("boom from the subprocess")


def _skip(workdir: Path) -> None:
    pytest.skip("skipped in the subprocess")


def _forked_output(workdir: Path, word: str) -> int:
    """Print from the parent and from a forked child that bypasses interpreter teardown."""
    sys.stdout.write("parent says hi\n")
    pid = os.fork()
    if pid == 0:
        os.write(1, f"child says {word}\n".encode())
        os._exit(7)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def _fork_and_return_in_child(workdir: Path) -> str:
    """Fork a child that returns from the callable instead of exiting."""
    pid = os.fork()
    if pid == 0:
        return "child"
    os.waitpid(pid, 0)
    return "parent"


def _leave_a_grandchild(workdir: Path, own_group: bool = False) -> int:
    """Return the pid of a long-lived child, in the same process group or in one of its own."""
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        process_group=0 if own_group else None,
    )
    return sleeper.pid


def _try_real_externals(workdir: Path) -> dict[str, str]:
    """Attempt a real agent CLI, HTTP request, and `httpx2` send; report which were refused."""
    import httpx2

    results = {}
    with tempfile.TemporaryDirectory() as directory:
        cli = Path(directory) / "claude"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)
        try:
            subprocess.run([str(cli)], check=False)
            results["agent"] = "ran"
        except AssertionError:
            results["agent"] = "refused"
    try:
        core_http.open_session().get("http://example.invalid/")
        results["http"] = "sent"
    except AssertionError:
        results["http"] = "refused"
    try:
        httpx2.HTTPTransport().handle_request(httpx2.Request("GET", "http://example.invalid/"))
        results["httpx2"] = "sent"
    except pytest.fail.Exception:
        results["httpx2"] = "refused"
    return results


def _sleep_forever(workdir: Path) -> None:
    time.sleep(60)


def _body_lines(function: Any) -> set[int]:
    """Executable lines of *function*'s own body, excluding its `def` line."""
    code = function.__code__
    return {line for _, _, line in code.co_lines() if line and line != code.co_firstlineno}


def _is_gone(pid: int) -> bool:
    """Whether *pid* no longer runs (a zombie awaiting its reaper counts as gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    return stat.exists() and stat.read_text().rpartition(")")[2].split()[0] == "Z"


def test_lines_run_only_by_a_forked_child_are_recorded(tmp_path: Path) -> None:
    """The fixture's parent never calls the `src/` helper; only its forked child does."""
    data_dir = tmp_path / "cov"
    data_file = data_dir / ".coverage"
    run_scenario(FORK_PROGRAM, "forked_child_work", tmp_path, coverage_data=data_file)
    source = inspect.getsourcefile(format_description_column)
    assert source is not None
    body = _body_lines(format_description_column)

    assert len(list(data_dir.glob(".coverage.*"))) >= 2
    cov = coverage.Coverage(data_file=str(data_file), config_file=False)
    cov.combine()
    combined = cov.get_data().lines(source)
    assert combined is not None
    assert body & set(combined)
    assert not list(data_dir.glob(".coverage.*"))


def test_children_output_is_captured_at_fd_level(tmp_path: Path) -> None:
    result = run_callable(_forked_output, tmp_path, {"word": "hello"})
    assert result.value == 7
    assert result.stdout == "parent says hi\nchild says hello\n"


def test_a_forked_child_that_returns_does_not_overwrite_the_report(tmp_path: Path) -> None:
    assert run_callable(_fork_and_return_in_child, tmp_path).value == "parent"


def test_the_subprocess_runs_in_the_suites_environment(tmp_path: Path) -> None:
    value = run_callable(_report_environment, tmp_path).value
    assert value["threads"] == 1
    assert value["workdir"] == str(tmp_path)
    assert Path(value["cwd"]) == Path(__file__).parent.parent
    assert value["self_validation"]


def test_the_subprocess_restores_the_default_interrupt_handler(tmp_path: Path) -> None:
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        value = run_callable(_report_environment, tmp_path).value
    finally:
        signal.signal(signal.SIGINT, previous)
    assert value["sigint_default"]


def test_the_subprocess_refuses_real_agent_clis_and_network_requests(tmp_path: Path) -> None:
    refused = run_callable(_try_real_externals, tmp_path).value
    assert refused == {"agent": "refused", "http": "refused", "httpx2": "refused"}


def test_without_active_coverage_the_subprocess_is_not_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coverage.Coverage, "current", classmethod(lambda cls: None))
    monkeypatch.setenv("COVERAGE_PROCESS_START", "inherited.toml")
    assert run_callable(_report_environment, tmp_path).value["coverage"] is None


def test_active_coverage_routes_subprocess_data_to_its_data_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = coverage.Coverage(data_file=str(tmp_path / ".active"), config_file=False)
    monkeypatch.setattr(coverage.Coverage, "current", classmethod(lambda cls: active))
    assert run_callable(_report_environment, tmp_path).value["coverage"] is not None
    assert list(tmp_path.glob(".active.*"))


def test_a_relative_coverage_data_file_is_resolved_against_the_invocation_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pytestconfig: pytest.Config
) -> None:
    invocation_dir = pytestconfig.invocation_params.dir
    name = f".relative-{os.getpid()}"
    active = coverage.Coverage(data_file=name, config_file=False)
    monkeypatch.setattr(coverage.Coverage, "current", classmethod(lambda cls: active))
    monkeypatch.chdir(tmp_path)
    try:
        run_callable(_report_environment, tmp_path)
        written = list(invocation_dir.glob(f"{name}.*"))
        assert written
    finally:
        for path in invocation_dir.glob(f"{name}.*"):
            path.unlink()
    assert not list(tmp_path.glob(f"{name}.*"))


def test_a_failure_in_the_subprocess_fails_the_caller_with_its_traceback(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="boom from the subprocess"):
        run_callable(_fail, tmp_path)


def test_a_failing_scenario_reports_the_actual_output(tmp_path: Path) -> None:
    program = tmp_path / "failing.agl"
    program.write_text('program def main() -> unit =\n  print "the actual output"\n')
    sidecar = {"scenarios": [{"name": "mismatch", "expect": {"stdout": "something else\n"}}]}
    program.with_name("failing.scenarios.json").write_text(json.dumps(sidecar))
    with pytest.raises(pytest.fail.Exception, match="the actual output"):
        run_scenario(program, "mismatch", tmp_path)


def test_a_skip_in_the_subprocess_skips_the_caller(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception, match="skipped in the subprocess"):
        run_callable(_skip, tmp_path)


def test_a_subprocess_that_overruns_its_timeout_fails_the_caller(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="timed out"):
        run_callable(_sleep_forever, tmp_path, timeout=0.5)


@pytest.mark.parametrize("own_group", [False, True])
def test_children_left_in_the_session_are_killed_when_the_run_ends(
    tmp_path: Path, own_group: bool
) -> None:
    pid = run_callable(_leave_a_grandchild, tmp_path, {"own_group": own_group}).value
    deadline = time.monotonic() + 10
    while not _is_gone(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _is_gone(pid)
