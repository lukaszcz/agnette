"""Kept reads are forgotten exactly when the own values they read are bound."""

from __future__ import annotations

import pytest

from agnette.agl.scope.read_memo import ReadMemo, ValueReads
from agnette.agl.scope.symbols import ScopePath


class _Failure(Exception):
    """A read that fails."""


class _Reads:
    """A memo of reads, each reading the own values at some paths and other keys, and its runs."""

    def __init__(self) -> None:
        self.values = ValueReads()
        self.memo: ReadMemo[str, int] = ReadMemo(self.values)
        self.runs: list[str] = []

    def get(
        self, key: str, *, paths: tuple[ScopePath, ...] = (), keys: tuple[str, ...] = ()
    ) -> int:
        """Read *key*, which reads own values at *paths* and the kept reads *keys*."""

        def read() -> int:
            self.runs.append(key)
            for path in paths:
                self.values.note(path)
            for other in keys:
                self.get(other)
            return 1

        return self.memo.get(key, read)

    def reads_run(self, plan: dict[str, tuple[ScopePath, ...]]) -> list[str]:
        """The keys of *plan*, read again with their own values, that were computed afresh."""
        self.runs.clear()
        for key, paths in plan.items():
            self.get(key, paths=paths)
        return self.runs


def test_binding_a_value_forgets_only_the_reads_of_its_path() -> None:
    reads = _Reads()
    plan: dict[str, tuple[ScopePath, ...]] = {"a": (("S", "x"),), "b": (("S", "y"),), "c": ()}
    reads.reads_run(plan)

    reads.values.bound(("S", "x"))

    assert reads.reads_run(plan) == ["a"]


def test_binding_a_value_forgets_the_reads_of_the_reads_of_its_path() -> None:
    reads = _Reads()
    reads.get("leaf", paths=(("x",),))
    reads.get("middle", keys=("leaf",))
    reads.get("top", keys=("middle",))
    reads.get("other")

    reads.values.bound(("x",))
    reads.runs.clear()
    reads.get("top", keys=("middle",))
    reads.get("middle", keys=("leaf",))
    reads.get("leaf", paths=(("x",),))
    reads.get("other")

    assert sorted(reads.runs) == ["leaf", "middle", "top"]


def test_a_read_that_fails_is_not_kept_and_binding_its_path_is_harmless() -> None:
    reads = _Reads()

    def failing() -> int:
        reads.values.note(("x",))
        raise _Failure

    with pytest.raises(_Failure):
        reads.memo.get("bad", failing)
    reads.values.bound(("x",))

    assert reads.get("bad") == 1
