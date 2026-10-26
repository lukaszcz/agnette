"""Process parallelism for ``std/ps``, over the host session's process host."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Protocol

from agl import (
    AglException,
    ProcessHostError,
    TransferError,
    nominals,
    option_none,
    option_some,
    runtime,
    transfer,
)

from agnette.agl.runtime.boundary import current_descriptors

ProcessError = nominals.std.ps.ProcessError
ProcessExitError = nominals.std.ps.ProcessExitError
Process = nominals.std.ps.Process

# The ``runtime.state`` key of the decoded outcomes, by process id. A waited-for process stays
# retained: the host keeps its raw payload and this cache the decoded result for the host
# session, and only `detach` releases them.
_OUTCOMES_KEY = "std/ps/outcomes"


class _Process(Protocol):
    """A ``Process`` handle."""

    @property
    def id(self) -> str: ...

    @property
    def pid(self) -> int: ...


@contextmanager
def _process_errors() -> Iterator[None]:
    """Report host misuse and untransferable results as `ProcessError`."""
    try:
        yield
    except (ProcessHostError, TransferError) as error:
        raise AglException(ProcessError(message=str(error))) from error


def _summary(carrier: AglException) -> str:
    """The exception a child raised, as ``Type: message``."""
    descriptor = current_descriptors().nominals[carrier.value.nominal]
    return f"{descriptor.display_name}: {carrier}"


def _deliver(f: Callable[[], object]) -> object:
    """The child's callable: *f*'s value as bytes, or its AgL exception marked as a failure."""
    try:
        return transfer.dumps(f())
    except AglException as raised:
        return runtime.processes.Delivered(transfer.dumps(raised), failure=_summary(raised))


def fork(f: Callable[[], object]) -> object:
    """Run *f* in a forked child; see `ps::fork`."""
    with _process_errors():
        info = runtime.processes.fork(lambda: _deliver(f), ())
    runtime.trace("fork", {"child_pid": info.pid})
    return Process(id=info.id, pid=info.pid)


def _exit_error(process: _Process, text: str, code: int | None, number: int | None) -> AglException:
    """The `ProcessExitError` carrier for a child that delivered no outcome."""
    pid = process.pid
    return AglException(
        ProcessExitError(
            message=f"process {pid} {text}",
            pid=pid,
            **{
                "exit-code": option_none() if code is None else option_some(code),
                "signal": option_none() if number is None else option_some(number),
            },
        )
    )


def _decode(process: _Process, outcome: object) -> object:
    """What *outcome* means for `wait`: the value, or the carrier to raise."""
    host = runtime.processes
    if isinstance(outcome, host.Delivered):
        return transfer.loads(outcome.payload)
    if isinstance(outcome, host.Failed):
        return _exit_error(process, f"failed: {outcome.message}", None, None)
    if outcome.signal is not None:
        text = f"was terminated by signal {outcome.signal}"
        return _exit_error(process, text, None, outcome.signal)
    return _exit_error(process, f"exited with status {outcome.exit_code}", outcome.exit_code, None)


def wait(p: _Process) -> object:
    """Wait for *p* and return its result or raise its exception; see `ps::wait`."""
    with _process_errors():
        outcome = runtime.processes.outcome(p.id)
        decoded: dict[str, object] = runtime.state(_OUTCOMES_KEY, dict)
        if p.id not in decoded:
            decoded[p.id] = _decode(p, outcome)
            raised = isinstance(decoded[p.id], AglException)
            runtime.trace("exit", {"child_pid": p.pid, "raised": raised})
        result = decoded[p.id]
    if isinstance(result, AglException):
        # A fresh carrier each time: the evaluator annotates the one it propagates.
        raise AglException(result.value, span=result.span)
    return result


def settle(p: _Process) -> None:
    """Wait for *p* to finish; see `ps::settle`."""
    with _process_errors():
        runtime.processes.settle(p.id)


def settle_any(ps: Sequence[_Process]) -> int:
    """Wait until one of *ps* has finished and return the lowest finished index."""
    with _process_errors():
        return runtime.processes.settle_any([p.id for p in ps])


def terminate(p: _Process) -> None:
    """Terminate *p* and wait for it to end; see `ps::terminate`."""
    with _process_errors():
        runtime.processes.terminate(p.id)


def terminate_all(ps: Sequence[_Process]) -> None:
    """Terminate *ps* under one grace period and wait for them; see `ps::WaitInternals`."""
    with _process_errors():
        runtime.processes.terminate(*(p.id for p in ps))


def detach(p: _Process) -> None:
    """Release *p*; see `ps::detach`."""
    with _process_errors():
        runtime.processes.detach(p.id)
    runtime.state(_OUTCOMES_KEY, dict).pop(p.id, None)


__all__ = ["detach", "fork", "settle", "settle_any", "terminate", "terminate_all", "wait"]
