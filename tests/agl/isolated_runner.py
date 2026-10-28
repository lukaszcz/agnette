"""Run AgL corpus scenarios and Python callables in a fresh single-threaded process.

The suite's own processes cannot fork safely: xdist workers carry a second Python thread
state. The isolated process has none. It reports its outcome through a file; fd 1 is
redirected to a file, so output written by forked children is captured too, and a scenario
reads what was written to fd 2 (the run's stderr file) through :class:`StderrTail`. It leads a
session of its own; every process left in that session when the run ends is killed,
including children that moved to groups of their own (``setpgid``), so none outlives the run.

A run is a child of a per-worker *zygote* (``tests/_zygote.py``) that has already imported
the harness and compiled the standard library in memory, or, when no zygote may serve it, a
cold ``python -m tests.agl.isolated_runner`` subprocess. The zygote never runs a scenario, so
a child inherits only the zygote's imports, the immutable compile caches it filled, its gc
state, its hash seed and its interpreter-start environment. A cold interpreter is not proven
able to import in the order a scenario needs, nor a scenario to survive a hash seed of its
own; ``AGM_TEST_NO_ZYGOTE`` runs every request cold, which is how the suite settles either
question. A request also runs cold when its coverage or interpreter-start environment
(``PYTHON*``, ``LANG``, ``LC_*``, ``TZ``) differs from the zygote's, when the zygote cannot be
reached, or when it failed to start (the session then stops using one and warns, quoting the
zygote's stderr).

Under active coverage the isolated process measures itself and its forked children through
``tests/isolated_coverage.toml``. The zygote measures nothing; each child starts measurement
right after the fork, as a cold subprocess does at startup, so the data holds exactly what
the child ran. The data files are named after the suite's ``.coverage``, so pytest-cov
combines them into the final report.
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
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, TextIO

from tests import _zygote

if TYPE_CHECKING:
    import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COVERAGE_CONFIG = REPO_ROOT / "tests" / "isolated_coverage.toml"
COVERAGE_DATA_VARIABLE = "AGM_ISOLATED_COVERAGE_DATA"
DEFAULT_TIMEOUT = 120.0

# How long a new zygote may take to import, compile and start listening.
_ZYGOTE_STARTUP_TIMEOUT = 120.0

_session: tuple[Path, Path] | None = None

# The coverage environment of the session's zygote: that of the suite's own coverage when
# bound, which a run's environment must equal for the zygote to serve it.
_session_coverage: dict[str, str] = {}

# Whether runs may use the zygote (``AGM_TEST_NO_ZYGOTE`` at bind time says not, and a zygote
# that failed to start ends it for the session), and the worker's zygote once a run has
# started it, with the interpreter-start environment it was started under.
_zygote_enabled = False
_warm: _zygote.Launcher | None = None
_warm_interpreter: dict[str, str] = {}


def bind_session(invocation_dir: Path | None, temp_root: Path | None) -> None:
    """Record pytest's invocation directory and base temporary directory (``None``: unbind).

    The invocation directory resolves coverage's relative data file; the temporary root lets
    the subprocess apply the suite's in-process guards (``tests/_isolation.py``). Binding
    fixes the coverage the worker's zygote runs under and whether it may be used at all;
    unbinding stops it.
    """
    global _session, _session_coverage, _zygote_enabled
    _stop_warm()
    if invocation_dir is None or temp_root is None:
        _session = None
        return
    _session = (invocation_dir, temp_root)
    _session_coverage = _coverage_environment(invocation_dir, None)
    _zygote_enabled = not os.environ.get(_zygote.DISABLE_ENV)


def _stop_warm() -> None:
    """Stop the worker's zygote, if it has one."""
    global _warm
    if _warm is not None:
        _warm.close()
        _warm = None


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


def _kill_session(sid: int) -> None:
    """SIGKILL every process left in session *sid* until none is alive."""
    while members := [pid for pid, _, session in _zygote.live_processes() if session == sid]:
        for pid in members:
            _zygote.kill_process(pid)


def _kill_child(pid: int) -> None:
    """Kill the zygote's child *pid* and everything in its session.

    The zygote reports *pid* right after the fork, before the child's ``setsid``. Until
    then the session is empty, so the child itself is killed first; the zygote does not reap
    it before this, so *pid* cannot have been reused.
    """
    _zygote.kill_process(pid)
    _kill_session(pid)


@dataclass(frozen=True)
class _Exit:
    """How an isolated process ended: its exit code, and whether the run overran its timeout."""

    code: int
    timed_out: bool


