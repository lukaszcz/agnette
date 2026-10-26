"""A preforking launcher framework for the test suite.

A *zygote* is a single-threaded server process that imports what a family of
short-lived child processes would otherwise import for themselves, then forks one
child per request. A child is an ordinary OS process: it takes over the file
descriptors the client passed (``SCM_RIGHTS``), and its exit status is the real one.
A zygote never runs a request itself, so a child inherits import-time state only.

One zygote serves one xdist worker, which asks it for one child at a time. The worker
cannot see a child the zygote reaped in its own :func:`os.times`, so the zygote
sends each child's CPU back with its status and :meth:`Launcher.invoke` charges it to
the running test (:func:`tests._durations.charge_external_cpu_seconds`).

A launcher is specialized by two callables run in the zygote: ``preload`` (what to
import before serving) and ``child`` (what a request becomes; it ends through
:func:`run_child` after taking over the request's process state with :func:`adopt`).
The client half has no further hooks: requests are pickled, so they hold plain data.

The wire protocol, per connection: the client sends a request frame carrying its
descriptors; the zygote answers with the child's pid, then, once the child has been
reaped, with ``(wait status, CPU seconds)``. A zygote exits when its worker is gone.
A client that gives up on a child kills it by pid (:meth:`Launcher.invoke`), which
makes the zygote's wait return, so it is idle again.
"""

from __future__ import annotations

import array
import contextlib
import gc
import os
import pickle
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

# Set to "1" to run every launcher's requests as cold processes instead.
DISABLE_ENV = "AGM_TEST_NO_ZYGOTE"

_LENGTH = struct.Struct("!I")

# How long the zygote waits for work before checking that it still has a worker
# to serve. A killed worker leaves no one to shut it down, so it notices itself.
_IDLE_CHECK = 5.0

# How long a client waits for the zygote to report a child it has just forked, and
# for the final report of a child it has just had killed.
_PROMPT_REPLY = 30.0

# The most descriptors a request may carry.
_MAX_FDS = 8


def _reaped_cpu_seconds() -> float:
    """Return the CPU this process has been credited with for children it reaped."""
    times = os.times()
    return times.children_user + times.children_system


def charge(seconds: float) -> None:
    """Report CPU the zygote burned on a test's behalf, or take it back when negative.

    Imported on use rather than at module scope: the server half of this module runs
    in the zygote, and every module it holds is inherited by every child it forks.
    """
    from tests._durations import charge_external_cpu_seconds

    charge_external_cpu_seconds(seconds)


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def send(conn: socket.socket, payload: bytes, fds: Sequence[int] = ()) -> None:
    """Send one length-prefixed frame, optionally passing *fds* alongside it."""
    frame = _LENGTH.pack(len(payload)) + payload
    if not fds:
        conn.sendall(frame, socket.MSG_NOSIGNAL)
        return
    rights = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))]
    sent = conn.sendmsg([frame], rights, socket.MSG_NOSIGNAL)
    if sent < len(frame):
        conn.sendall(frame[sent:], socket.MSG_NOSIGNAL)


