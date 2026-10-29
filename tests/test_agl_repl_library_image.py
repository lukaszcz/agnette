"""REPL workflows stay correct across imports, resets, and redeclarations."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl import PipelineDriver
from agnette.agl.modules.ids import ModuleId
from agnette.agl.repl import ReplSession
from agnette.agl.scope.symbols import ModuleResolution
from agnette.agl.semantics.values import IntValue, TextValue
from agnette.agl.typecheck import program as typecheck_program
from agnette.agl.typecheck.env import TypeEnvironment
from tests._agl_helpers import agl_roots


def _open_session(root: Path | None = None) -> ReplSession:
    session = ReplSession(cwd=root)
    assert session.open() == ()
    return session


@pytest.mark.parametrize(
    ("entry", "expected"),
    [("x + 1", 2), ("case x of\n  | 1 => 2\n  | _ => 3", 2), ("x + 2 * 3", 7)],
    ids=["binding", "match", "precedence"],
)
def test_later_entries_use_prior_bindings(entry: str, expected: int) -> None:
    session = _open_session()
    assert session.eval_entry("let x = 1").ok

    result = session.eval_entry(entry)

    assert result.ok, result.diagnostics
    assert result.value == IntValue(expected)


def test_reset_discards_bindings_and_accepts_fresh_definitions() -> None:
    session = _open_session()
    assert session.eval_entry("let x = 1").ok
    session.reset()

    missing = session.eval_entry("x")
    assert not missing.ok
    assert missing.diagnostics
    assert session.eval_entry("let x = 7").ok
    assert session.eval_entry("x + 1").value == IntValue(8)


class TestImportedModules:
    def test_later_entries_reuse_library_headers_despite_session_redeclarations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "tools.agl").write_text(
            "record Box[T]\n  value: T\ndef box(n: int) -> Box[int] = Box(value = n)\n"
        )
        preparations = 0
        original = typecheck_program.prepare_module_headers

        def counted(
            resolved: ModuleResolution,
            *,
            env: TypeEnvironment,
            module_id: ModuleId,
        ) -> None:
            nonlocal preparations
            if module_id == ModuleId(("tools",)):
                preparations += 1
            original(resolved, env=env, module_id=module_id)

        monkeypatch.setattr(typecheck_program, "prepare_module_headers", counted)
        session = _open_session(tmp_path)
        assert session.eval_entry("import tools\ntools::box(1).value").value == IntValue(1)
        for n in (2, 3):
            result = session.eval_entry(f"record Box\n  other: text\ntools::box({n}).value")
            assert result.ok, result.diagnostics
            assert result.value == IntValue(n)
        assert preparations == 1

    def test_a_newly_imported_module_is_resolved_and_used(self, tmp_path: Path) -> None:
        (tmp_path / "later.agl").write_text("def twice(n: int) -> int = n * 2\n")
        session = _open_session(tmp_path)
        assert session.eval_entry("let x = 1").ok

        result = session.eval_entry("import later::*\ntwice(21)")

        assert result.ok, result.diagnostics
        assert result.value == IntValue(42)

    def test_a_library_var_written_in_one_entry_is_observed_in_the_next(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "counter.agl").write_text("var count: int = 0\n")
        session = _open_session(tmp_path)
        assert session.eval_entry("import counter::*\ncount := 5").ok

        result = session.eval_entry("count")

        assert result.ok, result.diagnostics
        assert result.value == IntValue(5)

    def test_an_unannotated_library_var_written_in_one_entry_is_observed_in_the_next(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "counter.agl").write_text("var count = 0\n")
        session = _open_session(tmp_path)
        assert session.eval_entry("import counter::*\ncount := 5").ok

        result = session.eval_entry("count")

        assert result.ok, result.diagnostics
        assert result.value == IntValue(5)

    def test_imported_operators_evaluate_with_declared_precedence(self, tmp_path: Path) -> None:
        (tmp_path / "ops.agl").write_text(
            "infixl <+> at 6\n"
            "def <+>(a: int, b: int) -> int = a + b + 1\n"
            "def combined() -> int = 1 <+> 2 <+> 3\n"
        )
        session = _open_session(tmp_path)
        assert session.eval_entry("let x = 1").ok

        result = session.eval_entry("import ops::*\ncombined()")

        assert result.ok, result.diagnostics
        assert result.value == IntValue(8)

    def test_a_later_import_of_a_second_module_still_sees_the_first(self, tmp_path: Path) -> None:
        (tmp_path / "one.agl").write_text("def one() -> int = 1\n")
        (tmp_path / "two.agl").write_text("def two() -> int = 2\n")
        session = _open_session(tmp_path)
        assert session.eval_entry("import one::*\none()").ok

        result = session.eval_entry("import two::*\none() + two()")

        assert result.ok, result.diagnostics
        assert result.value == IntValue(3)

    def test_sessions_share_only_the_bootstrap_across_different_roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agnette.agl.modules import loader

        builds = 0
        original = loader.build_repl_graph

        def counted(*args: object, **kwargs: object) -> object:
            nonlocal builds
            builds += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(loader, "build_repl_graph", counted)
        first = tmp_path / "first"
        first.mkdir()
        second = tmp_path / "second"
        second.mkdir()
        body = "def pick(n: int) -> int =\n  case n of\n    | 0 => %s\n    | _ => %s\n"
        (first / "shared.agl").write_text(body % (1, 2))
        (second / "shared.agl").write_text(body % (30, 40))

        first_session = _open_session(first)
        initial_builds = builds
        second_session = _open_session(second)
        assert builds == initial_builds

        first_result = first_session.eval_entry("import shared::*\npick(0)")
        assert first_result.ok, first_result.diagnostics
        assert first_result.value == IntValue(1)
        result = second_session.eval_entry("import shared::*\npick(0)")
        assert result.ok, result.diagnostics
        assert result.value == IntValue(30)

        assert first_session.eval_entry("let private_value = 7").ok
        assert not second_session.eval_entry("private_value").ok
        assert first_session.eval_entry("pick(1)").value == IntValue(2)
        assert second_session.eval_entry("pick(1)").value == IntValue(40)

    def test_fresh_sessions_reuse_bootstrap_matches_without_sharing_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agnette.agl.matchcompile import stage
        from agnette.agl.typecheck.env import CheckedModule

        module = tmp_path / "counter.agl"
        source = (
            "var count: int = 0\n"
            "def pick(flag: bool) -> int =\n"
            "  case flag of\n    | true => count\n    | false => 99\n"
        )
        module.write_text(source)
        compilations = 0
        original = stage._compile_owner_sites

        def counted(owner: CheckedModule) -> object:
            nonlocal compilations
            if owner.module_id == ModuleId(("std", "prelude")):
                compilations += 1
            return original(owner)

        monkeypatch.setattr(stage, "_compile_owner_sites", counted)
        first = _open_session(tmp_path)
        prepared = PipelineDriver.prepare_program(
            "program def main() -> unit = ()", roots=agl_roots(tmp_path)
        )
        driver = PipelineDriver(get_sandbox_context=None, resolve_agent_spec=None)
        assert driver.check_prepared(prepared).ok
        assert first.eval_entry("import counter::*\ncount := 7\npick(true)").value == IntValue(7)
        initial_compilations = compilations
        second = _open_session(tmp_path)
        result = second.eval_entry("import counter::*\npick(true)")
        assert result.ok, result.diagnostics
        assert result.value == IntValue(0)
        assert compilations == initial_compilations

        second.reset()
        module.write_text(source.replace("=> 99", "=> 100"))
        assert second.open() == ()
        changed = second.eval_entry("import counter::*\npick(false)")
        assert changed.ok, changed.diagnostics
        assert changed.value == IntValue(100)
        assert first.eval_entry("pick(true)").value == IntValue(7)
        assert compilations == initial_compilations

    def test_a_reset_session_rereads_a_module_whose_file_changed(self, tmp_path: Path) -> None:
        """Reuse survives a reset only for modules the reopened session reloads."""
        module = tmp_path / "shifting.agl"
        module.write_text("def answer() -> int = 1\n")
        session = _open_session(tmp_path)
        assert session.eval_entry("import shifting::*\nanswer()").value == IntValue(1)

        session.reset()
        module.write_text("def answer() -> int = 2\n")
        result = session.eval_entry("import shifting::*\nanswer()")

        assert result.ok, result.diagnostics
        assert result.value == IntValue(2)


class TestRedeclarations:
    def test_redeclared_enum_keeps_old_and_new_member_identities_apart(self) -> None:
        session = _open_session()
        assert session.eval_entry("enum E\n  | A(old: int)").ok
        assert session.eval_entry("type OldA = E::A").ok
        assert session.eval_entry("let old: E = A(old = 1)").ok
        assert session.eval_entry("enum E\n  | B(fresh: text)").ok
        assert session.eval_entry('let new: E = B(fresh = "new")').ok

        assert not session.eval_entry("old is A").ok
        assert session.eval_entry("(old as OldA).old").value == IntValue(1)
        assert session.eval_entry("(new as E::B).fresh").value == TextValue("new")
        assert not session.eval_entry("old as E::B").ok

    def test_redeclared_record_keeps_the_retained_value_at_its_old_shape(self) -> None:
        session = _open_session()
        assert session.eval_entry("record R\n  a: int").ok
        assert session.eval_entry("let old = R(a = 1)").ok
        assert session.eval_entry("record R\n  b: text").ok

        assert session.eval_entry("old.a").value == IntValue(1)
        assert not session.eval_entry("let mixed: R = old").ok
        assert session.eval_entry('R(b = "x").b').value == TextValue("x")


def test_session_operators_do_not_change_imported_function_behavior(tmp_path: Path) -> None:
    (tmp_path / "ops.agl").write_text(
        "infixl <+> at 6\n"
        "def <+>(a: int, b: int) -> int = a + b + 1\n"
        "infixr <?> at 7\n"
        "def <?>(a: int, b: int) -> int = a * b + 2\n"
        "def combined() -> int = 1 <+> 2 <?> 3\n"
    )
    session = _open_session(tmp_path)
    assert session.eval_entry("import ops::*\ncombined()").value == IntValue(10)
    assert session.eval_entry("1 <+> 2 <?> 3 <+> 4").value == IntValue(15)

    assert session.eval_entry("infixr <+> at 9\ndef <+>(a: int, b: int) -> int = a - b").ok

    assert session.eval_entry("combined()").value == IntValue(10)
    assert session.eval_entry("8 <+> 3 <+> 1").value == IntValue(6)
    assert session.eval_entry("combined()").value == IntValue(10)


@pytest.mark.parametrize(
    ("bad_entry", "expected_error"),
    [('let x: int = "bad"', "static"), ("let ( = 1", "static"), ("1 / 0", "runtime")],
    ids=["type-error", "syntax-error", "runtime-error"],
)
def test_failed_entries_preserve_prior_state_and_allow_recovery(
    bad_entry: str, expected_error: str
) -> None:
    session = _open_session()
    assert session.eval_entry("let x = 21").ok

    failed = session.eval_entry(bad_entry)

    assert not failed.ok
    if expected_error == "static":
        assert failed.diagnostics
    else:
        assert failed.error is not None
    assert session.eval_entry("x * 2").value == IntValue(42)
