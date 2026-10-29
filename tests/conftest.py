"""Shared test fixtures."""

from __future__ import annotations

import gc
import os
import shutil
import signal
import tempfile
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from xdist.workermanage import WorkerController

from agnette.agl.self_validation import self_validation_enabled, set_self_validation_enabled
from agnette.core import dry_run, process
from agnette.core.process import CapturedOutput
from agnette.packages import python_deps
from tests import _command_coverage, _coverage_metadata, _isolation, _zygote
from tests._durations import (
    pytest_runtest_protocol,
    pytest_sessionfinish,
    pytest_testnodedown,
)
from tests._package_helpers import PythonInstaller
from tests.agl import isolated_runner

# Re-exported so pytest picks the per-test cost accounting up as conftest hooks.
# Registering the module with ``-p`` instead would break every invocation that
# does not put the repository root on ``sys.path`` (plain ``uv run pytest``).
__all__ = ["pytest_runtest_protocol", "pytest_sessionfinish", "pytest_testnodedown"]

_GC_THRESHOLDS = pytest.StashKey[tuple[int, int, int]]()
_GC_COLLECTIONS = pytest.StashKey[int]()
_COVERAGE_METADATA = pytest.StashKey[Path]()

# Enable the optional invariant self-checks — match-compilation self-checks, IR
# structural validation, and completion's re-raising of the failures it would
# degrade — for the whole test suite.  They are disabled in
# normal execution (zero production cost); turning them on here makes every case
# compiled and every program lowered anywhere in the suite double as an invariant
# oracle.  Individual tests may disable them to exercise the production path (see
# the ``self_validation_disabled`` fixture).
set_self_validation_enabled(True)


def _detach_from_controlling_terminal() -> None:
    """Put this process in a new session so it has no controlling terminal.

    Tests spawn many external processes — interactive shells through the
    workspace-shell wrapper (``bash -i`` / ``zsh -i``), the fake ``tmux`` shell
    script, runner/selector scripts, ``git``.  Each of these opens ``/dev/tty``
    on startup.  When the suite runs in a real terminal and one of those
    processes reads the controlling terminal while *not* in the foreground
    process group, the kernel raises ``SIGTTIN`` and suspends the whole
    ``just check`` job — the intermittent ``zsh: suspended (tty input)`` hang.

    ``os.setsid`` drops the controlling terminal entirely, so ``/dev/tty`` opens
    fail with ``ENXIO`` (exactly as under CI / an agent harness) and ``SIGTTIN``
    can never fire.  This makes the suite behave identically regardless of how
    it is launched and removes a whole class of concurrency-sensitive hangs.

    ``setsid`` fails for a process-group leader; the project always runs tests
    via ``uv run`` (so pytest is a child, never the leader), but we degrade
    gracefully if that ever changes.
    """
    try:
        os.setsid()
    except OSError:
        # Already a session/group leader — leave the disposition unchanged.
        return


def pytest_configure(config: pytest.Config) -> None:
    """Detach test-running processes from the controlling terminal.

    Runs once per process.  Under ``-n auto`` only the xdist *workers* execute
    tests, so the controller stays attached (it owns terminal reporting); each
    worker detaches.  Without xdist the single process runs tests and detaches.

    Also registers the e2e command-coverage gate as a plugin in its own right.
    It declares ``pytest_sessionfinish``/``pytest_testnodedown`` hooks of its
    own, which a conftest re-export (the mechanism used above for the duration
    hooks) cannot express twice under one name.
    """
    config.pluginmanager.register(_command_coverage, "agm_command_coverage")
    worker_input = getattr(config, "workerinput", {})
    if "agm_coverage_metadata" in worker_input:
        _coverage_metadata.configure(Path(worker_input["agm_coverage_metadata"]))
    _zygote.preload_coverage(warm=False)
    # Collect cyclic compiler state at bounded test boundaries, not during allocations.
    thresholds = gc.get_threshold()
    config.stash[_GC_THRESHOLDS] = thresholds
    config.stash[_GC_COLLECTIONS] = 0
    gc.set_threshold(0)
    config.add_cleanup(lambda: gc.set_threshold(*thresholds))
    is_xdist_worker = hasattr(config, "workerinput")
    xdist_active = getattr(config.option, "dist", "no") != "no"
    if is_xdist_worker or not xdist_active:
        _detach_from_controlling_terminal()


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: WorkerController) -> None:
    """Build static analysis once; measured coverage remains process-local."""
    config = node.config
    if os.environ.get("COVERAGE_CORE") != "sysmon" or config.getoption("no_cov", default=False):
        return
    if _COVERAGE_METADATA not in config.stash:
        directory = tempfile.TemporaryDirectory(prefix="agm-coverage-metadata-")
        config.add_cleanup(directory.cleanup)
        config.stash[_COVERAGE_METADATA] = _coverage_metadata.create(Path(directory.name))
    node.workerinput["agm_coverage_metadata"] = str(config.stash[_COVERAGE_METADATA])


