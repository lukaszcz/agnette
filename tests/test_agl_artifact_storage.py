"""Unused compiler artifacts are reclaimed in the background without disturbing compilation."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agnette.agl import artifact_storage
from agnette.agl.artifact_storage import artifact_entry, read_payload, write_payload

_DAY = 24 * 60 * 60
_OLD = 60 * _DAY


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(artifact_storage, "_PRUNE_SCHEDULED", False)
    return tmp_path / "agm" / "agl"


def _store(key: bytes, payload: bytes = b"payload") -> Path:
    entry, identity = artifact_entry(key, "ir")
    write_payload(entry, identity, payload)
    return entry


def _age(path: Path, seconds: int) -> None:
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


def _settle() -> None:
    """Wait for any background prune, so assertions never race it."""
    artifact_storage.finish_prune()


def _prune_now(cache: Path) -> None:
    """Run a due prune as a fresh process's next cache write would."""
    _settle()
    (cache / artifact_storage._PRUNE_MARKER).unlink(missing_ok=True)
    artifact_storage._PRUNE_SCHEDULED = False
    _store(b"trigger")
    _settle()


def test_entries_unused_past_the_age_limit_are_reclaimed(cache: Path) -> None:
    stale = _store(b"stale")
    current = _store(b"current")
    _settle()
    _age(stale, _OLD)
    _prune_now(cache)
    assert not stale.exists()
    assert current.exists()


def test_reading_an_entry_keeps_it_alive(cache: Path) -> None:
    entry, identity = artifact_entry(b"used", "ir")
    write_payload(entry, identity, b"payload")
    _settle()
    _age(entry, _OLD)
    assert read_payload(entry, identity) == b"payload"
    _prune_now(cache)
    assert read_payload(entry, identity) == b"payload"


def test_a_recently_used_entry_is_served_without_a_timestamp_update(cache: Path) -> None:
    entry, identity = artifact_entry(b"recent", "ir")
    write_payload(entry, identity, b"payload")
    _age(entry, 60)
    before = entry.stat().st_mtime_ns
    assert read_payload(entry, identity) == b"payload"
    assert entry.stat().st_mtime_ns == before


def test_a_rejected_entry_is_not_kept_alive(cache: Path) -> None:
    entry, _identity = artifact_entry(b"foreign", "ir")
    write_payload(entry, b"\x00" * 32, b"payload")
    _settle()
    _age(entry, _OLD)
    before = entry.stat().st_mtime_ns
    assert read_payload(entry, b"\x01" * 32) is None
    assert entry.stat().st_mtime_ns == before


def test_oversized_cache_evicts_the_least_recently_used_entries(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oldest = _store(b"oldest", b"x" * 100)
    middle = _store(b"middle", b"x" * 100)
    newest = _store(b"newest", b"x" * 100)
    _settle()
    monkeypatch.setattr(artifact_storage, "_MAX_CACHE_BYTES", 250)
    _age(oldest, 3 * _DAY)
    _age(middle, 2 * _DAY)
    _age(newest, 1 * _DAY)
    _prune_now(cache)
    assert not oldest.exists()
    assert not middle.exists()
    assert newest.exists()


def test_a_size_limit_smaller_than_any_entry_empties_the_cache(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _store(b"entry")
    _settle()
    monkeypatch.setattr(artifact_storage, "_MAX_CACHE_BYTES", 0)
    _prune_now(cache)
    assert not entry.exists()
    assert [path.name for path in cache.iterdir()] == [artifact_storage._PRUNE_MARKER]


def test_orphaned_temporary_files_are_reclaimed(cache: Path) -> None:
    _store(b"anchor")
    _settle()
    orphan = cache / "tmpabcd1234"
    orphan.write_bytes(b"incomplete")
    _age(orphan, _OLD)
    _prune_now(cache)
    assert not orphan.exists()


def test_a_prune_is_not_repeated_within_the_interval(cache: Path) -> None:
    stale = _store(b"stale")
    _settle()
    _age(stale, _OLD)
    artifact_storage._PRUNE_SCHEDULED = False
    _store(b"trigger")
    _settle()
    assert stale.exists()


def test_a_prune_runs_again_after_the_interval(cache: Path) -> None:
    stale = _store(b"stale")
    _settle()
    _age(stale, _OLD)
    _age(cache / artifact_storage._PRUNE_MARKER, 2 * _DAY)
    artifact_storage._PRUNE_SCHEDULED = False
    _store(b"trigger")
    _settle()
    assert not stale.exists()


def test_a_process_starts_only_one_prune(cache: Path) -> None:
    stale = _store(b"stale")
    _settle()
    _age(stale, _OLD)
    (cache / artifact_storage._PRUNE_MARKER).unlink()
    _store(b"trigger")
    _settle()
    assert stale.exists()


def test_a_prune_does_not_block_the_writer(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()
    real_scandir = os.scandir

    def slow_scandir(path: Path) -> Iterator[os.DirEntry[str]]:
        started.set()
        release.wait(30)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", slow_scandir)
    entry, identity = artifact_entry(b"entry", "ir")
    write_payload(entry, identity, b"payload")
    assert started.wait(30)
    assert read_payload(entry, identity) == b"payload"
    assert not release.is_set()
    release.set()
    _settle()


def test_an_interrupted_prune_is_retried(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_path: Path) -> Iterator[os.DirEntry[str]]:
        raise OSError("storage went away")

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", unavailable)
        stale = _store(b"stale")
        _settle()
    assert not (cache / artifact_storage._PRUNE_MARKER).exists()
    _age(stale, _OLD)
    artifact_storage._PRUNE_SCHEDULED = False
    _store(b"trigger")
    _settle()
    assert not stale.exists()


def test_failing_to_start_the_prune_leaves_the_write_intact(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_thread: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    entry, identity = artifact_entry(b"entry", "ir")
    write_payload(entry, identity, b"payload")
    assert read_payload(entry, identity) == b"payload"


def test_an_undeletable_entry_does_not_stop_the_prune(cache: Path) -> None:
    stale = _store(b"stale")
    _settle()
    undeletable = cache / "directory"
    undeletable.mkdir()
    _age(undeletable, 2 * _OLD)
    _age(stale, _OLD)
    _prune_now(cache)
    assert undeletable.exists()
    assert not stale.exists()
