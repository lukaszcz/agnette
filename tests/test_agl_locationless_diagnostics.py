"""A diagnostic about something no source declares has no location and is handled as such."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import pytest

from agnette.agl.diagnostics import (
    AglError,
    Diagnostic,
    format_diagnostic,
    format_diagnostic_location,
)
from agnette.agl.modules.errors import ModuleNotFound
from agnette.agl.modules.ids import STD_CONFIG_ID
from agnette.agl.modules.roots import RootSet
from agnette.agl.pipeline import PipelineDriver
from agnette.agl.repl import ReplSession
from agnette.agl.repl.entry import REPL_LABEL, relocate_diagnostic
from agnette.agl.scope.symbols import AllowRegion
from agnette.agl.syntax.spans import UNKNOWN_SOURCE, SourceSpan
from agnette.agl.warning_policy import WarningPolicy, apply_warning_policy
from agnette.config import module_roots
from agnette.config.module_roots import StdlibResolutionError
from tests._agl_helpers import REPO_STDLIB_ROOT, agent_value, agl_roots, run_inline_code
from tests.agl.module_graph import load_graph

_TAB = "tab-indentation"


def test_a_diagnostic_without_a_line_is_formatted_without_a_location() -> None:
    diagnostic = Diagnostic(message="boom", source_label="/a/b.agl")

    for source_name in ("<agl>", None):
        output = format_diagnostic(diagnostic, source_name=source_name)
        assert format_diagnostic_location(diagnostic, source_name=source_name) == ""
        assert not re.search(r":\d+:", output)
        assert "boom" in output


def test_a_located_diagnostic_keeps_its_location() -> None:
    diagnostic = Diagnostic(message="boom", line=3, column=2)

    location = format_diagnostic_location(diagnostic)

    assert location
    assert format_diagnostic(diagnostic).startswith(location)


def test_relocation_leaves_a_diagnostic_without_a_line_in_place() -> None:
    diagnostic = Diagnostic(message="boom")

    def place(location: tuple[str | None, int, int | None]) -> tuple[str | None, int, int | None]:
        return "moved", location[1] + 10, location[2]

    assert relocate_diagnostic(diagnostic, place) == diagnostic


def test_an_allow_region_never_covers_a_warning_without_a_line() -> None:
    warning = Diagnostic(message="tab", severity="warning", warning_name=_TAB)
    everything = SourceSpan(1, 1, 99, 1, 0, 1)
    regions = {UNKNOWN_SOURCE.label: [AllowRegion(everything, frozenset({_TAB}))]}

    outcome = apply_warning_policy([warning], regions, WarningPolicy(), scope_complete=True)

    assert outcome.reported == (warning,)


def _driver() -> PipelineDriver:
    return PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)


def _assert_unlocated(diagnostics: Sequence[Diagnostic], source: str) -> None:
    assert diagnostics
    for diagnostic in diagnostics:
        assert diagnostic.line is None
        assert not re.search(r":\d+:", format_diagnostic(diagnostic, source_name=source))


def test_an_error_without_a_span_reports_no_location() -> None:
    _assert_unlocated([AglError("boom").to_diagnostic()], "<agl>")


def test_a_failing_implicit_prelude_import_reports_no_location(tmp_path: Path) -> None:
    with pytest.raises(ModuleNotFound) as caught:
        load_graph("let x = 1", entry_path=None, roots=agl_roots(tmp_path, include_stdlib=False))

    assert caught.value.span is None
    _assert_unlocated(caught.value.diagnostics(), "<code>")


def test_an_unresolvable_stdlib_reports_no_location(monkeypatch: pytest.MonkeyPatch) -> None:
    def unresolvable(*, home: Path, anchor: Path | None = None) -> module_roots.StdlibSelection:
        raise StdlibResolutionError("corrupt active std package")

    monkeypatch.setattr(module_roots, "select_stdlib_root", unresolvable)

    prepared = PipelineDriver.prepare_program("program def main() -> unit = ()")

    _assert_unlocated(prepared.diagnostics, "<code>")


def test_several_programs_without_a_selection_report_no_location() -> None:
    result = _driver().run("program def a() -> unit = ()\nprogram def b() -> unit = ()\n")

    assert not result.ok
    _assert_unlocated(result.diagnostics, "<code>")


_MALFORMED_AGENT = {
    (STD_CONFIG_ID, (), "agent"): agent_value("AgentCommand", command="x -p 'oops"),
}
"""A seed of the ``agent`` setting whose command does not shell-split."""

_UNSEEDED_READ = "import std/first\n\nlet seen = std/first::value\nseen"
"""Reads, on line 3, a ``builtin var`` of an unseeded, default-less library."""


def _unseeded_library(root: Path) -> None:
    (root / "std").mkdir()
    (root / "std" / "first.agl").write_text("builtin var value: int\n", encoding="utf-8")


def test_a_malformed_host_agent_of_a_run_reports_no_location() -> None:
    result = run_inline_code(
        _driver(),
        "1 + 1",
        roots=agl_roots(REPO_STDLIB_ROOT),
        builtin_var_seeds=_MALFORMED_AGENT,
    )

    assert not result.ok
    _assert_unlocated(result.diagnostics, "<code>")


def test_a_missing_host_value_of_a_run_is_reported_where_it_is_read(tmp_path: Path) -> None:
    _unseeded_library(tmp_path)

    result = run_inline_code(
        _driver(), _UNSEEDED_READ, roots=RootSet(roots=frozenset({tmp_path})), default_stdlib=False
    )

    assert not result.ok
    assert [d.line for d in result.diagnostics] == [3]


def test_a_malformed_host_agent_of_a_repl_entry_reports_no_location() -> None:
    session = ReplSession(builtin_var_seeds=_MALFORMED_AGENT)

    result = session.eval_entry("1 + 1")

    assert not result.ok
    _assert_unlocated(result.diagnostics, REPL_LABEL)


def test_a_missing_host_value_of_a_repl_entry_is_reported_where_it_is_read(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "first.agl").write_text("builtin var value: int\n", encoding="utf-8")
    session = ReplSession(stdlib_root=tmp_path, default_stdlib=False)

    result = session.eval_entry(_UNSEEDED_READ)

    assert not result.ok
    assert [d.line for d in result.diagnostics] == [3]


def test_a_repl_entry_interrupted_at_run_time_reports_no_location() -> None:
    session = ReplSession()
    with patch("agnette.agl.eval.ir_interpreter.IrInterpreter.run", side_effect=KeyboardInterrupt):
        result = session.eval_entry("1 + 1")

    assert not result.ok
    _assert_unlocated(result.diagnostics, REPL_LABEL)


def test_a_repl_hashability_downgrade_is_reported_at_the_offending_declaration() -> None:
    session = ReplSession(default_stdlib=False)
    assert session.eval_entry("exception Base extends Exception").ok
    assert session.eval_entry("def key(e: Base) -> dict[Base, int] = {e: 1}").ok

    result = session.eval_entry(
        "def unrelated() -> int = 1\nexception Bad extends Base\n  xs: array[int]"
    )

    assert not result.ok
    assert [d.line for d in result.diagnostics] == [2]


def test_a_repl_hashability_downgrade_is_reported_at_an_indirect_subtype() -> None:
    session = ReplSession(default_stdlib=False)
    assert session.eval_entry("exception Base extends Exception").ok
    assert session.eval_entry("exception Mid extends Base").ok
    assert session.eval_entry("def key(e: Base) -> dict[Base, int] = {e: 1}").ok

    result = session.eval_entry(
        "exception Other extends Exception\nexception Bad extends Mid\n  xs: array[int]"
    )

    assert not result.ok
    assert [d.line for d in result.diagnostics] == [2]
