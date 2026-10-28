"""An enforced gate for "100% command coverage in e2e tests".

Line coverage cannot express this guarantee.  A command's implementation can be
fully covered by in-process unit tests while the command itself is never once
driven through the real ``agm`` binary, so a broken argument container, a
missing registration or a crash in CLI wiring ships unnoticed.  What has to be
checked is the *command surface*: every leaf command AgM registers must be
invoked, as a subprocess, by at least one e2e test.

Both halves of that comparison are derived, never written down:

* The command set comes from walking the live Typer registry of
  :data:`agnette.agm.cli.app` — its ``registered_commands`` and ``registered_groups``,
  recursively.  Adding a subcommand anywhere in the tree therefore adds a gate
  obligation with no test-side bookkeeping.  Walking the Typer objects rather
  than the Click tree also keeps package-registered commands (resolved
  dynamically at invocation time) out of the set: they are not AgM's own
  surface.
* ``tests.test_e2e.run_agm`` collects dispatch evidence from each child through
  a private descriptor. A test-only profiler observes a registered leaf reaching
  its command-layer ``run`` function, after CLI validation. Help flags, bare
  groups and parser failures therefore provide no evidence. The real ``help``
  leaf implements its behavior in its callback and counts on callback entry.
  Local monitoring revokes evidence if deferred CLI parsing rejects arguments
  or requests program help. No application changes or argv re-parsing are involved.

Group spellings that are pure aliases (``wsp`` for ``workspace``, ``wt`` for
``worktree`` — see ``agnette.command_catalog.COMMAND_ALIASES``) are folded onto
their canonical group before comparison.  Distinctly registered leaves such as
``config cp`` and ``config copy`` are *not* folded: they are separate callbacks
and each needs its own end-to-end proof.

Aggregation across ``pytest-xdist`` workers reuses the pattern established by
:mod:`tests._durations`: each worker ships its recorded set up through
``config.workeroutput`` at session finish and the controller merges the shipped
sets in ``pytest_testnodedown`` before judging.  The module is registered as a
plugin by ``tests/conftest.py`` so its hooks live beside — not on top of — the
duration hooks that share those names.

The gate only fires on a full-suite run; see :func:`gate_enabled`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import CodeType, FrameType, FunctionType
from typing import BinaryIO

import pytest
import typer
from typer.main import get_command_name

from agnette.agm.cli import app
from agnette.command_catalog import COMMAND_ALIASES

# Forces the gate on ("1") or off ("0"), overriding full-suite detection.
GATE_ENV = "AGM_TEST_COMMAND_COVERAGE"

_WORKEROUTPUT_KEY = "agm_command_coverage"

# Only the staged test entry point reads this; production knows nothing about it.
EVIDENCE_FD_ENV = "AGM_TEST_COMMAND_COVERAGE_FD"

# Leaf commands that cannot be driven end-to-end, with the reason each is out.
EXCLUDED_COMMANDS: dict[tuple[str, ...], str] = {}

# Canonical leaf paths invoked through ``run_agm`` in this process.
_invoked: set[tuple[str, ...]] = set()


# ---------------------------------------------------------------------------
# The command surface, read off the live Typer registry
# ---------------------------------------------------------------------------


def _command_name(info: typer.models.CommandInfo) -> str:
    """Return the CLI name Typer registers *info* under."""
    if info.name:
        return info.name
    assert info.callback is not None, "a Typer command has either a name or a callback"
    return get_command_name(info.callback.__name__)


def group_alias_map(root: typer.Typer = app) -> dict[str, str]:
    """Return ``{alias group name: canonical group name}`` for *root*'s groups.

    An alias registers the *same* sub-application under a second name, so the
    canonical spelling is found by identity: the non-alias name that carries the
    same ``Typer`` instance.  Which names are aliases is AgM's own declaration
    (``agnette.command_catalog.COMMAND_ALIASES``), not a guess.
    """
    canonical: dict[int, str] = {
        id(group.typer_instance): group.name or ""
        for group in root.registered_groups
        if group.name not in COMMAND_ALIASES
    }
    return {
        group.name: canonical[id(group.typer_instance)]
        for group in root.registered_groups
        if group.name in COMMAND_ALIASES and id(group.typer_instance) in canonical
    }


def _walk(
    node: typer.Typer, prefix: tuple[str, ...]
) -> Iterable[tuple[tuple[str, ...], typer.models.CommandInfo]]:
    """Yield every registered leaf and its path."""
    for info in node.registered_commands:
        yield (*prefix, _command_name(info)), info
    for group in node.registered_groups:
        if group.typer_instance is not None:
            yield from _walk(group.typer_instance, (*prefix, group.name or ""))


def leaf_commands(root: typer.Typer = app) -> frozenset[tuple[str, ...]]:
    """Return every canonical leaf-command path AgM registers."""
    aliases = group_alias_map(root)
    return frozenset((aliases.get(path[0], path[0]), *path[1:]) for path, _info in _walk(root, ()))


# ---------------------------------------------------------------------------
# Recording what an e2e invocation actually ran
# ---------------------------------------------------------------------------


def _watch_rejections(fd: int) -> None:
    """Revoke dispatch evidence on deferred argument errors or program help.

    Command-layer adapters can still parse program flags or validate values.
    Local code events avoid profiling the entire AgL execution that follows.
    """
    from agnette.agm.cli_support.program_options import CommandUsageError, ProgramHelpRequested
    from agnette.agm.parser import exit_with_usage_error

    codes = (
        CommandUsageError.__init__.__code__,
        ProgramHelpRequested.__init__.__code__,
        exit_with_usage_error.__code__,
    )
    tool = next(index for index in range(5, -1, -1) if sys.monitoring.get_tool(index) is None)
    sys.monitoring.use_tool_id(tool, "agm-command-coverage")

    def reject(code: CodeType, offset: int) -> None:
        del code, offset
        for watched in codes:
            sys.monitoring.set_local_events(tool, watched, 0)
        sys.monitoring.free_tool_id(tool)
        os.ftruncate(fd, 0)
        os.close(fd)

    sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, reject)
    for code in codes:
        sys.monitoring.set_local_events(tool, code, sys.monitoring.events.PY_START)


def install_dispatch_observer(fd: int | None = None) -> None:
    """Write one canonical leaf to *fd* when this child actually dispatches.

    Cold staged entry points obtain the descriptor from the environment; forked
    children receive it directly. Neither environment nor descriptor survives
    an exec, so commands AgM launches cannot supply evidence for their caller.
    General profiling stops at dispatch; only deferred rejection sites remain
    monitored. Domain failures still count: dispatch, not success, is the gate.
    """
    inherited = os.environ.pop(EVIDENCE_FD_ENV, None)
    if fd is None:
        if inherited is None:
            return
        fd = int(inherited)
    os.set_inheritable(fd, False)
    aliases = group_alias_map()
    callbacks: dict[CodeType, tuple[str, ...]] = {
        info.callback.__code__: (aliases.get(path[0], path[0]), *path[1:])
        for path, info in _walk(app, ())
        if isinstance(info.callback, FunctionType)
    }
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, arg: object) -> None:
        if previous is not None:
            previous(frame, event, arg)
        if event != "call":
            return
        path = callbacks.get(frame.f_code)
        if path != ("help",):
            module = frame.f_globals.get("__name__", "")
            if frame.f_code.co_name != "run" or not module.startswith("agnette.agm.commands."):
                return
            caller = frame.f_back
            path = None
            while caller is not None:
                path = callbacks.get(caller.f_code)
                if path is not None:
                    break
                caller = caller.f_back
        if path is not None:
            sys.setprofile(previous)
            os.write(fd, (" ".join(path) + "\n").encode())
            if path == ("help",):
                os.close(fd)
            else:
                _watch_rejections(fd)

    sys.setprofile(observe)


def record_invocation(evidence: str) -> None:
    """Record only registered leaves witnessed by a child's observer."""
    surface = leaf_commands()
    _invoked.update(
        path for line in evidence.splitlines() if (path := tuple(line.split(" "))) in surface
    )