def _run(
    request: dict[str, Any], workdir: Path, coverage_data: Path | None, timeout: float
) -> IsolatedResult:
    import pytest

    assert _session is not None, "the isolated runner needs a pytest session"
    invocation_dir, temp_root = _session
    environment = {k: v for k, v in os.environ.items() if k != "COVERAGE_PROCESS_START"}
    coverage_environment = _coverage_environment(invocation_dir, coverage_data)
    environment.update(coverage_environment)
    with tempfile.TemporaryDirectory(prefix="isolated-") as scratch_name:
        scratch = Path(scratch_name)
        full_request = {**request, "workdir": str(workdir), "temp_root": str(temp_root)}
        (scratch / "request.json").write_text(json.dumps(full_request))
        ended = _launch_warm(scratch, environment, coverage_environment, timeout)
        if ended is None:
            ended = _launch_cold(scratch, environment, timeout)
        if ended.timed_out:
            pytest.fail(f"isolated run timed out after {timeout}s", pytrace=False)
        stderr_text = (scratch / "stderr.txt").read_text(errors="replace")
        report_path = scratch / "report.json"
        if not report_path.exists():
            pytest.fail(
                f"isolated run exited {ended.code} without a report:\n{stderr_text}",
                pytrace=False,
            )
        report = json.loads(report_path.read_text())
        stdout = (scratch / "stdout.txt").read_text(errors="replace")
    if report["outcome"] == "skipped":
        pytest.skip(report["detail"])
    if report["outcome"] == "failed":
        pytest.fail(f"{report['detail']}\n{stderr_text}", pytrace=False)
    return IsolatedResult(stdout=stdout, value=report["value"])


def _launch_cold(scratch: Path, environment: dict[str, str], timeout: float) -> _Exit:
    """Run the request in *scratch* in a new interpreter and kill what it leaves behind."""
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
    return _Exit(process.returncode, timed_out)


def _interpreter_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """The part of *environment* that an interpreter reads once, at its start."""
    return {
        name: value
        for name, value in environment.items()
        if name.startswith(("PYTHON", "LC_")) or name in ("LANG", "LANGUAGE", "TZ")
    }


def _warm_launcher(
    environment: dict[str, str], coverage_environment: dict[str, str]
) -> _zygote.Launcher | None:
    """The worker's zygote if it may serve a run, started or restarted as needed.

    A zygote that fails to start ends zygote use for the session, with a warning.
    """
    global _warm, _warm_interpreter, _zygote_enabled
    if not _zygote_enabled or coverage_environment != _session_coverage:
        return None
    interpreter = _interpreter_environment(environment)
    if _warm is not None and not _warm.alive:
        _stop_warm()
    if _warm is None:
        assert _session is not None
        name = _zygote.unique_name("isolated-zygote")
        # The zygote measures nothing: each child starts coverage itself.
        zygote_environment = {
            k: v
            for k, v in environment.items()
            if k not in ("COVERAGE_PROCESS_START", COVERAGE_DATA_VARIABLE)
        }
        try:
            _warm = _zygote.start(
                _zygote.Launcher,
                [sys.executable, "-m", __name__, ZYGOTE_FLAG],
                name=name,
                env=zygote_environment,
                log=_session[1] / f"{name}.log",
                cwd=str(REPO_ROOT),
                startup_timeout=_ZYGOTE_STARTUP_TIMEOUT,
            )
        except _zygote.LauncherError as error:
            _zygote_enabled = False
            warnings.warn(f"isolated runs stop using a zygote and run cold: {error}", stacklevel=2)
            return None
        _warm_interpreter = interpreter
    elif interpreter != _warm_interpreter:
        return None
    return _warm


def _launch_warm(
    scratch: Path,
    environment: dict[str, str],
    coverage_environment: dict[str, str],
    timeout: float,
) -> _Exit | None:
    """Run the request in *scratch* in a child of the zygote and kill what it leaves behind.

    Returns ``None`` when the zygote cannot serve the run, which then needs a cold process:
    it may not be used, or could not be started, or the request never reached it. A zygote
    lost once the request was sent fails the run instead -- the request may have run, and
    must not run again -- and the next run starts a new zygote.
    """
    import pytest

    warm = _warm_launcher(environment, coverage_environment)
    if warm is None:
        return None
    with (scratch / "stderr.txt").open("wb") as stderr:
        try:
            reply = warm.invoke(
                (str(scratch), environment),
                [stderr.fileno()],
                timeout=timeout,
                kill=_kill_child,
            )
        except _zygote.LauncherError as lost:
            _stop_warm()
            if not lost.sent:
                return None
            pytest.fail(f"the isolated-run zygote failed: {lost}", pytrace=False)
    _kill_session(reply.pid)
    return _Exit(os.waitstatus_to_exitcode(reply.status), reply.timed_out)


