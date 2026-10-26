"""``ReplSession.load_file``: file-level attributes and file-relative diagnostics."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.diagnostics import Diagnostic
from agnette.agl.repl.entry import EntryResult, FileOrigin
from agnette.agl.repl.render import render_entry_result
from agnette.agl.repl.session import ReplSession

HOLE = "unquoted-exec-hole"
UNUSED_MODULE_DOC = "unused-module-doc"


def _session() -> ReplSession:
    session = ReplSession()
    session.open()
    return session


def _load(tmp_path: Path, source: str, session: ReplSession | None = None) -> list[EntryResult]:
    path = tmp_path / "f.agl"
    path.write_text(source, encoding="utf-8")
    return (session or _session()).load_file(path)


def _names(results: list[EntryResult]) -> list[str | None]:
    return [warning.warning_name for result in results for warning in result.warnings]


HOLES = 'let v = "x"\nexec $ echo %{v}\nexec $ echo %{v}\n'


def test_leading_allow_governs_every_loaded_entry(tmp_path: Path) -> None:
    allowed = _load(tmp_path, f'@!allow("{HOLE}")\n\n{HOLES}')
    unallowed = _load(tmp_path, HOLES)

    assert all(result.ok for result in allowed)
    assert _names(allowed) == []
    assert _names(unallowed) == [HOLE, HOLE]


def test_module_doc_warning_is_reported_once(tmp_path: Path) -> None:
    results = _load(tmp_path, '@!doc("m")\nlet x = 1\nlet y = 2\n')

    assert _names(results) == [UNUSED_MODULE_DOC]


def test_a_file_holding_only_a_module_doc_warns(tmp_path: Path) -> None:
    results = _load(tmp_path, '@!doc("m")\n')

    assert _names(results) == [UNUSED_MODULE_DOC]


@pytest.mark.parametrize("typed", [True, False])
def test_attributes_may_head_a_bare_type(tmp_path: Path, typed: bool) -> None:
    source = '@!doc("x")\nint'
    results = [_session().eval_entry(source)] if typed else _load(tmp_path, source + "\n")

    assert all(result.ok for result in results)
    assert [result.kind for result in results] == ["type"]
    assert _names(results) == [UNUSED_MODULE_DOC]


def test_allow_silences_the_warning_of_an_attributed_bare_type() -> None:
    result = _session().eval_entry(f'@!allow("{UNUSED_MODULE_DOC}")\n@!doc("x")\nint')

    assert result.ok
    assert result.warnings == []


def test_static_diagnostics_name_the_file_and_use_file_positions(tmp_path: Path) -> None:
    path = tmp_path / "f.agl"
    path.write_text("let a = 1\n\nlet b = zz\n", encoding="utf-8")

    results = _session().load_file(path)

    assert [result.ok for result in results] == [True, False]
    assert render_entry_result(results[-1], echo=True, echo_unit=False) == (
        f"{path}:3:9-10: error: 'zz' is not defined."
    )


def test_warnings_name_the_file_and_use_file_positions(tmp_path: Path) -> None:
    path = tmp_path / "f.agl"
    path.write_text('let v = "x"\n\n\nexec $ echo %{v}\n', encoding="utf-8")

    results = _session().load_file(path)

    warning = results[-1].warnings[0]
    assert (warning.source_label, warning.line) == (str(path), 4)


def test_runtime_errors_name_the_file_and_use_file_positions(tmp_path: Path) -> None:
    path = tmp_path / "f.agl"
    path.write_text("let a = 1\n\nlet b = 1 / 0\n", encoding="utf-8")

    results = _session().load_file(path)

    error = results[-1].error
    assert error is not None
    assert (error.source, error.line) == (str(path), 3)


def test_invalid_leading_attributes_allow_nothing_and_are_reported(tmp_path: Path) -> None:
    results = _load(tmp_path, '@!allow(1)\n@!doc("m")\nlet x = 1\n')

    assert not all(result.ok for result in results)


def test_a_diagnostic_of_another_source_keeps_its_place() -> None:
    other = Diagnostic("m", 2, 3, source_label="other.agl")
    bare = Diagnostic("m", 2, 3)
    result = EntryResult("statement", None, None, None, [other, bare], [], None, False)

    placed = FileOrigin("f.agl", 10, 5).locate(result).diagnostics

    assert (placed[0].line, placed[0].source_label) == (2, "other.agl")
    assert (placed[1].line, placed[1].column, placed[1].end_line, placed[1].source_label) == (
        11,
        3,
        None,
        "f.agl",
    )


def test_a_denied_module_doc_warning_rejects_an_attributed_type_entry() -> None:
    from agnette.agl.warning_policy import WarningPolicy

    session = ReplSession(warning_policy=WarningPolicy({UNUSED_MODULE_DOC: "deny"}))
    session.open()

    result = session.eval_entry('@!doc("m")\nint')

    assert not result.ok
    assert result.kind != "type"
