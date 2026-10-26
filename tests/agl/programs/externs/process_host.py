import os

from agl import ProcessHostError, runtime


def _state() -> str:
    return runtime.state(
        "process-host", lambda: "parent", _closed, fork=lambda value: f"{value}>child"
    )


def _closed(value: str) -> None:
    os.write(1, f"closed {value}\n".encode())


def forked_double(n):
    """Fork a child that doubles *n* and wait for its delivered bytes."""
    host = runtime.processes
    outcome = host.outcome(host.fork(lambda: str(n * 2).encode(), ()).id)
    assert isinstance(outcome, host.Delivered)
    return int(outcome.payload)


def forked_state():
    """Report the child's view of the companion state; the child closes it at exit."""
    _state()
    host = runtime.processes
    outcome = host.outcome(host.fork(lambda: _state().encode(), ()).id)
    try:
        host.settle("unknown")
    except ProcessHostError:
        return outcome.payload.decode()
    raise AssertionError("an unknown process id was accepted")