def pytest_runtest_setup(item: pytest.Item) -> None:
    # Allocation pressure varies greatly between lexer cases and REPL histories.
    # Collect only at test boundaries, before cyclic compiler graphs accumulate.
    if gc.get_count()[0] < 25_000:
        return
    count = item.config.stash[_GC_COLLECTIONS] + 1
    gc.collect(2 if count == 30 else 0)
    item.config.stash[_GC_COLLECTIONS] = 0 if count == 30 else count


@pytest.fixture(scope="session", autouse=True)
def compiler_gc_policy(
    pytestconfig: pytest.Config, isolated_compiler_cache: None
) -> Generator[None, None, None]:
    """Avoid repeatedly scanning collection metadata during compiler-heavy tests."""
    if hasattr(pytestconfig, "workerinput"):
        _zygote.preload_stdlib()
    gc.collect()
    gc.freeze()
    try:
        yield
    finally:
        from agnette.agl.artifact_cache import clear_retained_artifacts
        from agnette.agl.modules.parsed_module_cache import clear_parsed_module_cache
        from agnette.agl.repl import session as repl_session_module

        clear_retained_artifacts()
        clear_parsed_module_cache()
        repl_session_module._bootstrap_cache.clear()
        gc.unfreeze()
        gc.set_threshold(*pytestconfig.stash[_GC_THRESHOLDS])


