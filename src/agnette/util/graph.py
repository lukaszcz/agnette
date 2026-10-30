"""Pure, stdlib-only graph algorithms shared by AgM and AgL.

Provides:
- ``GraphCycleError`` — raised by :func:`toposort` when a cycle is detected.
- ``bfs_first`` — deterministic breadth-first search for the nearest hit.
- ``reachable_nodes`` — depth-first pre-order of the nodes reachable from a start node.
- ``sccs`` — Tarjan's strongly-connected-components (reverse topological order).
- ``toposort`` — Kahn's topological sort (leaves first, deterministic).
"""

from __future__ import annotations

import bisect
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from _typeshed import SupportsRichComparison

    K = TypeVar("K", bound=SupportsRichComparison)
else:
    K = TypeVar("K")

T = TypeVar("T")
R = TypeVar("R")

__all__ = ["GraphCycleError", "bfs_first", "reachable_nodes", "sccs", "toposort"]


def bfs_first(
    seeds: Iterable[T],
    successors: Callable[[T], Iterable[T]],
    select: Callable[[T], R | None],
    *,
    key: Callable[[T], K],
) -> R | None:
    """Return the first non-``None`` ``select(node)`` reached, or ``None``.

    Breadth-first from *seeds*, visiting each node at most once, so the
    SHALLOWEST node satisfying *select* wins — which is what makes this
    useful for "name the culprit" diagnostics, where a node named directly by
    the query is a better explanation than one buried several hops away.

    Parameters
    ----------
    seeds:
        Starting nodes, explored in the order given (their own order is the
        caller's to choose and is not re-sorted).
    successors:
        Yields the direct successors of a node.
    select:
        Applied to each visited node; the first non-``None`` result is
        returned immediately and the search stops.
    key:
        Sort key applied to each hop's newly discovered successors, so the
        traversal — and therefore the reported result — is deterministic.
    """
    seen: set[T] = set()
    queue: deque[T] = deque(seeds)
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        found = select(node)
        if found is not None:
            return found
        queue.extend(sorted((s for s in successors(node) if s not in seen), key=key))
    return None


class GraphCycleError(Exception):
    """Raised by :func:`toposort` when the dependency graph contains a cycle.

    ``cycle`` is the set of nodes that could not be ordered (the participants
    in the cycle, expressed as ``set[object]`` so no TypeVar threading through
    the exception class is required).
    """

    def __init__(self, cycle: set[object]) -> None:
        self.cycle = cycle
        super().__init__(f"cycle detected among {len(cycle)} node(s)")


def reachable_nodes(start: T, successors: Callable[[T], Iterable[T]]) -> tuple[T, ...]:
    """Return *start* and every node reachable from it, depth-first in successor order."""
    seen: set[T] = set()
    order: list[T] = []
    pending = [start]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        order.append(current)
        pending.extend(reversed(tuple(successors(current))))
    return tuple(order)


def sccs(
    adj: Mapping[T, Iterable[T]],
    *,
    key: Callable[[T], K],
) -> tuple[tuple[T, ...], ...]:
    """Return the strongly-connected components of *adj* in reverse topological order.

    Uses Tarjan's algorithm.  Each SCC's members are sorted by *key*; nodes are
    visited in *key*-sorted order so the result is fully deterministic.

    Parameters
    ----------
    adj:
        Adjacency mapping: ``adj[node]`` yields the direct successors of *node*.
        Nodes absent from *adj* are treated as having no outgoing edges.
    key:
        Sort key applied to nodes for deterministic ordering within each SCC and
        for the outer iteration order.

    Returns
    -------
    tuple[tuple[T, ...], ...]
        SCCs in **reverse topological order** (sinks first, roots last).  Within
        each SCC the members are sorted by *key*.
    """
    traversal = _SccTraversal(adj, key)
    nodes = sorted(adj, key=key)
    for node in nodes:
        if node not in traversal.index:
            traversal.strongconnect(node)
    return tuple(traversal.result)


class _SccTraversal(Generic[T, K]):
    """Tarjan state with no self-capturing closure retaining the borrowed graph/key."""

    def __init__(self, adj: Mapping[T, Iterable[T]], key: Callable[[T], K]) -> None:
        self.adj = adj
        self.key = key
        self.index_counter = 0
        self.stack: list[T] = []
        self.on_stack: set[T] = set()
        self.index: dict[T, int] = {}
        self.lowlink: dict[T, int] = {}
        self.result: list[tuple[T, ...]] = []

    def strongconnect(self, node: T) -> None:
        idx = self.index_counter
        self.index[node] = idx
        self.lowlink[node] = idx
        self.index_counter += 1
        self.stack.append(node)
        self.on_stack.add(node)

        if node in self.adj:
            for neighbour in self.adj[node]:
                if neighbour not in self.index:
                    self.strongconnect(neighbour)
                    self.lowlink[node] = min(self.lowlink[node], self.lowlink[neighbour])
                elif neighbour in self.on_stack:
                    self.lowlink[node] = min(self.lowlink[node], self.index[neighbour])

        if self.lowlink[node] == self.index[node]:
            scc: list[T] = []
            while True:
                w = self.stack.pop()
                self.on_stack.discard(w)
                scc.append(w)
                if w == node:
                    break
            scc.sort(key=self.key)
            self.result.append(tuple(scc))


def toposort(
    nodes: Iterable[T],
    deps: Mapping[T, Iterable[T]],
    *,
    key: Callable[[T], K],
) -> list[T]:
    """Return *nodes* in topological order (leaves first) via Kahn's algorithm.

    ``deps[n]`` is the collection of nodes that *n* **depends on** (must appear
    before *n* in the output).  Ties between ready nodes are broken by *key* for
    determinism.

    Parameters
    ----------
    nodes:
        All nodes to be sorted.  Must be unique (no duplicates); duplicates
        would make the cycle check (ordered-count vs node-count) spurious.
    deps:
        Dependency mapping: ``deps[n]`` yields the nodes that *n* depends on.
        Nodes absent from *deps* are treated as having no dependencies.
    key:
        Sort key for deterministic tie-breaking in the ready queue.

    Returns
    -------
    list[T]
        All *nodes* ordered so each node appears after all its dependencies.

    Raises
    ------
    GraphCycleError
        When the graph contains a cycle.  ``exc.cycle`` is the set of nodes
        that could not be placed (the cycle participants).
    """
    all_nodes = list(nodes)

    # Build in-degree and reverse-adjacency (adj[u] = nodes that depend on u).
    in_degree: dict[T, int] = {k: 0 for k in all_nodes}
    adj: dict[T, list[T]] = {k: [] for k in all_nodes}

    for node, node_deps in deps.items():
        for dep in node_deps:
            adj[dep].append(node)
            in_degree[node] = in_degree.get(node, 0) + 1

    # Kahn's: start with all zero-in-degree nodes, sorted for determinism.
    ready: list[T] = sorted(
        (k for k, d in in_degree.items() if d == 0),
        key=key,
    )
    order: list[T] = []

    while ready:
        node = ready.pop(0)
        order.append(node)
        dependents = sorted(adj.get(node, []), key=key)
        for dep in dependents:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                bisect.insort(ready, dep, key=key)

    if len(order) < len(all_nodes):
        done = set(order)
        remaining: set[object] = set()
        for k in all_nodes:
            if k not in done:
                remaining.add(k)
        raise GraphCycleError(remaining)

    return order
