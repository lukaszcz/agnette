"""Run AgL corpus scenarios and Python callables in a fresh single-threaded subprocess.

The suite's own processes cannot fork safely: xdist workers carry a second Python thread
state. The subprocess (``python -m tests.agl.isolated_runner``) has none. It reports its
outcome through a file; fd 1 is redirected to a file, so output written by forked children
is captured too. The subprocess leads a session of its own; every process left in that
session when the run ends is killed, including children that moved to groups of their own
(``setpgid``), so none outlives the run.

Under active coverage the subprocess measures itself and its forked children through
``tests/isolated_coverage.toml``. The data files are named after the suite's ``.coverage``,
so pytest-cov combines them into the final report.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import operator
import os
import signal
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COVERAGE_CONFIG = REPO_ROOT / "tests" / "isolated_coverage.toml"
COVERAGE_DATA_VARIABLE = "AGM_ISOLATED_COVERAGE_DATA"
DEFAULT_TIMEOUT = 120.0

_session: tuple[Path, Path] | None = None


def bind_session(invocation_dir: Path | None, temp_root: Path | None) -> None:
    """Record pytest's invocation directory and base temporary directory (``None``: unbind).

    The invocation directory resolves coverage's relative data file; the temporary root lets
    the subprocess apply the suite's in-process guards (``tests/_isolation.py``).
    """
    global _session
    _session = None if invocation_dir is None or temp_root is None else (invocation_dir, temp_root)


@dataclass(frozen=True)
class IsolatedResult:
    """A passed isolated run: all of its fd-level stdout and a return value."""

    stdout: str
    value: Any


def run_scenario(
    program: Path,
    name: str,
    workdir: Path,
    *,
    coverage_data: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> IsolatedResult:
    """Run the corpus scenario *name* of *program* as ``test_program_scenarios`` would.

    Fails the calling test with the subprocess's traceback if the scenario fails.
    """
    request = {"kind": "scenario", "program": str(program), "name": name}
    return _run(request, workdir, coverage_data, timeout)


def run_callable(
    function: Callable[..., Any],
    workdir: Path,
    args: Mapping[str, Any] | None = None,
    *,
    coverage_data: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> IsolatedResult:
    """Call the module-level *function*\\ ``(workdir, **args)`` in the subprocess.

    *args* and the return value must be JSON-serializable. The function may raise or skip.
    """
    target = f"{function.__module__}:{function.__qualname__}"
    request = {"kind": "callable", "target": target, "args": dict(args or {})}
    return _run(request, workdir, coverage_data, timeout)


def _coverage_environment(invocation_dir: Path, coverage_data: Path | None) -> dict[str, str]:
    """Environment starting coverage in a subprocess, or empty when nothing is measured.

    Data goes to *coverage_data*, else to the data file of the coverage active in this process,
    a relative one resolved against *invocation_dir*.
    """
    import coverage

    if coverage_data is None:
        active = coverage.Coverage.current()
        if active is None:
            return {}
        coverage_data = invocation_dir / active.config.data_file
    return {
        "COVERAGE_PROCESS_START": str(COVERAGE_CONFIG),
        COVERAGE_DATA_VARIABLE: str(coverage_data),
    }


def _live_session_members(sid: int) -> list[int]:
    """The pids of the live (non-zombie) processes in session *sid*, from ``/proc``."""
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # Fields after the parenthesized command: state, ppid, pgrp, session, ...
        state, _, _, session = stat.rpartition(")")[2].split()[:4]
        if int(session) == sid and state != "Z":
            members.append(int(entry.name))
    return members


def _kill_session(sid: int) -> None:
    """SIGKILL every process left in session *sid* until none is alive."""
    while members := _live_session_members(sid):
        for pid in members:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def _run(
    request: dict[str, Any], workdir: Path, coverage_data: Path | None, timeout: float
) -> IsolatedResult:
    import pytest

    assert _session is not None, "the isolated runner needs a pytest session"
    invocation_dir, temp_root = _session
    environment = {k: v for k, v in os.environ.items() if k != "COVERAGE_PROCESS_START"}
    environment.update(_coverage_environment(invocation_dir, coverage_data))
    with tempfile.TemporaryDirectory(prefix="isolated-") as scratch_name:
        scratch = Path(scratch_name)
        full_request = {**request, "workdir": str(workdir), "temp_root": str(temp_root)}
        (scratch / "request.json").write_text(json.dumps(full_request))
        with (scratch / "stderr.txt").open("wb") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-m", __name__, str(scratch)],
                cwd=REPO_ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                start_new_session=True,
            )
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                _kill_session(process.pid)
            if timed_out:
                pytest.fail(f"isolated run timed out after {timeout}s", pytrace=False)
        stderr_text = (scratch / "stderr.txt").read_text(errors="replace")
        report_path = scratch / "report.json"
        if not report_path.exists():
            pytest.fail(
                f"isolated run exited {process.returncode} without a report:\n{stderr_text}",
                pytrace=False,
            )
        report = json.loads(report_path.read_text())
        stdout = (scratch / "stdout.txt").read_text(errors="replace")
    if report["outcome"] == "skipped":
        pytest.skip(report["detail"])
    if report["outcome"] == "failed":
        pytest.fail(f"{report['detail']}\n{stderr_text}", pytrace=False)
    return IsolatedResult(stdout=stdout, value=report["value"])


class _FdCapture:
    """Redirect fd 1 to a file, so forked children's output lands there too.

    Stdout is line-buffered: output is in order with children's, and a fork duplicates no
    complete line.
    """

    def __init__(self, path: Path) -> None:
        sys.stdout.reconfigure(line_buffering=True)
        writer = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.dup2(writer, 1)
        os.close(writer)
        self._reader = path.open("rb")

    def read(self) -> str:
        """Everything written to stdout since the previous call."""
        sys.stdout.flush()
        return self._reader.read().decode(errors="replace")


def _execute(request: dict[str, Any], capture: _FdCapture, patch: pytest.MonkeyPatch) -> Any:
    workdir = Path(request["workdir"])
    if request["kind"] == "scenario":
        from tests.agl.scenario_harness import assert_program_scenario, scenario_named

        program = Path(request["program"])
        scenario = scenario_named(program, request["name"])
        assert_program_scenario(program, scenario, capture.read, workdir, patch, None)
        return None
    module_name, _, qualname = request["target"].partition(":")
    function = operator.attrgetter(qualname)(importlib.import_module(module_name))
    return json.loads(json.dumps(function(workdir, **request["args"])))


def _main(scratch: Path) -> None:
    import coverage

    coverage.process_startup()
    import pytest

    from agnette.agl.self_validation import set_self_validation_enabled
    from tests._isolation import install_test_guards

    request = json.loads((scratch / "request.json").read_text())
    capture = _FdCapture(scratch / "stdout.txt")
    # A launcher may have set SIGINT to ignored; make interrupts raise as in the suite.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    set_self_validation_enabled(True)
    started_pid = os.getpid()
    outcome, detail, value = "passed", "", None
    with pytest.MonkeyPatch.context() as patch:
        install_test_guards(patch, Path(request["temp_root"]))
        try:
            value = _execute(request, capture, patch)
        except pytest.skip.Exception as skipped:
            outcome, detail = "skipped", str(skipped)
        except BaseException:
            outcome, detail = "failed", traceback.format_exc()
    if os.getpid() != started_pid:
        # A forked child that returned instead of exiting must not write the parent's report.
        os._exit(1)
    sys.stdout.flush()
    report = {"outcome": outcome, "detail": detail, "value": value}
    (scratch / "report.json").write_text(json.dumps(report))


if __name__ == "__main__":
    _main(Path(sys.argv[1]))