@pytest.fixture(scope="session", autouse=True)
def isolated_compiler_cache(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[None, None, None]:
    """Keep compiler artifacts and inherited child-process caches disposable."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("compiler-cache")))
        yield


@dataclass(frozen=True, slots=True)
class InstallerGuard:
    """The stub installers every test sees on ``PATH`` unless it fakes one itself."""

    bin_dir: Path
    log: Path


_GUARDED_INSTALLERS = ("uv", "pip", "pip3")


@pytest.fixture(scope="session", autouse=True)
def installer_guard(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[InstallerGuard, None, None]:
    """Make every real Python installer unreachable for the whole suite.

    Stub ``uv``/``pip``/``pip3`` at the front of ``PATH`` log their argv and exit
    97; the offline settings also stop ``python -m pip`` and any ``uv`` found by
    other means. The patch is this fixture's own, so a test's ``monkeypatch``
    cannot undo it; a fake a test installs is prepended later and wins.
    Subprocesses inherit all of it.
    """
    bin_dir = tmp_path_factory.mktemp("installer-guard")
    log = bin_dir / "calls.log"
    log.touch()
    for name in _GUARDED_INSTALLERS:
        stub = bin_dir / name
        stub.write_text(
            f'#!/bin/sh\necho "{name} $*" >> "{log}"\n'
            'echo "real installer blocked in tests" >&2\nexit 97\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    patch = pytest.MonkeyPatch()
    patch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    patch.setenv("UV_OFFLINE", "1")
    patch.setenv("UV_NO_CONFIG", "1")
    patch.setenv("PIP_NO_INDEX", "1")
    patch.setenv("PIP_CONFIG_FILE", os.devnull)
    try:
        yield InstallerGuard(bin_dir, log)
    finally:
        patch.undo()


@pytest.fixture(autouse=True)
def fresh_python_overlay_state(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Give every test its own process-wide overlay state, releasing the locks it took."""
    held: dict[Path, int] = {}
    monkeypatch.setattr(python_deps, "_fixed", {})
    monkeypatch.setattr(python_deps, "_stale", {})
    monkeypatch.setattr(python_deps, "_held", held)
    yield
    for fd in held.values():
        os.close(fd)


@pytest.fixture(autouse=True)
def fail_on_blocked_installer(installer_guard: InstallerGuard) -> Generator[None, None, None]:
    """Fail a test during which a real installer was invoked and blocked."""
    before = installer_guard.log.stat().st_size
    yield
    assert installer_guard.log.stat().st_size <= before, (
        "test invoked a real installer:\n" + installer_guard.log.read_text(encoding="utf-8")
    )


@pytest.fixture(scope="session", autouse=True)
def shared_qualifier_modules(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[None, None, None]:
    """Reuse immutable qualifier libraries while keeping every REPL session fresh."""
    from tests.agl import qualifier_support

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            qualifier_support, "_MODULES_ROOT", tmp_path_factory.mktemp("qualifier-modules")
        )
        yield
    qualifier_support._MODULE_ROOTS.clear()


@pytest.fixture(scope="session", autouse=True)
def isolated_runner_session(
    pytestconfig: pytest.Config, tmp_path_factory: pytest.TempPathFactory
) -> Generator[None, None, None]:
    """Tell the isolated runner where pytest was invoked and where its temp root is."""
    isolated_runner.bind_session(
        pytestconfig.invocation_params.dir, tmp_path_factory.getbasetemp().resolve()
    )
    yield
    isolated_runner.bind_session(None, None)


@pytest.fixture()
def self_validation_disabled() -> Generator[None, None, None]:
    """Run the body with the optional self-checks off, as in normal execution.

    The suite enables them globally; tests that pin the production path — where a
    compile or lowering is trusted without being re-verified, or a completer
    degrades a failure to no suggestions — take this fixture.
    """
    previous = self_validation_enabled()
    set_self_validation_enabled(False)
    try:
        yield
    finally:
        set_self_validation_enabled(previous)


@pytest.fixture()
def default_sigint() -> Generator[None, None, None]:
    """Ensure SIGINT uses Python's default handler for the duration of a test.

    When the test suite is launched as a background process (e.g. by a CI runner
    or agent harness, or with a trailing ``&``), the shell sets SIGINT to
    ``SIG_IGN`` and child processes — pytest-xdist workers and any subprocess
    they spawn — inherit it.  Interrupt tests must restore the default
    disposition so SIGINT is actually delivered as ``KeyboardInterrupt`` and so
    spawned ``agm`` processes reset to the default handler on ``exec`` instead of
    silently ignoring the signal.
    """

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.fixture(autouse=True)
def refuse_real_external_agent_clis(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Refuse every in-process spawn of an external agent CLI that no test faked."""
    _isolation.refuse_real_external_agent_clis(monkeypatch, tmp_path_factory.getbasetemp())


@pytest.fixture(autouse=True)
def reset_dry_run_state() -> Generator[None, None, None]:
    dry_run.set_enabled(False)
    yield
    dry_run.set_enabled(False)


@pytest.fixture(autouse=True)
def clear_project_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROJ_DIR", raising=False)
    monkeypatch.delenv("REPO_DIR", raising=False)


@pytest.fixture(autouse=True)
def clear_workspace_shell_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip agm workspace-shell control variables inherited from the host.

    When the suite is run from inside an agm workspace shell, the process
    inherits ``AGM_REAL_SHELL``/``AGM_WORKSPACE_SHELL`` and friends.  Any test
    that spawns the shell wrapper with ``{**os.environ}`` would then pick up the
    host's real shell instead of the one it set up, so the wrapper would launch
    e.g. zsh in place of the requested bash.  Remove them so tests behave the
    same whether or not the suite itself runs inside a workspace shell.
    """

    for name in list(os.environ):
        if name.startswith("AGM_"):
            monkeypatch.delenv(name, raising=False)


#: The environment the suite was launched with, captured before any fixture
#: rewrites it.  Only the isolation tests read it — to prove the suite no longer
#: runs against it.
LAUNCH_ENVIRONMENT: Mapping[str, str] = MappingProxyType(dict(os.environ))

#: Git identity for every test, so committing never needs a personal
#: ``~/.gitconfig``.  ``GIT_CONFIG_NOSYSTEM`` drops the machine-wide file too.
GIT_IDENTITY: Mapping[str, str] = MappingProxyType(
    {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
)

#: Variables that tell AgM which project and terminal it is running inside.  A
#: suite launched from an agm workspace inherits them and would otherwise
#: resolve *that* project instead of the one the test built.
_HOST_CONTEXT_VARIABLES = ("PROJ_DIR", "REPO_DIR", "TMUX", "TMUX_PANE")


def _git_ceiling_directories(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Pytest's shared temp root itself, so git discovery can never climb past it.

    A tmp root nested under, or coinciding with, a git-tracked directory (e.g. a
    sandboxed ``TMPDIR`` under the invoking user's home) would otherwise let git,
    walking upward from a test's own repository, discover that outer one instead.
    The ceiling directory itself is excluded from discovery, so it must be the
    root a test's tree hangs from, not that root's parent, or a repository at the
    root itself would still be found.
    """
    return str(tmp_path_factory.getbasetemp().resolve())


@pytest.fixture(autouse=True)
def isolate_host_environment(
    clear_workspace_shell_env: None,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run every test against its own empty home, never the developer's.

    ``HOME`` resolves the AgM home (``~/.agm``), so without this a developer who
    has run ``agm pkg install`` makes the suite read their installed package
    store and their ``config.toml`` — the suite would then pass
    or fail depending on the machine.  Point ``HOME`` (and the XDG roots git
    consults) at a per-test directory, supply git identity explicitly so an
    empty home can still commit, and drop the project/terminal variables an agm
    workspace shell exports, drop every inherited ``GIT_*`` variable (``GIT_DIR``,
    ``GIT_CONFIG_*``, ...) that would redirect or reconfigure git, and drop inherited
    proxy settings so HTTP clients can initialize before their fake transport takes over.
    Drop shell startup overrides so subprocesses cannot source the host's rc files.

    Tests that need a populated home build one and pass it explicitly, as
    ``home=`` or in an ``env`` mapping.  The home lives outside the test's own
    ``tmp_path`` so that a test inspecting that directory sees only its own
    files.
    """
    home = Path(tempfile.mkdtemp(prefix="isolated-home-", dir=tmp_path_factory.getbasetemp()))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    for name, value in GIT_IDENTITY.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", _git_ceiling_directories(tmp_path_factory))
    for name in (*_HOST_CONTEXT_VARIABLES, "BASH_ENV", "ENV", "ZDOTDIR"):
        monkeypatch.delenv(name, raising=False)
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def fence_project_discovery_at_temp_root(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop ancestor-walking project discovery at the suite's temporary root."""
    _isolation.fence_project_discovery_at_temp_root(monkeypatch, tmp_path_factory.getbasetemp())


@pytest.fixture(autouse=True)
def start_outside_any_project(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start every test in a private empty directory, never in the launching one.

    Project discovery, configuration and package-store resolution default to the
    process working directory. Launched from a checkout inside a real AgM
    project, that would make every test that resolves a context read the host
    project's configuration and package store. The directory lives outside the
    test's own ``tmp_path`` so that a test listing that directory sees only its
    own files.
    """
    monkeypatch.chdir(tempfile.mkdtemp(prefix="cwd-", dir=tmp_path_factory.getbasetemp()))


_REPO_STDLIB_ROOT = Path(__file__).resolve().parent.parent / "packages" / "stdlib"


@pytest.fixture(autouse=True)
def pin_agm_stdlib_to_repo(
    clear_workspace_shell_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the AgL stdlib to the in-repo tree so tests never read installed files.

    ``clear_workspace_shell_env`` strips every ``AGM_*`` variable first; this
    runs afterwards and points ``AGM_STDLIB`` at ``<repo>/packages/stdlib``.  Every
    ``agm exec``/``agm repl`` invocation — in-process or as a subprocess that
    inherits the environment — then resolves the standard library from the
    current source tree, independent of whatever ``~/.agm`` happens to contain.
    """
    monkeypatch.setenv("AGM_STDLIB", str(_REPO_STDLIB_ROOT))


@pytest.fixture(autouse=True)
def detach_installed_agm_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide any AgM installed at the test runner's own prefix from the suite.

    Tests that exercise the installation-prefix fallback take the
    ``installed_agm_prefix`` fixture instead.
    """
    _isolation.detach_installed_agm_prefix(monkeypatch)


@pytest.fixture()
def installed_agm_prefix(monkeypatch: pytest.MonkeyPatch) -> Callable[[Path], None]:
    """Opt out of ``detach_installed_agm_prefix`` for one test by pinning a staged prefix."""

    def pin(prefix: Path) -> None:
        monkeypatch.setattr("agnette.config.home.agm_installation_prefix", lambda: prefix)

    return pin


@pytest.fixture(autouse=True)
def refuse_real_http_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test's HTTP transport to one that refuses any request.

    A test that scripts HTTP overrides this by patching ``agnette.core.http.open_session``
    again afterward.
    """
    _isolation.refuse_real_http_requests(monkeypatch)


@pytest.fixture(autouse=True)
def refuse_real_httpx2_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any real ``httpx2`` network send and strip the TypeSafe SDK's environment."""
    _isolation.refuse_real_httpx2_requests(monkeypatch)


@pytest.fixture()
def python_installer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PythonInstaller:
    """Replace the Python installer with a recorder for AgM home ``tmp_path / "home"``."""
    recorder = PythonInstaller(home=tmp_path / "home")
    monkeypatch.setattr(process, "run_foreground", recorder.run_foreground)
    monkeypatch.setattr("agnette.core.pyenv.shutil.which", lambda _name: "/opt/tools/uv")
    return recorder


@pytest.fixture()
def env(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Environment dict with git identity and a home of its own.

    A separate home from the one :func:`isolate_host_environment` installs, so a
    test that passes this mapping to a subprocess can inspect what AgM wrote
    under it.
    """
    e = os.environ.copy()
    e.update(GIT_IDENTITY)
    e["GIT_CEILING_DIRECTORIES"] = _git_ceiling_directories(tmp_path_factory)
    e["SHELL"] = shutil.which("bash") or "/bin/sh"
    for name in _HOST_CONTEXT_VARIABLES:
        e.pop(name, None)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    e["HOME"] = str(fake_home)
    e["XDG_CONFIG_HOME"] = str(fake_home / ".config")
    e["XDG_DATA_HOME"] = str(fake_home / ".local" / "share")
    return e


@dataclass(slots=True)
class FakeAgentTransport:
    """Records dispatched agent runs and stubs their transport result.

    Every dispatched ``(prompt, runner argv)`` pair is recorded on ``calls``.
    Queue responses built with :meth:`success` / :meth:`failure` via
    :meth:`queue` to script what consecutive dispatches return, in order.  A
    queued callable is called when its dispatch happens and returns that
    dispatch's response, letting a test act as the agent would.  A transport
    that was never scripted answers every dispatch with a placeholder
    success; once scripted, a dispatch past the last queued response fails the
    test rather than inventing one.
    """

    calls: list[tuple[str, list[str]]] = field(default_factory=list)
    _responses: list[object] = field(default_factory=list)
    _scripted: bool = False

    def queue(self, *responses: object) -> None:
        """Append *responses* to the FIFO consumed one-per-dispatch.

        A callable takes no arguments and returns the response, after any side effects.
        """
        self._scripted = True
        self._responses.extend(responses)

    @staticmethod
    def success(stdout: str = "ok") -> SimpleNamespace:
        """Build a successful transport result carrying *stdout*."""
        return SimpleNamespace(
            spawn_error=None,
            timed_out=False,
            returncode=0,
            stdout=CapturedOutput(data=stdout.encode(), truncated=False),
            stderr=CapturedOutput(data=b"", truncated=False),
            elapsed=0.0,
        )

    @staticmethod
    def failure(
        *,
        spawn_error: str | None = None,
        timed_out: bool = False,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
        elapsed: float = 0.0,
    ) -> SimpleNamespace:
        """Build a failing transport result: spawn error, timeout, or bad exit code."""
        return SimpleNamespace(
            spawn_error=spawn_error,
            timed_out=timed_out,
            returncode=returncode,
            stdout=CapturedOutput(data=stdout.encode(), truncated=timed_out),
            stderr=CapturedOutput(data=stderr.encode(), truncated=timed_out),
            elapsed=elapsed,
        )

    def _next_response(self) -> object:
        if self._responses:
            response = self._responses.pop(0)
            return response() if callable(response) else response
        if self._scripted:
            raise AssertionError("agent dispatched more times than there were queued responses")
        return self.success()


@pytest.fixture()
def fake_agent_transport(monkeypatch: pytest.MonkeyPatch) -> FakeAgentTransport:
    """Stub the ``agnette.agent.runner`` prepare/run seam that every agent dispatch uses.

    Replaces ``prepare_rendered_prompt_run`` and ``run_prepared_prompt_result``
    so no real subprocess is spawned, regardless of whether the dispatch is
    driven through the ``agm exec`` CLI, ``exec_command.run``, or a bare
    ``PipelineDriver`` — all three route through the same seam. See
    :class:`FakeAgentTransport` for recording/queuing behavior.
    """
    from agnette.agent.runner import PreparedPromptRun

    transport = FakeAgentTransport()

    def prepare(prompt: str, *, runner: list[str], **_: object) -> PreparedPromptRun:
        transport.calls.append((prompt, runner))
        return PreparedPromptRun(env={}, argv=runner)

    monkeypatch.setattr("agnette.agent.runner.prepare_rendered_prompt_run", prepare)
    monkeypatch.setattr(
        "agnette.agent.runner.run_prepared_prompt_result",
        lambda _prepared, **_: transport._next_response(),
    )
    return transport


# pytest names each test's ``tmp_path`` directory after the test itself, so an
# absolute path embedded in a message can supply the very word an assertion is
# looking for — ``assert "ambiguous" in message`` has passed on the strength of
# a ``..._ambiguous_module_fails0`` path component alone, with the production
# wording deleted.  Setting AGM_TEST_NEUTRAL_TMP_PATH=1 hands out neutrally
# named directories instead, so any assertion resting on its own test's name
# fails.  Off by default: test-named temp directories are worth keeping when
# reading a failure.
if os.environ.get("AGM_TEST_NEUTRAL_TMP_PATH") == "1":

    @pytest.fixture
    def tmp_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
        """Temp directory whose name cannot leak the test name into assertions."""
        return tmp_path_factory.mktemp("t")


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Return a setter for the isolated home config file's contents, as ``agm exec`` reads it."""
    from agnette.agm.commands import exec_program
    from agnette.config.context import ConfigContext

    home = tmp_path / "home"
    (home / ".agm").mkdir(parents=True)
    monkeypatch.setattr(
        exec_program,
        "current_config_context",
        lambda: ConfigContext(home=home, proj_dir=None, cwd=tmp_path),
    )

    def write(contents: str) -> None:
        (home / ".agm" / "config.toml").write_text(contents)

    return write
