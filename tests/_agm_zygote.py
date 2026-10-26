"""A preforking launcher for the staged ``agm`` CLI used by the e2e tests.

Every e2e invocation of the checkout CLI pays a fresh interpreter start and
the import of several hundred modules before the command under test runs any
AgM code at all — around 70 ms for a command that never touches AgL and around
340 ms for one that does, against roughly 620 invocations per suite run.

This module amortizes that. A *zygote* process per xdist worker imports the CLI
and the AgL stack once, then forks a child for each invocation. The child is an
ordinary OS process: it gets the invocation's own argv, environment, working
directory and standard file descriptors, and its exit status is the real one.
Two things it cannot re-prove: that a *cold* interpreter can import AgM in the
order a given command needs, and that a command survives a hash seed of its
own, since every child inherits the zygote's. :envvar:`AGM_TEST_NO_ZYGOTE`
turns the whole mechanism off and returns every invocation to a plain
``subprocess.run``, which is how the suite is run to settle either question.

The zygote never runs a command itself, so a child inherits only what the zygote
imported, its hash seed and its interpreter-start environment.
"""

from __future__ import annotations

import importlib
import locale
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from tests import _zygote

if TYPE_CHECKING:
    from collections.abc import Sequence

# An invocation as it crosses the socket: argv, environment, working directory.
type Invocation = tuple[list[str], dict[str, str], str]

# Set to "1" to run every e2e invocation as its own cold process.
DISABLE_ENV = _zygote.DISABLE_ENV

# How long a client waits for a newly spawned zygote to start listening.
_STARTUP_TIMEOUT = 60.0

# Standard descriptors, in the order the client hands them over.
_STREAM_COUNT = 3


def _encoding() -> str:
    """Return the encoding ``subprocess.run(text=True)`` would use."""
    return locale.getencoding()


def _decode(data: bytes) -> str:
    """Decode captured output exactly as ``text=True`` does, newlines included."""
    return data.decode(_encoding()).replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


# What a cold ``agm`` run imports on its way to a command. Pre-importing the
# AgL stack is the point of the exercise; the CLI modules come along because
# they are what reaches it.
_PRELOADED = (
    "agnette.agm.cli",
    "agnette.agm.cli_dispatch",
    "agnette.agm.cli_support.program_options",
    "agnette.agm.commands.exec_program",
    "agnette.agl.pipeline",
    "agnette.agl.runtime",
)


def _preload() -> None:
    """Import everything a command could need, so a forked child imports none of it."""
    for name in _PRELOADED:
        importlib.import_module(name)


def _run_command(request: Invocation) -> int:
    """Become the invocation described by *request* and run the CLI.

    Returns what the staged entry point would hand to ``sys.exit``.
    """
    argv, env, cwd = request
    _zygote.adopt(env, cwd)
    sys.argv = list(argv)
    from agnette.agm.cli import main

    return _zygote.exit_status(main())


def _child(request: Invocation, fds: list[int]) -> NoReturn:
    """Take over the standard descriptors, run the command, and exit."""

    def body() -> int:
        for target, fd in enumerate(fds):
            os.dup2(fd, target)
        for fd in fds:
            if fd >= _STREAM_COUNT:
                os.close(fd)
        return _run_command(request)

    _zygote.run_child(body)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Zygote(_zygote.Launcher):
    """A running ``agm`` zygote and the socket its clients invoke it through."""

    def run(
        self,
        argv: Sequence[str],
        env: dict[str, str],
        cwd: str,
        stdin_text: str | None,
    ) -> subprocess.CompletedProcess[str]:
        """Run *argv* in a forked child and collect it like ``subprocess.run`` would.

        Output goes to temporary files rather than pipes, so neither side can
        block on a full pipe buffer and no reader thread is needed.
        """
        with (
            tempfile.TemporaryFile() as stdin_file,
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            if stdin_text is not None:
                stdin_file.write(stdin_text.encode(_encoding()))
                stdin_file.seek(0)
            request: Invocation = (list(argv), env, cwd)
            reply = self.invoke(
                request, (stdin_file.fileno(), stdout_file.fileno(), stderr_file.fileno())
            )
            stdout_file.seek(0)
            stderr_file.seek(0)
            return subprocess.CompletedProcess(
                list(argv),
                os.waitstatus_to_exitcode(reply.status),
                _decode(stdout_file.read()),
                _decode(stderr_file.read()),
            )


# Run by the zygote's interpreter: place the checkout's source root and the repository
# root (for this module), then drop the latter again so the children cannot import
# ``tests``. ``-I`` ignores PYTHONPATH, which is why the paths travel as arguments.
_BOOTSTRAP = """\
import sys
repo, source, name = sys.argv[1:]
sys.path[:0] = [source, repo]
from tests import _agm_zygote
sys.path.remove(repo)
_agm_zygote.main(name)
"""


def start(source_root: Path, log: Path) -> Zygote:
    """Spawn a zygote and wait until it accepts connections.

    *source_root* is prepended to the child's import path, which is what makes the
    zygote serve this checkout -- the same selection the staged ``agm`` entry point
    makes for itself. The zygote's stderr goes to the file *log*.
    """
    repo_root = Path(__file__).resolve().parents[1]
    return _zygote.start(
        Zygote,
        [sys.executable, "-I", "-c", _BOOTSTRAP, str(repo_root), str(source_root)],
        name=_zygote.unique_name("agm-zygote"),
        env=os.environ,
        log=log,
        startup_timeout=_STARTUP_TIMEOUT,
    )


def main(name: str) -> None:
    """Serve invocations on the socket *name* (the zygote process's entry point)."""
    _zygote.serve(name, _preload, _child)