class _FileTail:
    """Read what a standard stream's file gained since the previous read."""

    def __init__(self, path: Path, stream: TextIO, *, skip_existing: bool) -> None:
        self._stream = stream
        self._reader = path.open("rb")
        if skip_existing:
            self._reader.seek(0, os.SEEK_END)

    def read(self) -> str:
        """Everything written to the stream since the previous call."""
        self._stream.flush()
        return self._reader.read().decode(errors="replace")


class _FdCapture(_FileTail):
    """Redirect fd 1 to a file, so forked children's output lands there too.

    Stdout is line-buffered: output is in order with children's, and a fork duplicates no
    complete line.
    """

    def __init__(self, path: Path) -> None:
        sys.stdout.reconfigure(line_buffering=True)
        writer = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.dup2(writer, 1)
        os.close(writer)
        super().__init__(path, sys.stdout, skip_existing=False)


class StderrTail(_FileTail):
    """Read what the process and its children write to fd 2, the run's stderr file.

    Skips output written before construction.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path, sys.stderr, skip_existing=True)


def _execute(
    request: dict[str, Any], capture: _FdCapture, stderr: StderrTail, patch: pytest.MonkeyPatch
) -> Any:
    workdir = Path(request["workdir"])
    if request["kind"] == "scenario":
        from tests.agl.scenario_harness import assert_program_scenario, scenario_named

        program = Path(request["program"])
        scenario = scenario_named(program, request["name"])
        assert_program_scenario(program, scenario, capture.read, workdir, patch, None, stderr.read)
        return None
    module_name, _, qualname = request["target"].partition(":")
    function = operator.attrgetter(qualname)(importlib.import_module(module_name))
    return json.loads(json.dumps(function(workdir, **request["args"])))


def _run_request(scratch: Path) -> None:
    """Run the request in *scratch* in this process and write its report."""
    import pytest

    from agnette.agl.self_validation import set_self_validation_enabled
    from tests._isolation import install_test_guards

    request = json.loads((scratch / "request.json").read_text())
    capture = _FdCapture(scratch / "stdout.txt")
    stderr = StderrTail(scratch / "stderr.txt")
    # A launcher may have set SIGINT to ignored; make interrupts raise as in the suite.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    set_self_validation_enabled(True)
    started_pid = os.getpid()
    outcome, detail, value = "passed", "", None
    with pytest.MonkeyPatch.context() as patch:
        install_test_guards(patch, Path(request["temp_root"]))
        try:
            value = _execute(request, capture, stderr, patch)
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


def _main(scratch: Path) -> None:
    """Entry point of a cold subprocess."""
    import coverage

    coverage.process_startup()
    _run_request(scratch)


# ---------------------------------------------------------------------------
# Zygote
# ---------------------------------------------------------------------------

# Argument that makes ``python -m tests.agl.isolated_runner`` serve as a zygote.
ZYGOTE_FLAG = "--zygote"

# What a run imports on its way to a scenario or callable, beyond the AgL stack that
# ``PipelineDriver`` brings along.
_PRELOADED = (
    "coverage",
    "pytest",
    "jsonschema",
    "agnette.config.module_roots",
    "agnette.packages.development",
    "agnette.packages.stdlib",
    "tests._isolation",
    "tests.agl.scenario_harness",
)


def _preload() -> None:
    """Import what a run needs and compile the standard library, so a child does neither.

    The standard library is compiled in memory only: parsing, resolution and type checking
    of every ``std`` module fill the process-global parsed-module and artifact caches, with
    self-validation on, as in a run: a compilation that reuses a cached module trusts that it
    was validated. The caches are immutable and keyed by module identity and source, and hold
    no runtime state (``agnette/agl/artifact_cache.py``). Lowering fills the immutable
    IR cache too; nothing is run. Coverage source maps contain no measured arcs.
    """
    from agnette.agl.self_validation import set_self_validation_enabled

    set_self_validation_enabled(True)
    for name in _PRELOADED:
        importlib.import_module(name)
    _zygote.preload_stdlib()
    _zygote.preload_coverage()


def _child(request: tuple[str, dict[str, str]], fds: list[int]) -> NoReturn:
    """Become the isolated process of *request*: own session, stderr on ``fds[0]``."""
    scratch, environment = request

    def body() -> int:
        import coverage

        os.setsid()
        os.dup2(fds[0], 2)
        os.close(fds[0])
        _zygote.adopt(environment, REPO_ROOT)
        coverage.process_startup()
        _run_request(Path(scratch))
        return 0

    _zygote.run_child(body)


if __name__ == "__main__":
    if sys.argv[1] == ZYGOTE_FLAG:
        _zygote.serve(sys.argv[2], _preload, _child)
    else:
        _main(Path(sys.argv[1]))
