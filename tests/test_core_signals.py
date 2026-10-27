"""Signal primitives: wakeup of blocking polls, forwarding, and held-signal sections."""

from __future__ import annotations

import itertools
import os
import select
import signal
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from unittest import mock

import pytest

from agnette.core.signals import SignalWakeup, held_signals
from tests._proc_helpers import open_fd_count
from tests._signal_helpers import signal_handler, trip_signal_off_thread
from tests._timeouts import fail_if_slow

_USR1 = signal.SIGUSR1


@contextmanager
def _previous_wakeup() -> Iterator[tuple[int, int]]:
    """Install a non-blocking pipe as the process's wakeup fd: ``(read, write)``."""
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    signal.set_wakeup_fd(write_fd)
    try:
        yield read_fd, write_fd
    finally:
        assert signal.set_wakeup_fd(-1) == write_fd
        os.close(read_fd)
        os.close(write_fd)


def test_a_signal_that_did_not_interrupt_the_poll_makes_the_pipe_readable() -> None:
    seen: list[int] = []
    poller = select.poll()
    with (
        signal_handler(_USR1, lambda number, frame: seen.append(number)),
        SignalWakeup() as wakeup,
    ):
        trip_signal_off_thread(_USR1)
        with fail_if_slow("the poll slept through a signal", seconds=10):
            ready = wakeup.poll(poller)
        assert wakeup.fd is not None
        assert ready == []
        assert poller.poll(0) == []
        assert wakeup.poll(poller, time.monotonic()) is None

    assert seen == [_USR1]


def test_bytes_are_forwarded_to_the_previous_wakeup_fd_and_it_is_restored() -> None:
    with _previous_wakeup() as (previous_read, _), signal_handler(_USR1, lambda n, f: None):
        fds = open_fd_count()
        with SignalWakeup() as wakeup:
            trip_signal_off_thread(_USR1)
            assert wakeup.poll(select.poll()) == []
            assert os.read(previous_read, 16) == bytes([_USR1])
            # A signal that no wait consumed still reaches the previous owner at exit.
            trip_signal_off_thread(_USR1)
        assert os.read(previous_read, 16) == bytes([_USR1])
        assert open_fd_count() == fds


def test_a_full_previous_wakeup_fd_does_not_break_the_wait() -> None:
    with _previous_wakeup() as (_, previous_write), signal_handler(_USR1, lambda n, f: None):
        with pytest.raises(BlockingIOError):
            while True:
                os.write(previous_write, bytes(4096))
        with SignalWakeup() as wakeup:
            trip_signal_off_thread(_USR1)
            assert wakeup.poll(select.poll()) == []


def test_nothing_remains_after_an_interrupted_wait_or_a_failed_setup() -> None:
    def interrupt(number: int, frame: object) -> None:
        raise KeyboardInterrupt

    fds = open_fd_count()
    with signal_handler(_USR1, interrupt), pytest.raises(KeyboardInterrupt), SignalWakeup():
        trip_signal_off_thread(_USR1)
    with (
        mock.patch.object(signal, "set_wakeup_fd", side_effect=ValueError),
        pytest.raises(ValueError),
        SignalWakeup(),
    ):
        pass

    assert signal.set_wakeup_fd(-1) == -1
    assert open_fd_count() == fds


def test_an_interrupt_as_the_wakeup_is_installed_leaves_nothing_behind() -> None:
    real = signal.set_wakeup_fd

    def install_then_signal(fd: int, *, warn_on_full_buffer: bool = True) -> int:
        previous = real(fd, warn_on_full_buffer=warn_on_full_buffer)
        if not warn_on_full_buffer:
            # Held (pending) while the wakeup is being installed, delivered when unblocked.
            signal.pthread_kill(threading.get_ident(), _USR1)
        return previous

    def interrupt(number: int, frame: object) -> None:
        raise KeyboardInterrupt

    fds = open_fd_count()
    with (
        signal_handler(_USR1, interrupt),
        mock.patch.object(signal, "set_wakeup_fd", install_then_signal),
        pytest.raises(KeyboardInterrupt),
        SignalWakeup(),
    ):
        pass

    assert real(-1) == -1
    assert open_fd_count() == fds


def test_outside_the_main_thread_the_wait_proceeds_without_a_wakeup_fd() -> None:
    report: list[object] = []

    def run() -> None:
        poller = select.poll()
        with SignalWakeup() as wakeup:
            report.extend([wakeup.fd, wakeup.poll(poller, time.monotonic() + 0.01)])

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()

    assert report == [None, []]
    assert signal.set_wakeup_fd(-1) == -1


def test_held_signals_stay_pending_until_the_mask_is_restored() -> None:
    seen: list[int] = []
    with signal_handler(_USR1, lambda number, frame: seen.append(number)):
        with held_signals([_USR1]) as previous:
            signal.pthread_kill(threading.get_ident(), _USR1)
            assert _USR1 in signal.pthread_sigmask(signal.SIG_BLOCK, [])
            assert _USR1 not in previous
            assert seen == []
        assert seen == [_USR1]
        assert _USR1 not in signal.pthread_sigmask(signal.SIG_BLOCK, [])


@contextmanager
def _handler_raises_after_blocking(call: int) -> Iterator[None]:
    """Make the *call*-th blocking ``pthread_sigmask`` raise once it has set the mask.

    Python runs pending handlers as that call returns, and one may raise.
    """
    real = signal.pthread_sigmask
    blocks = itertools.count()

    def pthread_sigmask(how: int, mask: Iterable[int]) -> set[signal.Signals]:
        previous = real(how, mask)
        if how == signal.SIG_BLOCK and list(mask) and next(blocks) == call:
            raise KeyboardInterrupt
        return previous

    with mock.patch.object(signal, "pthread_sigmask", pthread_sigmask):
        yield


def test_held_signals_are_released_if_a_handler_raises_as_they_are_blocked() -> None:
    with _handler_raises_after_blocking(0), pytest.raises(KeyboardInterrupt), held_signals([_USR1]):
        pass

    assert _USR1 not in signal.pthread_sigmask(signal.SIG_BLOCK, [])


def test_the_wakeup_pipe_is_released_if_a_handler_raises_as_signals_are_held_to_close_it() -> None:
    fds = open_fd_count()
    with _handler_raises_after_blocking(1), pytest.raises(KeyboardInterrupt), SignalWakeup():
        pass

    assert signal.set_wakeup_fd(-1) == -1
    assert open_fd_count() == fds
    assert signal.SIGINT not in signal.pthread_sigmask(signal.SIG_BLOCK, [])
