"""Deterministic signal timing for tests of interruptible waits."""

from __future__ import annotations

import select
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from unittest import mock


def trip_signal_off_thread(number: signal.Signals) -> None:
    """Deliver *number* to a helper thread, which has finished when this returns.

    Python's C-level handler runs there (and writes the wakeup fd) while the main thread's
    blocking system call is not interrupted: the signal "arrived just before the call".
    """
    thread = threading.Thread(target=lambda: signal.pthread_kill(threading.get_ident(), number))
    thread.start()
    thread.join()


class _TracedPoll:
    """A ``select.poll`` object that runs *before(call_index)* ahead of each ``poll``."""

    def __init__(self, real: select.poll, before: Callable[[int], None], calls: list[None]) -> None:
        self._real = real
        self._before = before
        self._calls = calls

    def register(self, fd: int, events: int) -> None:
        self._real.register(fd, events)

    def unregister(self, fd: int) -> None:
        self._real.unregister(fd)

    def poll(self, timeout: int | None = None) -> list[tuple[int, int]]:
        self._before(len(self._calls))
        self._calls.append(None)
        return self._real.poll(timeout)


@contextmanager
def before_each_poll(before: Callable[[int], None]) -> Iterator[None]:
    """Run *before(call_index)* ahead of every ``poll`` of every ``select.poll`` object."""
    calls: list[None] = []
    real_poll = select.poll
    with mock.patch.object(select, "poll", lambda: _TracedPoll(real_poll(), before, calls)):
        yield


@contextmanager
def signal_handler(
    number: signal.Signals, handler: Callable[[int, object], None]
) -> Iterator[None]:
    """Install *handler* for *number* (main thread only) and restore the previous one."""
    previous = signal.signal(number, handler)
    try:
        yield
    finally:
        signal.signal(number, previous)