@contextmanager
def invocation_evidence() -> Iterator[BinaryIO]:
    """Collect one child's evidence, including when its invocation raises."""
    with tempfile.TemporaryFile() as evidence:
        try:
            yield evidence
        finally:
            evidence.seek(0)
            record_invocation(evidence.read().decode())


def recorded_commands() -> frozenset[tuple[str, ...]]:
    """Return the leaf commands recorded in this process so far."""
    return frozenset(_invoked)


# ---------------------------------------------------------------------------
# Gate scope
# ---------------------------------------------------------------------------


def gate_enabled(config: pytest.Config) -> bool:
    """Return whether this session is a full-suite run the gate may judge.

    Running a single file must stay silent: an e2e-free session would otherwise
    report the entire command surface as uncovered.  A session qualifies when
    every path it was pointed at contains the whole ``tests`` directory and no
    selection option (``-k``, ``-m``, ``--deselect``, ``--lf``/``--ff``) narrows
    what runs.  ``AGM_TEST_COMMAND_COVERAGE`` overrides the detection in either
    direction, which is how the gate is exercised on a subset.
    """
    override = os.environ.get(GATE_ENV)
    if override:
        return override != "0"
    option = config.option
    if option.collectonly:
        return False
    if option.keyword or option.markexpr or option.deselect:
        return False
    if getattr(option, "lf", False) or getattr(option, "failedfirst", False):
        return False
    tests_dir = Path(__file__).resolve().parent
    invocation_dir = Path(config.invocation_params.dir)
    args = [str(arg).split("::")[0] for arg in config.args]
    if not args:
        return False
    return all(
        (target := (invocation_dir / arg).resolve()) == tests_dir or target in tests_dir.parents
        for arg in args
    )


# ---------------------------------------------------------------------------
# pytest hooks
# ---------------------------------------------------------------------------


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Ship a worker's recorded commands upwards, or judge them on the controller."""
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_WORKEROUTPUT_KEY] = sorted(" ".join(path) for path in _invoked)
        return
    _enforce(session)


def pytest_testnodedown(node: object, error: object) -> None:
    """Merge one finished xdist worker's recorded commands on the controller."""
    output = getattr(node, "workeroutput", None) or {}
    _invoked.update(tuple(entry.split(" ")) for entry in output.get(_WORKEROUTPUT_KEY, ()))


def _enforce(session: pytest.Session) -> None:
    """Fail the session listing every leaf command no e2e test invoked."""
    if not gate_enabled(session.config):
        return
    if session.testsfailed or session.shouldstop or session.shouldfail:
        # An incomplete run proves nothing about which commands were reachable.
        return
    missing = sorted(leaf_commands() - _invoked - set(EXCLUDED_COMMANDS))
    if not missing:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "commands with no e2e coverage")
        for path in missing:
            reporter.write_line("  agm " + " ".join(path))
        reporter.write_line(f"{len(missing)} command(s) were never invoked through the agm binary")
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
