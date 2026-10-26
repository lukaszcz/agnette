"""Helpers for process-oriented tests."""

from __future__ import annotations

import os
import select
import signal
import time
from pathlib import Path

SIGNAL_POLL_SECONDS = 0.05
"""Longest blocking call a process waiting for a Python-level signal handler makes."""

SLEEP_UNTIL_SIGNALLED_SNIPPET = (
    f"for _ in range({round(60 / SIGNAL_POLL_SECONDS)}):\n    time.sleep({SIGNAL_POLL_SECONDS})\n"
)
"""`sleep_until_signalled(60)` as a ``-c`` script tail; the script must ``import time``."""


def sleep_until_signalled(seconds: float = 60.0) -> None:
    """Sleep up to *seconds*, in short steps, for a signal whose Python handler ends the wait.

    A single long blocking call can miss a signal that arrives just before the call starts:
    CPython's C handler only sets a flag, the call is never interrupted, and the Python
    handler would not run until the call returns. A pending handler runs within one step.
    """
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(remaining, SIGNAL_POLL_SECONDS))


def read_until_signalled(fd: int, size: int) -> bytes:
    """``os.read(fd, size)`` after short-step readability waits (see `sleep_until_signalled`)."""
    while not select.select([fd], [], [], SIGNAL_POLL_SECONDS)[0]:
        pass
    return os.read(fd, size)


def wait_for_path(path: Path, *, timeout: float = 30.0) -> None:
    """Block until *path* exists, or raise ``AssertionError`` after *timeout*."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def wait_for_signal_ignored_script(pid: int | str, signum: int, *, timeout: int = 30) -> str:
    """Shell snippet: block until *pid* has *signum* set in its `/proc` `SigIgn` mask.

    Exits 1 after *timeout* seconds if the ignore never appears.

    *pid* is a literal pid or a shell expression (e.g. ``'"$TARGET"'``) that
    evaluates to one. For a test child that signals *pid* right after
    starting: a fixed-delay child would race whatever installs the ignore in
    *pid* (e.g. a ``run_foreground_ignoring_signals`` call, which only starts
    ignoring once its own child is spawned); polling the real kernel-reported
    disposition instead makes the handshake exact.
    """
    bit = 1 << (signum - 1)
    return (
        f"t={pid}\n"
        f"d=$(( $(date +%s) + {timeout} ))\n"
        "while :; do\n"
        '  [ "$(date +%s)" -lt "$d" ] || exit 1\n'
        '  m=$(sed -n "s/^SigIgn:[[:space:]]*//p" /proc/$t/status)\n'
        f"  [ $(( 0x$m & {bit} )) -ne 0 ] && break\n"
        "  sleep 0.02\n"
        "done\n"
    )


def signal_when_sleeping_script(pid: int | str, signum: int, *, timeout: int = 30) -> str:
    """Shell snippet: wait until *pid* is blocked (`/proc` state ``S``), then send it *signum*.

    Exits 1 after *timeout* seconds if *pid* never blocks. *pid* is a literal pid or a shell
    expression. For delivering an interrupt from outside, while the target waits in a call.
    """
    return (
        f"t={pid}\n"
        f"d=$(( $(date +%s) + {timeout} ))\n"
        "while :; do\n"
        '  [ "$(date +%s)" -lt "$d" ] || exit 1\n'
        '  [ "$(sed -n "s/^.*) \\(.\\).*/\\1/p" /proc/$t/stat)" = S ] && break\n'
        "  sleep 0.02\n"
        "done\n"
        f'kill -{signum} "$t"\n'
    )


def reap_child(pid: int, *, timeout: float = 10.0) -> int:
    """Reap child *pid* and return its wait status, killing it if it outlives *timeout*."""
    deadline = time.monotonic() + timeout
    while True:
        reaped, status = os.waitpid(pid, os.WNOHANG)
        if reaped:
            return status
        if time.monotonic() >= deadline:
            os.kill(pid, signal.SIGKILL)
            return os.waitpid(pid, 0)[1]
        time.sleep(0.02)
