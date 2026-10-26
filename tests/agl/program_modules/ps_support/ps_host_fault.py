"""Companion of ``ps_host_fault.agl``."""

from agl import runtime


def _fail_to_close(state: object) -> None:
    raise RuntimeError("close failed")


def fail_at_exit() -> int:
    runtime.state("ps/host-fault", object, close=_fail_to_close)
    return 0
