"""Atomic, versioned compiler-artifact storage envelope.

Locates a slot for a caller-chosen key under the disposable AgL cache, keys
identity by compiler digest plus that key and an optional validator, and
reads/writes payloads atomically. Reclaims unused entries on a background
thread, so a prune never delays compilation; :func:`finish_prune` awaits it, as
a fork must. Shared by the AgL disk caches;
imports no other Agnette modules.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from contextlib import suppress
from importlib.metadata import version
from pathlib import Path
from tempfile import NamedTemporaryFile

_COMPILER_DIGEST: bytes | None = None
_PRUNE_SCHEDULED = False
_PRUNE_THREAD: threading.Thread | None = None

_DAY_SECONDS = 24 * 60 * 60
_MAX_ENTRY_AGE_SECONDS = 30 * _DAY_SECONDS
_MAX_CACHE_BYTES = 256 * 1024 * 1024
_PRUNE_INTERVAL_SECONDS = _DAY_SECONDS
_RECENCY_RESOLUTION_SECONDS = _DAY_SECONDS
_PRUNE_MARKER = ".last-prune"
_PRUNE_THREAD_NAME = "agl-cache-prune"


def _compiler_digest() -> bytes:
    """Invalidate artifacts when Agnette code, grammar, Python, or parser dependencies change."""
    global _COMPILER_DIGEST
    if _COMPILER_DIGEST is not None:
        return _COMPILER_DIGEST
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256((sys.version + version("lark") + version("immutables")).encode())
    for path in sorted(root.rglob("*")):
        if path.suffix in {".py", ".lark"}:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    _COMPILER_DIGEST = digest.digest()
    return _COMPILER_DIGEST


def artifact_entry(key: bytes, kind: str, *, validator: bytes = b"") -> tuple[Path, bytes]:
    """Locate a versioned compiler artifact in the user's disposable cache.

    *validator* extends the identity without moving the slot, for a caller
    whose reuse condition is not fully captured by *key* alone.
    """
    cache_home = os.environ.get("XDG_CACHE_HOME")
    root = Path(cache_home) if cache_home else Path.home() / ".cache"
    slot = hashlib.sha256(key).hexdigest()
    identity = hashlib.sha256(_compiler_digest() + key + validator).digest()
    return root / "agm" / "agl" / (slot + "." + kind), identity


def read_payload(entry: Path, identity: bytes) -> bytes | None:
    """Read a complete, compatible payload, or report a cache miss."""
    try:
        data = entry.read_bytes()
        payload = data[64:]
        if data[:32] != identity or data[32:64] != hashlib.sha256(payload).digest():
            return None
    except OSError:
        return None
    _mark_used(entry)
    return payload


def write_payload(entry: Path, identity: bytes, payload: bytes) -> None:
    """Publish a complete payload atomically; unavailable storage is harmless."""
    temporary: Path | None = None
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=entry.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(identity + hashlib.sha256(payload).digest() + payload)
        temporary.replace(entry)
        _schedule_prune(entry.parent)
    except OSError:
        pass
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _mark_used(entry: Path) -> None:
    """Refresh *entry*'s mtime, the recency a prune reads; atime is unreliable across mounts."""
    with suppress(OSError):
        if time.time() - entry.stat().st_mtime >= _RECENCY_RESOLUTION_SECONDS:
            os.utime(entry)


def _schedule_prune(directory: Path) -> None:
    """Start this process's only prune; a process unable to start one skips it."""
    global _PRUNE_SCHEDULED, _PRUNE_THREAD
    if _PRUNE_SCHEDULED:
        return
    _PRUNE_SCHEDULED = True
    thread = threading.Thread(
        target=_prune, args=(directory,), name=_PRUNE_THREAD_NAME, daemon=True
    )
    with suppress(RuntimeError):
        thread.start()
        _PRUNE_THREAD = thread


def finish_prune() -> None:
    """Wait until this process's background prune, if any, has ended (e.g. before a fork)."""
    if _PRUNE_THREAD is not None:
        _PRUNE_THREAD.join()


def _prune(directory: Path) -> None:
    """Reclaim unused files if due; the marker lands last, so a killed prune is retried."""
    marker = directory / _PRUNE_MARKER
    now = time.time()
    with suppress(OSError):
        if now - marker.stat().st_mtime < _PRUNE_INTERVAL_SECONDS:
            return
    with suppress(OSError):
        _evict(directory, now)
        marker.touch()


def _evict(directory: Path, now: float) -> None:
    """Delete expired files, then the least recently used until under the size limit.

    Covers crash-orphaned temporary files. Losing a race with a concurrent
    reader or writer only costs that process a cache miss.
    """
    files: list[tuple[float, int, Path]] = []
    with os.scandir(directory) as scan:
        for item in scan:
            with suppress(OSError):
                status = item.stat()
                files.append((status.st_mtime, status.st_size, Path(item.path)))
    total = sum(size for _modified, size, _path in files)
    for modified, size, path in sorted(files):
        if now - modified < _MAX_ENTRY_AGE_SECONDS and total <= _MAX_CACHE_BYTES:
            break
        with suppress(OSError):
            path.unlink(missing_ok=True)
            total -= size