def _recv_exactly(conn: socket.socket, size: int) -> bytes:
    """Read exactly *size* bytes, or return short on a closed connection."""
    chunks = []
    remaining = size
    while remaining:
        chunk = conn.recv(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv(conn: socket.socket) -> bytes:
    """Read one length-prefixed frame, or ``b""`` when the peer hung up."""
    header = _recv_exactly(conn, _LENGTH.size)
    if len(header) < _LENGTH.size:
        return b""
    return _recv_exactly(conn, _LENGTH.unpack(header)[0])


def recv_with_fds(conn: socket.socket) -> tuple[bytes, list[int]]:
    """Read one frame together with the descriptors passed alongside it."""
    space = socket.CMSG_SPACE(_MAX_FDS * array.array("i").itemsize)
    header, ancillary, _flags, _addr = conn.recvmsg(_LENGTH.size, space)
    fds: list[int] = []
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            received = array.array("i")
            received.frombytes(data[: len(data) - len(data) % received.itemsize])
            fds.extend(received)
    if len(header) < _LENGTH.size:
        for fd in fds:
            os.close(fd)
        return b"", []
    return _recv_exactly(conn, _LENGTH.unpack(header)[0]), fds


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def serve(
    name: str,
    preload: Callable[[], None],
    child: Callable[[Any, list[int]], NoReturn],
) -> None:
    """Serve requests on the abstract socket *name* until the worker that started us is gone.

    Each request is unpickled and handed, with the descriptors that came with it, to
    *child* in a freshly forked process; *child* must end that process itself.
    """
    preload()
    # Imported state stays live; keep child GC from dirtying its shared pages.
    gc.collect()
    gc.freeze()
    # A thread here would leave every child with a lock no thread releases.
    assert threading.active_count() == 1, "a zygote must stay single-threaded"
    worker = os.getppid()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind("\0" + name)
        server.listen(64)
        server.settimeout(_IDLE_CHECK)
        while True:
            try:
                conn, _peer = server.accept()
            except TimeoutError:
                if os.getppid() != worker:
                    return
                continue
            with conn:
                request_bytes, fds = recv_with_fds(conn)
                if not request_bytes:
                    for fd in fds:
                        os.close(fd)
                    continue
                request = pickle.loads(request_bytes)
                # Nothing of this process may reach the child half-written.
                sys.stdout.flush()
                sys.stderr.flush()
                pid = os.fork()
                if pid == 0:
                    server.close()
                    conn.close()
                    child(request, fds)
                for fd in fds:
                    os.close(fd)
                _serve_one(conn, pid)


def _serve_one(conn: socket.socket, pid: int) -> None:
    """Report forked child *pid* to its client, wait for it, and report how it ended.

    A client that gave up (and killed the child) is gone by now; that is not an error.
    """
    with contextlib.suppress(OSError):
        send(conn, pickle.dumps(pid))
    _waited, status, usage = os.wait4(pid, 0)
    with contextlib.suppress(OSError):
        send(conn, pickle.dumps((status, usage.ru_utime + usage.ru_stime)))


def adopt(env: Mapping[str, str], cwd: str | Path) -> None:
    """Make this forked child's working directory and environment the request's own."""
    os.chdir(cwd)
    os.environ.clear()
    os.environ.update(env)
    # A temp directory cached from the zygote's environment must not leak in.
    tempfile.tempdir = None


def exit_status(code: object) -> int:
    """Return what the interpreter would exit with for a ``SystemExit`` carrying *code*.

    A missing code is success and an integer is itself; anything else is a
    message the interpreter prints before failing.
    """
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1


def run_child(body: Callable[[], int]) -> NoReturn:
    """Run *body* in a forked child and exit the process with the status it returns.

    An exception escaping *body* is reported on stderr and fails, as in a cold process;
    ``SystemExit`` carries its code. Output is flushed before the exit.
    """
    code = 1
    try:
        code = body()
    except SystemExit as exit_request:
        code = exit_status(exit_request.code)
    except BaseException:
        traceback.print_exc()
    finally:
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(ValueError):
                stream.flush()
        os._exit(code)


def kill_process(pid: int) -> None:
    """SIGKILL *pid*, which may already be gone."""
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def live_processes() -> list[tuple[int, int, int]]:
    """Return ``(pid, parent pid, session id)`` of every live (non-zombie) process."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # Fields after the parenthesized command: state, ppid, pgrp, session, ...
        state, ppid, _, session = stat.rpartition(")")[2].split()[:4]
        if state != "Z":
            found.append((int(entry.name), int(ppid), int(session)))
    return found


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LauncherError(RuntimeError):
    """The zygote failed to start or stopped answering.

    *pid* is the child it had forked, if it reported one. *sent* tells whether the request
    reached the zygote, which may then have run it: only an error with ``sent`` false
    leaves the request unexecuted.
    """

    def __init__(self, message: str, pid: int | None = None, *, sent: bool = True) -> None:
        super().__init__(message)
        self.pid = pid
        self.sent = sent


@dataclass(frozen=True)
class Reply:
    """How a child ended: its pid, its wait status, and whether the client gave up waiting."""

    pid: int
    status: int
    timed_out: bool = False


class Launcher:
    """A running zygote process and the socket its clients invoke it through."""

    def __init__(self, address: str, process: subprocess.Popen[bytes]) -> None:
        self._address = address
        self._process = process

    @property
    def alive(self) -> bool:
        """Whether the zygote process is still running."""
        before = _reaped_cpu_seconds()
        running = self._process.poll() is None
        # Noticing that it ended reaps it, with everything it was never charged for.
        charge(before - _reaped_cpu_seconds())
        return running

    def invoke(
        self,
        request: object,
        fds: Sequence[int],
        *,
        timeout: float | None = None,
        kill: Callable[[int], None] = kill_process,
    ) -> Reply:
        """Fork a child for *request* with *fds* and wait for it; charge its CPU to the test.

        After *timeout* seconds the child is given up on and the reply is marked timed out.
        Giving up -- by timeout or by any exception, including an interrupt, while the child
        runs -- calls *kill* with the child's pid; it must kill the child and whatever it
        started. The zygote then reaps the child and is idle again. If no pid was reported
        yet, the zygote cannot be told from its child: both are killed (*kill* for each of the
        zygote's children) and this launcher is dead.
        Raises :class:`LauncherError` if the zygote cannot be reached, hangs up or goes quiet.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        pid: int | None = None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            try:
                conn.connect(self._address)
                send(conn, pickle.dumps(request), fds)
            except OSError as error:
                raise LauncherError(f"the zygote is unreachable: {error}", sent=False) from error
            try:
                pid = self._receive(conn, _PROMPT_REPLY)
                timed_out = False
                try:
                    remaining = (
                        None if deadline is None else max(deadline - time.monotonic(), 0.001)
                    )
                    status, cost = self._receive(conn, remaining, pid, may_time_out=True)
                except TimeoutError:
                    timed_out = True
                    kill(pid)
                    status, cost = self._receive(conn, _PROMPT_REPLY, pid)
            except BaseException:
                self._give_up(pid, kill)
                raise
        charge(cost)
        return Reply(pid, status, timed_out)

    def _give_up(self, pid: int | None, kill: Callable[[int], None]) -> None:
        """Kill the child *pid* (or, if no pid arrived, the zygote and all its children)."""
        if pid is not None:
            kill(pid)
            return
        zygote = self._process.pid
        children = [child for child, parent, _ in live_processes() if parent == zygote]
        self._process.kill()
        for child in children:
            kill(child)

    def _receive(
        self,
        conn: socket.socket,
        timeout: float | None,
        pid: int | None = None,
        *,
        may_time_out: bool = False,
    ) -> Any:
        """Read one unpickled reply frame within *timeout* seconds.

        A hang-up or, unless *may_time_out*, a silence ends the zygote and raises.
        """
        conn.settimeout(timeout)
        try:
            frame = recv(conn)
        except TimeoutError:
            if may_time_out:
                raise
            self._process.kill()
            raise LauncherError("the zygote went quiet", pid) from None
        if not frame:
            raise LauncherError("the zygote closed the connection before reporting", pid)
        return pickle.loads(frame)

    def close(self) -> None:
        """Stop the zygote and un-charge its subtree.

        Reaping the zygote credits this process with the CPU of every child the
        zygote reaped -- which each invocation was already charged for as it
        finished -- and with the zygote's own start-up, which no test owns. Left
        alone, that arrives as one enormous extra cost against whichever test
        happens to be the last, so it is taken back here (and in :attr:`alive`).
        """
        before = _reaped_cpu_seconds()
        self._process.terminate()
        self._process.wait()
        charge(before - _reaped_cpu_seconds())


def unique_name(prefix: str) -> str:
    """A socket name no other zygote of any worker shares."""
    return f"{prefix}-{os.getpid()}-{uuid.uuid4().hex}"


def start[L: Launcher](
    launcher_type: type[L],
    argv: Sequence[str],
    *,
    name: str,
    env: Mapping[str, str],
    log: Path,
    cwd: str | None = None,
    startup_timeout: float,
) -> L:
    """Spawn the zygote ``argv + [name]`` and wait until it accepts connections.

    It listens on a Linux abstract-namespace socket: no filesystem path, so no
    path-length cap under a long ``TMPDIR``, and the name vanishes with the
    process. The zygote's ``__main__`` passes *name*, its last argument, to :func:`serve`.
    Its stderr goes to the file *log*, which a :class:`LauncherError` for a zygote that
    failed to start quotes.
    """
    address = "\0" + name
    with log.open("wb") as stderr:
        process = subprocess.Popen(
            [*argv, name],
            env=dict(env),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
    launcher = launcher_type(address, process)
    deadline = time.monotonic() + startup_timeout
    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.connect(address)
            return launcher
        except ConnectionRefusedError:
            if process.poll() is not None or time.monotonic() > deadline:
                launcher.close()
                detail = log.read_text(errors="replace")
                raise LauncherError(
                    f"the zygote did not start; its stderr:\n{detail}", sent=False
                ) from None
            time.sleep(0.01)
