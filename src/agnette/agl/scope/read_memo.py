"""Kept path reads, forgotten when the module binds an own value they read."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from typing import Protocol

from agnette.agl.scope.symbols import ScopePath


class KeptTable(Protocol):
    """Where results are kept by key."""

    def forget(self, key: Hashable) -> None:
        """Forget the result kept under *key*, if any."""
        ...


class KeptRead:
    """A result kept under *key* in *table*, tracked while it depends on own values.

    ``sensitive`` once it, or a kept result it read, read an own value;
    ``parents`` are the sensitive results that read it.
    """

    __slots__ = ("alive", "key", "parents", "sensitive", "table")

    def __init__(self, table: KeptTable, key: Hashable) -> None:
        self.table = table
        self.key = key
        self.alive = True
        self.sensitive = False
        self.parents: list[KeptRead] = []


class _Frame:
    """What one computation read: own value paths, and the sensitive kept results."""

    __slots__ = ("children", "paths")

    def __init__(self) -> None:
        self.paths: set[ScopePath] = set()
        self.children: set[KeptRead] = set()


class ValueReads:
    """Tracks which kept results read which own values.

    A script or REPL entry binds its root and scoped values only where its
    walk reaches them. Binding a value forgets exactly the kept results that
    read it, directly or through another kept result.
    """

    def __init__(self) -> None:
        self._frames: list[_Frame] = []
        self._readers: dict[ScopePath, list[KeptRead]] = {}

    def note(self, path: ScopePath) -> None:
        """Record that the computation in progress read the own value at *path*."""
        if self._frames:
            self._frames[-1].paths.add(path)

    @contextmanager
    def computing(self, kept: KeptRead) -> Iterator[None]:
        """Track what the computation of, or an extension to, *kept* reads."""
        frame = _Frame()
        self._frames.append(frame)
        try:
            yield
        finally:
            self._frames.pop()
            if frame.paths or frame.children:
                kept.sensitive = True
                for path in frame.paths:
                    self._readers.setdefault(path, []).append(kept)
                for child in frame.children:
                    child.parents.append(kept)
            self.hit(kept)

    def hit(self, kept: KeptRead) -> None:
        """Record that the computation in progress read *kept*."""
        if kept.sensitive and self._frames:
            self._frames[-1].children.add(kept)

    def bound(self, path: ScopePath) -> None:
        """Forget the kept results that read the own value at *path*, now bound."""
        pending = self._readers.pop(path, [])
        while pending:
            kept = pending.pop()
            if kept.alive:
                kept.alive = False
                kept.table.forget(kept.key)
                pending.extend(kept.parents)


class _Kept[V](KeptRead):
    """A kept result, *value* once computed."""

    __slots__ = ("value",)
    value: V


class ReadMemo[K: Hashable, V]:
    """What reads computed, by key, tracked for the own values they read."""

    def __init__(self, reads: ValueReads) -> None:
        self._reads = reads
        self._table: dict[Hashable, _Kept[V]] = {}

    def forget(self, key: Hashable) -> None:
        """Forget what is kept under *key*."""
        self._table.pop(key, None)

    def get(self, key: K, read: Callable[[], V]) -> V:
        """Return what *read* reads, kept under *key*."""
        kept = self._table.get(key)
        if kept is not None:
            self._reads.hit(kept)
            return kept.value
        kept = _Kept(self, key)
        with self._reads.computing(kept):
            kept.value = read()
        self._table[key] = kept
        return kept.value
