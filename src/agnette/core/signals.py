"""Signal primitives: race-free interruptible polls and held-signal critical sections."""

from __future__ import annotations

import os
import select
import signal
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Self

__all__ = ["SignalMask", "SignalWakeup", "held_signals"]

SignalMask = set[int | signal.Signals]
_DRAIN_CHUNK = 512


@contextmanager
def held_signals(numbers: Iterable[signal.Signals]) -> Iterator[SignalMask]:
    """Block *numbers* in the calling thread; yield the previous mask, restored on exit.

    A blocked signal stays pending and is delivered when the mask is restored. Python runs
    pending handlers as ``pthread_sigmask`` returns, so one may raise just after the block
    or the restore; the mask is restored regardless.
    """
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
        yield previous
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


class SignalWakeup:
    """Makes a blocking ``poll`` return on any signal, even one that arrives just before it.

    Python runs signal handlers only between bytecodes, so a signal landing after the last
    check but before ``poll`` enters the kernel would leave ``poll`` blocked, handler pending.
    The signal's C handler also writes to a self-pipe (``signal.set_wakeup_fd``); registering
    :attr:`fd` for ``POLLIN`` makes such a ``poll`` return, after which Python-level handlers
    run (a raising one propagates). A handler that does not raise leaves the wait to resume.

    Bytes the pipe receives are forwarded to the wakeup fd that was installed before (e.g.
    an event loop's). Only the main thread can install a wakeup fd: elsewhere :attr:`fd` is
    ``None`` and the poll behaves as without it. Nothing persists past the ``with`` block,
    so enter it only after any ``os.fork``; a forked child must enter its own.
    """

    def __init__(self) -> None:
        self.fd: int | None = None
        self._write_fd = -1
        self._previous = -1

    def __enter__(self) -> Self:
        if threading.current_thread() is not threading.main_thread():
            return self
        try:
            with held_signals(signal.valid_signals()):
                self._install()
        except BaseException:
            # A handler that raised as the signals were unblocked, with the wakeup installed.
            self._uninstall()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._uninstall()

    def _install(self) -> None:
        read_fd, self._write_fd = os.pipe()
        self.fd = read_fd
        try:
            os.set_blocking(read_fd, False)
            os.set_blocking(self._write_fd, False)
            self._previous = signal.set_wakeup_fd(self._write_fd, warn_on_full_buffer=False)
        except BaseException:
            self._close()
            raise

    def _uninstall(self) -> None:
        """Restore the previous wakeup fd, forward what is left, and close the pipe.

        Signals are held, so no handler runs between the steps and leaks the pipe; if a
        handler raises as they are held, the steps run unheld.
        """
        if self.fd is None:
            return
        try:
            with held_signals(signal.valid_signals()):
                self._release()
        finally:
            self._release()

    def _release(self) -> None:
        """The steps of :meth:`_uninstall`; a no-op once done."""
        if self.fd is None:
            return
        try:
            signal.set_wakeup_fd(self._previous)
            self._drain()
        finally:
            self._close()

    def register(self, poller: select.poll) -> None:
        """Add the pipe to *poller* (nothing without a wakeup fd)."""
        if self.fd is not None:
            poller.register(self.fd, select.POLLIN)

    def consume(self, fd: int) -> bool:
        """Drain the pipe if *fd* is it; whether it was."""
        if fd != self.fd:
            return False
        self._drain()
        return True

    def _drain(self) -> None:
        assert self.fd is not None
        while True:
            try:
                data = os.read(self.fd, _DRAIN_CHUNK)
            except BlockingIOError:
                return
            if self._previous >= 0:
                try:
                    os.write(self._previous, data)
                except OSError:
                    # A full or closed pipe of the previous owner must not break this wait.
                    pass

    def _close(self) -> None:
        assert self.fd is not None
        try:
            os.close(self.fd)
        finally:
            os.close(self._write_fd)
            self.fd = None
