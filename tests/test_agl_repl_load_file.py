"""``ReplSession.load_file``: file-level attributes and file-relative diagnostics."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.diagnostics import Diagnostic
from agnette.agl.repl.entry import EntryResult, FileOrigin, relocate
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

    placed = relocate(result, FileOrigin("f.agl", 10, 5).place).diagnostics

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


def _transcript(*entries: str) -> str:
    return "# agm:repl-transcript:v1\n" + "".join(
        f"# agm:entry:{len(entry)}\n{entry}\n" for entry in entries
    )


def test_a_leading_allow_does_not_silence_a_library_module_it_imports(tmp_path: Path) -> None:
    (tmp_path / "w.agl").write_text('def run() -> unit =\n  let v = "x"\n  exec $ echo %{v}\n')
    source = f'@!allow("{HOLE}")\nimport w::*\nlet a = 1\n'
    path = tmp_path / "lw.agl"
    path.write_text(source, encoding="utf-8")

    loaded = ReplSession(cwd=tmp_path).load_file(path)
    typed = ReplSession(cwd=tmp_path).eval_entry(source)

    assert _names(loaded) == _names([typed]) == [HOLE]
    assert {w.source_label for r in loaded for w in r.warnings} == {
        w.source_label for w in typed.warnings
    }


RUNTIME_ERROR_FILE = "def f(x: int) -> int =\n  let y = [1]\n  y[x]\n\n\n\nlet b = f(7)\n"


def test_a_runtime_error_in_an_earlier_loaded_entry_keeps_its_own_place(tmp_path: Path) -> None:
    path = tmp_path / "rt.agl"
    path.write_text(RUNTIME_ERROR_FILE, encoding="utf-8")

    error = _session().load_file(path)[-1].error

    assert error is not None
    assert (error.source, error.line) == (str(path), 3)


def test_a_runtime_error_in_a_loaded_function_called_from_a_typed_entry(tmp_path: Path) -> None:
    path = tmp_path / "rt.agl"
    path.write_text(RUNTIME_ERROR_FILE.split("\n\n")[0] + "\n", encoding="utf-8")
    session = _session()
    session.load_file(path)

    error = session.eval_entry("f(7)").error

    assert error is not None
    assert (error.source, error.line) == (str(path), 3)


def test_a_runtime_error_in_a_typed_function_has_entry_positions(tmp_path: Path) -> None:
    path = tmp_path / "pad.agl"
    path.write_text("let pad = 1\n\n\n\nlet pad2 = 2\n", encoding="utf-8")
    session = _session()
    session.load_file(path)
    session.eval_entry("def f(x: int) -> int =\n  let y = [1]\n  y[x]")

    error = session.eval_entry("f(7)").error

    assert error is not None
    assert error.line == 3
    assert error.source != str(path)


def test_a_loaded_transcript_reports_file_positions(tmp_path: Path) -> None:
    text = _transcript("let a = 1", "let b = zz")
    path = tmp_path / "t.agl"
    path.write_text(text, encoding="utf-8")

    result = _session().load_file(path)[-1]

    assert not result.ok
    assert result.diagnostics[0].source_label == str(path)
    assert result.diagnostics[0].line == text.split("\n").index("let b = zz") + 1


def test_a_loaded_transcript_runtime_error_names_the_file(tmp_path: Path) -> None:
    text = _transcript("let a = 1", "let b = 1 / 0")
    path = tmp_path / "t.agl"
    path.write_text(text, encoding="utf-8")

    error = _session().load_file(path)[-1].error

    assert error is not None
    assert (error.source, error.line) == (str(path), text.split("\n").index("let b = 1 / 0") + 1)


@pytest.mark.parametrize("attributes", ["", '@!doc("x")\n'])
@pytest.mark.parametrize("type_text", ["int", "int -> int", "dict[text, int]"])
def test_a_file_may_be_one_bare_type(tmp_path: Path, attributes: str, type_text: str) -> None:
    results = _load(tmp_path, f"{attributes}{type_text}\n")

    assert [r.kind for r in results] == ["type"]
    assert all(r.ok for r in results)


def test_a_file_with_a_syntax_error_runs_nothing(tmp_path: Path) -> None:
    session = _session()

    results = _load(tmp_path, "let a = 1\nlet = 2\n", session)

    assert [r.ok for r in results] == [False]
    assert results[0].diagnostics[0].source_label == str(tmp_path / "f.agl")
    assert session.bindings() == []


def test_a_saved_transcript_keeps_the_loaded_files_allow(tmp_path: Path) -> None:
    source = f'@!allow("{HOLE}")\n\n{HOLES}'
    session = _session()
    assert all(result.ok for result in _load(tmp_path, source, session))
    saved = tmp_path / "saved.agl"
    saved.write_text(session.dump_source(), encoding="utf-8")

    replayed = _load(tmp_path, saved.read_text(), _session())

    assert all(result.ok for result in replayed)
    assert _names(replayed) == _names(_load(tmp_path, source)) == []
    assert _names(_load(tmp_path, HOLES)) != []
    assert session.dump_source() == _session_dump_after(tmp_path, saved)


def _session_dump_after(tmp_path: Path, saved: Path) -> str:
    session = _session()
    session.load_file(saved)
    return session.dump_source()
