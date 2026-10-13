"""The warning policy on every pipeline surface: driver passes, caches, and the REPL."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from agnette.agl import PipelineDriver, artifact_cache
from agnette.agl.diagnostics import Diagnostic
from agnette.agl.pipeline import RunResult
from agnette.agl.repl.session import ReplSession
from agnette.agl.warning_policy import WarningPolicy
from tests._agl_helpers import agl_roots, prepare_inline_code, repl_session_with_root

_TAB = "# a comment carrying a TAB:\there\n"
_PROGRAM = _TAB + 'print "ran"\n'
_DENY_TAB = WarningPolicy({"tab-indentation": "deny"})
_ALLOW_TAB = WarningPolicy({"tab-indentation": "allow"})


def _driver(policy: WarningPolicy | None = None) -> PipelineDriver:
    return PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None, warning_policy=policy)


def _names(diagnostics: object) -> list[tuple[str, str | None]]:
    assert isinstance(diagnostics, (list, tuple))
    return [(d.severity, d.warning_name) for d in diagnostics if isinstance(d, Diagnostic)]


def test_default_policy_reports_the_warning_and_runs(capsys: pytest.CaptureFixture[str]) -> None:
    result = _driver().run_prepared(prepare_inline_code(_PROGRAM), select_default_program=True)

    assert result.ok
    assert _names(result.warnings) == [("warning", "tab-indentation")]
    assert capsys.readouterr().out == "ran\n"


@pytest.mark.parametrize("check_only", [False, True])
def test_a_denied_warning_fails_the_run_before_anything_executes(
    check_only: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _driver(_DENY_TAB).run_prepared(prepare_inline_code(_PROGRAM), check_only=check_only)

    assert not result.ok and result.error is None
    assert _names(result.diagnostics) == [("error", "tab-indentation")]
    assert result.warnings == []
    assert capsys.readouterr().out == ""


def test_an_allowed_warning_is_not_reported() -> None:
    result = _driver(_ALLOW_TAB).run_prepared(prepare_inline_code(_PROGRAM), check_only=True)

    assert result.ok
    assert result.warnings == []


def test_discovery_applies_the_policy() -> None:
    prepared = prepare_inline_code(_PROGRAM)

    denied = _driver(_DENY_TAB).discover_programs(prepared)
    allowed = _driver(_ALLOW_TAB).discover_programs(prepared)

    assert denied.compiled is None
    assert _names(denied.diagnostics) == [("error", "tab-indentation")]
    assert denied.warnings == ()
    assert allowed.compiled is not None
    assert allowed.warnings == ()


def test_the_policy_applies_to_a_program_compiled_under_another_policy() -> None:
    prepared = prepare_inline_code(_PROGRAM)
    discovery = _driver().discover_programs(prepared)

    result = _driver(_DENY_TAB).run_prepared(prepared, compiled=discovery.compiled)

    assert not result.ok
    assert _names(result.diagnostics) == [("error", "tab-indentation")]


_TYPE_ERROR = 'let x: int = "s"\n'


def test_a_denied_warning_follows_the_errors_of_the_failing_pass() -> None:
    result = _driver(_DENY_TAB).run_prepared(prepare_inline_code(_TAB + _TYPE_ERROR))

    assert not result.ok
    assert _names(result.diagnostics) == [("error", None), ("error", "tab-indentation")]


def test_warnings_of_a_module_scope_never_resolved_are_withheld() -> None:
    result = _driver(_DENY_TAB).run_prepared(prepare_inline_code(_TAB + "nonexistent\n"))

    assert not result.ok
    assert _names(result.diagnostics) == [("error", None)]
    assert result.warnings == []


def test_a_warning_of_an_entry_that_failed_to_parse_is_withheld() -> None:
    result = _driver(_DENY_TAB).run_prepared(prepare_inline_code(_TAB + "let = 1\n"))

    assert not result.ok
    assert _names(result.diagnostics) == [("error", None)]
    assert result.warnings == []


def test_a_region_hides_the_warning_inside_its_declaration() -> None:
    source = (
        '@allow("tab-indentation")\n'
        "def quiet() -> int =\n"
        "  # a TAB:\there\n"
        "  1\n"
        "def loud() -> int =\n"
        "  # a TAB:\there\n"
        "  2\n"
    )

    result = _driver(_DENY_TAB).run_prepared(prepare_inline_code(source), check_only=True)

    assert [(d.line, d.warning_name) for d in result.diagnostics] == [(6, "tab-indentation")]


_LIBRARY = (
    '@allow("noop-parse-retries")\n'
    'def quiet() -> text = ask("Q", parse-error-retries = 0)\n'
    'def loud() -> text = ask("Q", parse-error-retries = 0)\n'
)


def _check_library(
    root: Path, policy: WarningPolicy | None = None, *, entry: str = "import lib::*\n"
) -> RunResult:
    prepared = prepare_inline_code(entry, roots=agl_roots(root))
    return _driver(policy).check_prepared(prepared)


@pytest.mark.parametrize("policy", [None, WarningPolicy({"noop-parse-retries": "deny"})])
def test_regions_of_an_imported_module_survive_memory_and_disk_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: WarningPolicy | None
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "lib.agl").write_text(_LIBRARY)
    artifact_cache.clear_retained_artifacts()
    cold = _check_library(tmp_path, policy)
    warm = _check_library(tmp_path, policy)
    artifact_cache.clear_retained_artifacts()  # memory only; disk persists
    restored = _check_library(tmp_path, policy)

    for result in (cold, warm, restored):
        reported = result.diagnostics if policy is not None else result.warnings
        assert [(d.line, d.warning_name) for d in reported] == [(3, "noop-parse-retries")]
        assert all(d.source_label == str(tmp_path / "lib.agl") for d in reported)
        assert result.ok is (policy is None)


def test_cached_modules_keep_raw_warnings_so_each_invocation_applies_its_own_policy(
    tmp_path: Path,
) -> None:
    (tmp_path / "lib.agl").write_text('def loud() -> text = ask("Q", parse-error-retries = 0)\n')
    allow = WarningPolicy({"noop-parse-retries": "allow"})
    deny = WarningPolicy({"noop-parse-retries": "deny"})

    allowed = _check_library(tmp_path, allow)
    default = _check_library(tmp_path)
    denied = _check_library(tmp_path, deny)

    assert allowed.warnings == []
    assert _names(default.warnings) == [("warning", "noop-parse-retries")]
    assert default.ok
    assert not denied.ok


def test_repl_entry_with_a_denied_warning_fails_without_promoting_anything() -> None:
    session = ReplSession(warning_policy=_DENY_TAB)

    denied = session.eval_entry("let x = 1 # a TAB:\there\n")
    later = session.eval_entry("x")

    assert not denied.ok
    assert _names(denied.diagnostics) == [("error", "tab-indentation")]
    assert denied.warnings == []
    assert not later.ok


def test_repl_applies_levels_and_regions_per_entry() -> None:
    default = ReplSession().eval_entry("let x = 1 # a TAB:\there\n")
    allowed = ReplSession(warning_policy=_ALLOW_TAB).eval_entry("let y = 1 # a TAB:\there\n")
    region = ReplSession(warning_policy=_DENY_TAB).eval_entry(
        '@allow("tab-indentation")\ndef f() -> int =\n  # a TAB:\there\n  1\n'
    )

    assert default.ok
    assert _names(default.warnings) == [("warning", "tab-indentation")]
    assert allowed.ok and allowed.warnings == []
    assert region.ok and region.warnings == []


def test_repl_withholds_the_warning_of_an_entry_that_failed_to_parse() -> None:
    result = ReplSession(warning_policy=_DENY_TAB).eval_entry("let = # a TAB:\there\n")

    assert not result.ok
    assert _names(result.diagnostics) == [("error", None)]
    assert result.warnings == []


# A TAB inside the hidden def, one outside it; the second def is the one reported.
_REGION_AND_STRAY = (
    '@allow("tab-indentation")\n'
    "def quiet() -> int =\n"
    "  # a TAB:\there\n"
    "  1\n"
    "def loud() -> int =\n"
    "  # a TAB:\there\n"
    "  2\n"
)


def test_a_region_is_honoured_when_scope_fails_in_a_later_module(tmp_path: Path) -> None:
    (tmp_path / "bad.agl").write_text("def broken() -> int = nonexistent\n")
    prepared = prepare_inline_code("import bad::*\n" + _REGION_AND_STRAY, roots=agl_roots(tmp_path))

    result = _driver(_DENY_TAB).run_prepared(prepared)

    assert prepared.resolved is None
    assert [(d.line, d.warning_name) for d in result.diagnostics][1:] == [(7, "tab-indentation")]
    assert result.diagnostics[0].warning_name is None


def test_a_region_is_honoured_when_type_checking_fails() -> None:
    source = _REGION_AND_STRAY + _TYPE_ERROR

    result = _driver(_DENY_TAB).run_prepared(prepare_inline_code(source))

    assert [(d.line, d.warning_name) for d in result.diagnostics][1:] == [(6, "tab-indentation")]
    assert result.diagnostics[0].warning_name is None


def test_repl_honours_a_region_when_type_checking_fails() -> None:
    result = ReplSession(warning_policy=_DENY_TAB).eval_entry(_REGION_AND_STRAY + _TYPE_ERROR)

    assert not result.ok
    assert [(d.line, d.warning_name) for d in result.diagnostics][1:] == [(6, "tab-indentation")]
    assert result.diagnostics[0].warning_name is None


def test_repl_denies_a_stray_warning_beside_a_type_error() -> None:
    result = ReplSession(warning_policy=_DENY_TAB).eval_entry(_TAB + _TYPE_ERROR)

    assert _names(result.diagnostics) == [("error", None), ("error", "tab-indentation")]


def test_a_warning_of_an_imported_module_that_failed_to_parse_is_withheld(tmp_path: Path) -> None:
    (tmp_path / "lib.agl").write_text(_TAB + "let = 1\n")

    result = _check_library(tmp_path, _DENY_TAB)

    assert not result.ok
    assert _names(result.diagnostics) == [("error", None)]
    assert result.warnings == []


_TABBY_LIBRARY = "def tabby() -> int =\n  # a TAB:\there\n  1\n"


def test_driver_and_repl_govern_the_lexer_warnings_of_an_imported_module_alike(
    tmp_path: Path,
) -> None:
    (tmp_path / "tl.agl").write_text(_TABBY_LIBRARY)
    expected = [(2, "tab-indentation", str(tmp_path / "tl.agl"))]

    def located(diagnostics: Sequence[Diagnostic]) -> list[tuple[int, str | None, str | None]]:
        return [(d.line, d.warning_name, d.source_label) for d in diagnostics]

    for policy, fails in ((None, False), (_DENY_TAB, True)):
        driven = _check_library(tmp_path, policy, entry="import tl::*\n")
        repl = repl_session_with_root(tmp_path, warning_policy=policy).eval_entry("import tl::*\n")

        for ok, diagnostics, warnings in (
            (driven.ok, driven.diagnostics, driven.warnings),
            (repl.ok, repl.diagnostics, repl.warnings),
        ):
            assert ok is not fails
            assert located(diagnostics if fails else warnings) == expected


# One line holding a declaration, a TAB, and a sibling declaration carrying ``@allow``; the
# TAB inside the allowed sibling is hidden, the two before it are not.
_SIBLINGS = {
    "module-root": 'def f() -> int =\t1;\t@allow("tab-indentation") def g() -> int =\t2\n',
    "block": ('def h() -> int =\n  let a =\t1;\t@allow("tab-indentation") let b =\t2\n  a + b\n'),
    "parenthesized-block": (
        'def k() -> int = (let a =\t1;\t@allow("tab-indentation") let b =\t2; a + b)\n'
    ),
}


@pytest.mark.parametrize("source", _SIBLINGS.values(), ids=_SIBLINGS.keys())
def test_a_region_never_reaches_a_sibling_declaration_on_its_first_line(source: str) -> None:
    result = _driver(_DENY_TAB).run_prepared(prepare_inline_code(source), check_only=True)

    line = source.count("\n", 0, source.index("@allow")) + 1
    line_start = source.rfind("\n", 0, source.index("@allow")) + 1
    first = source.index("\t", line_start) - line_start + 1
    second = source.index("\t", line_start + first) - line_start + 1
    assert [(d.line, d.column) for d in result.diagnostics] == [(line, first), (line, second)]


def _entry_regions(source: str) -> list[tuple[int, int, int]]:
    prepared = prepare_inline_code(source)
    assert prepared.resolved is not None
    entry = prepared.resolved.modules[prepared.resolved.entry_id]
    return [
        (r.span.start_line, r.span.start_col, r.span.end_line)
        for r in entry.resolved.attributes.warning_allows
    ]


def test_a_region_starts_at_its_declaration_unless_only_whitespace_precedes_it() -> None:
    indented = 'def h() -> int =\n  @allow("tab-indentation") let b = 2\n  b\n'
    sibling = 'def f() -> int = 1; @allow("tab-indentation") def g() -> int = 2\n'

    assert _entry_regions(indented) == [(2, 1, 2)]
    assert _entry_regions(sibling) == [(1, sibling.index("@allow") + 1, 1)]


def test_repl_reports_the_warnings_of_a_library_module_once_when_it_first_loads(
    tmp_path: Path,
) -> None:
    (tmp_path / "lib.agl").write_text(_LIBRARY)
    (tmp_path / "tl.agl").write_text(_TABBY_LIBRARY)
    session = repl_session_with_root(tmp_path)

    loaded = session.eval_entry("import lib::*\n")
    tabby = session.eval_entry("import tl::*\n")
    later = [session.eval_entry("1 + 1"), session.eval_entry("loud")]

    assert _names(loaded.warnings) == [("warning", "noop-parse-retries")]
    assert _names(tabby.warnings) == [("warning", "tab-indentation")]
    assert all(entry.ok and entry.warnings == [] for entry in later)
