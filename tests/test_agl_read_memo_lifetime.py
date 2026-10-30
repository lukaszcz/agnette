"""Memo ownership does not extend table lifetime or shorten dependency lifetime."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from weakref import ref

import pytest

from agnette.agl.modules.ids import ModuleId
from agnette.agl.modules.loader import build_repl_graph, parse_entry_module
from agnette.agl.modules.roots import RootSet
from agnette.agl.scope.lookup import LookupKind, Reading
from agnette.agl.scope.program import resolve_program
from agnette.agl.scope.read_memo import ReadMemo, ValueReads
from agnette.agl.scope.sources import ModuleSources
from agnette.agl.scope.symbols import ScopePath

pytestmark = pytest.mark.usefixtures("no_cyclic_gc")


class _Value:
    """A weak-referenceable result."""


class _Source:
    """A callback owner retained by a memoized result."""

    def read(self) -> int:
        return 7


def test_discarding_an_insensitive_memo_releases_its_results() -> None:
    reads = ValueReads()
    memo = ReadMemo[str, _Value](reads)
    value = ref(memo.get("result", _Value))
    table = ref(memo)

    del memo

    assert table() is None
    assert value() is None


def test_a_subscription_does_not_keep_its_memo_alive() -> None:
    reads = ValueReads()
    memo = ReadMemo[str, _Value](reads)

    def compute() -> _Value:
        reads.note(("x",))
        return _Value()

    value = ref(memo.get("result", compute))
    table = ref(memo)
    del memo

    assert table() is None
    # Subscriptions still own their results until invalidation.
    assert value() is not None
    reads.bound(("x",))
    assert value() is None


def test_stale_subscriptions_do_not_retain_a_discarded_memo() -> None:
    reads = ValueReads()
    memo = ReadMemo[str, _Value](reads)

    def compute() -> _Value:
        reads.note(("x",))
        reads.note(("y",))
        return _Value()

    memo.get("result", compute)
    reads.bound(("x",))
    table = ref(memo)
    del memo

    assert table() is None
    reads.bound(("y",))


def test_transitive_invalidation_crosses_a_discarded_child_table() -> None:
    reads = ValueReads()
    parent = ReadMemo[str, int](reads)
    current = 1

    def leaf() -> int:
        reads.note(("x",))
        return current

    def subscribe() -> tuple[ref[ReadMemo[str, int]], ref[ReadMemo[str, int]]]:
        child = ReadMemo[str, int](reads)
        middle = ReadMemo[str, int](reads)
        child.get("leaf", leaf)
        middle.get("middle", lambda: child.get("leaf", leaf))
        assert parent.get("parent", lambda: middle.get("middle", lambda: 0)) == 1
        return ref(child), ref(middle)

    child_table, middle_table = subscribe()

    assert child_table() is None
    assert middle_table() is None
    current = 2
    reads.bound(("x",))
    assert parent.get("parent", leaf) == 2


def test_cached_callbacks_keep_their_source_alive() -> None:
    reads = ValueReads()
    callbacks = ReadMemo[str, Callable[[], int]](reads)

    def cache_callback() -> tuple[Callable[[], int], ref[_Source]]:
        memo = ReadMemo[str, _Source](reads)
        source = _Source()

        def compute() -> _Source:
            reads.note(("x",))
            return source

        memo.get("source", compute)
        callback = callbacks.get("callback", lambda: memo.get("source", compute).read)
        return callback, ref(source)

    callback, owner = cache_callback()

    assert owner() is not None
    assert callback() == 7
    reads.bound(("x",))
    assert callback() == 7
    del callback
    assert owner() is None
    assert callbacks.get("callback", lambda: lambda: 9)() == 9


def test_a_retained_source_callback_can_still_read_imports(tmp_path: Path) -> None:
    def retain() -> tuple[
        Callable[[ScopePath, ScopePath, LookupKind], Reading], ref[ModuleSources]
    ]:
        (tmp_path / "lib.agl").write_text(
            "scope Box\n  def f() -> int = 1\n  def g() -> int = 2\nend Box"
        )
        text = "import lib\nuse lib::Box::*\nf()"
        parsed = parse_entry_module(text, entry_path=None, inline_code=True)
        graph, _, _ = build_repl_graph(
            parsed.program,
            parsed.next_id,
            path=None,
            cached={},
            roots=RootSet(frozenset({tmp_path})),
            default_stdlib=False,
            source_text=text,
        )
        resolved = resolve_program(graph)
        sources = resolved.sources(graph.entry_id)
        # Populate the memo before its program owner disappears.
        assert sources.contributed_at((), ("f",), LookupKind.VALUE).candidates
        return sources.contributed_at, ref(sources)

    callback, sources = retain()

    assert sources() is not None
    found = callback((), ("g",), LookupKind.VALUE)
    assert {candidate.target.key for candidate in found.candidates} == {
        (ModuleId.from_path("lib"), ("Box",), "g")
    }
    del callback
    assert sources() is None


def test_old_subscriptions_do_not_invalidate_a_fresh_result() -> None:
    reads = ValueReads()
    memo = ReadMemo[str, int](reads)

    def old() -> int:
        reads.note(("x",))
        reads.note(("y",))
        return 1

    def fresh() -> int:
        reads.note(("z",))
        return 2

    assert memo.get("result", old) == 1
    reads.bound(("x",))
    assert memo.get("result", fresh) == 2
    reads.bound(("y",))
    assert memo.get("result", lambda: 3) == 2
    reads.bound(("z",))
    assert memo.get("result", lambda: 3) == 3


def test_distinct_horizons_keep_separate_results_and_dependencies() -> None:
    reads = ValueReads()
    memo = ReadMemo[tuple[int | None, str], int](reads)

    def before() -> int:
        reads.note(("before",))
        return 1

    def after() -> int:
        reads.note(("after",))
        return 2

    assert memo.get((10, "name"), before) == 1
    assert memo.get((None, "name"), after) == 2
    reads.bound(("before",))
    assert memo.get((10, "name"), lambda: 3) == 3
    assert memo.get((None, "name"), lambda: 4) == 2


def test_a_failed_read_does_not_retain_its_table() -> None:
    reads = ValueReads()
    memo = ReadMemo[str, int](reads)

    def failing() -> int:
        reads.note(("x",))
        raise ValueError

    with pytest.raises(ValueError):
        memo.get("result", failing)
    table = ref(memo)
    del memo

    assert table() is None
    reads.bound(("x",))
