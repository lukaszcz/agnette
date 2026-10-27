"""REPL trace records and runtime errors carry the locations diagnostics show."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from agnette.agl.repl.session import ReplSession

FAILING = 'exception Boom\n\ndef boom() -> int =\n  raise Boom(message = "x")\n'


def _records(path: Path) -> list[dict[str, object]]:
    return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]


def _session(tmp_path: Path) -> tuple[ReplSession, Path]:
    trace = tmp_path / "trace.jsonl"
    session = ReplSession(cwd=tmp_path, trace_path=trace)
    session.open()
    return session, trace


def test_typed_entries_are_traced_under_one_repl_label(tmp_path: Path) -> None:
    session, trace = _session(tmp_path)
    session.eval_entry('print("a")')
    session.eval_entry('print("b")')

    files = {record["file"] for record in _records(trace) if record["file"] is not None}

    assert files == {"<repl>"}


def test_a_loaded_files_entries_are_traced_at_file_positions(tmp_path: Path) -> None:
    session, trace = _session(tmp_path)
    path = tmp_path / "f.agl"
    path.write_text('let a = 1\n\nprint("hi")\n')
    assert all(r.ok for r in session.load_file(path))

    printed = [r for r in _records(trace) if r["kind"] == "print"]

    assert [(r["file"], r["line"]) for r in printed] == [(str(path), 3)]


def test_a_runtime_error_in_a_typed_function_names_the_repl(tmp_path: Path) -> None:
    session, _ = _session(tmp_path)
    assert session.eval_entry(FAILING).ok

    result = session.eval_entry("boom()")

    assert result.error is not None
    assert result.error.source == "<repl>"
