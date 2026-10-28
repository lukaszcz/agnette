"""Spelling an exception's base never recurses into display and runs few pipelines."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agnette.agl.diagnostics import AglError
from agnette.agl.repl.entry_pipeline import EntryPipeline
from agnette.agl.repl.session import ReplSession
from agnette.agl.scope.program import ResolvedProgram
from agnette.agl.semantics.types import ExceptionType
from agnette.agl.syntax.advisories import SpacedQualifier
from agnette.agl.syntax.nodes import Program


def _session() -> ReplSession:
    session = ReplSession()
    session.open()
    return session


def _resolutions[T](
    monkeypatch: pytest.MonkeyPatch, session: ReplSession, action: Callable[[ReplSession], T]
) -> tuple[T, int]:
    """Return *action*'s result and the number of programs it resolves."""
    count = 0
    resolve = EntryPipeline.resolve_program

    def counting[**P, R](method: Callable[P, R]) -> Callable[P, R]:
        def run(*args: P.args, **kwargs: P.kwargs) -> R:
            nonlocal count
            count += 1
            return method(*args, **kwargs)

        return run

    with monkeypatch.context() as patch:
        patch.setattr(EntryPipeline, "resolve_program", counting(resolve))
        result = action(session)
    return result, count


def test_a_redeclared_cycle_of_bases_is_displayed() -> None:
    session = _session()
    for entry in ["exception B", "exception C extends B", "exception B extends C"]:
        assert session.eval_entry(entry).ok

    shown = session.eval_entry("C")
    info = session.info_of("C")

    assert shown.ok
    assert shown.type_display == "exception C extends B"
    assert _extends_line(info) == shown.type_display


def test_a_deep_chain_of_bases_is_spelled_with_constant_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def chain(length: int) -> ReplSession:
        session = _session()
        declarations = ["exception E0"]
        declarations.extend(f"exception E{i} extends E{i - 1}" for i in range(1, length))
        assert session.eval_entry("\n".join(declarations)).ok
        return session

    short, long = chain(40), chain(80)

    short_shown, short_work = _resolutions(monkeypatch, short, lambda s: s.eval_entry("E39"))
    long_shown, long_work = _resolutions(monkeypatch, long, lambda s: s.eval_entry("E79"))

    assert long_work == short_work
    for session, shown, name, base in [
        (short, short_shown, "E39", "E38"),
        (long, long_shown, "E79", "E78"),
    ]:
        assert shown.ok
        assert shown.type_display == f"exception {name} extends {base}"
        _redefines_same_base(session, name, declaration=shown.type_display)


def test_many_aliases_do_not_multiply_the_work_of_an_unspellable_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def session_with(aliases: int) -> ReplSession:
        session = _session()
        declarations = ["scope sc\n  exception E\n  exception F extends E\nend sc"]
        declarations.extend(f"type A{i} = int" for i in range(aliases))
        assert session.eval_entry("\n".join(declarations)).ok
        return session

    few, many = session_with(5), session_with(100)

    few_shown, few_work = _resolutions(monkeypatch, few, lambda s: s.info_of("sc::F"))
    many_shown, many_work = _resolutions(monkeypatch, many, lambda s: s.info_of("sc::F"))

    assert many_work == few_work
    for shown in (few_shown, many_shown):
        # E is scoped: the declaration must retain its bare-name fallback.
        assert _extends_line(shown) == "exception sc::F extends E"


def test_aliases_of_imported_types_do_not_multiply_the_work_of_an_unspellable_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "lib.agl").write_text("record LR\ntype LA = LR\n", encoding="utf-8")

    def session_with(aliases: int) -> ReplSession:
        session = ReplSession(cwd=tmp_path)
        session.open()
        for entry in ["import lib", "exception X", "exception Y extends X", "exception X"]:
            assert session.eval_entry(entry).ok
        assert session.eval_entry("\n".join(f"type A{i} = lib::LA" for i in range(aliases))).ok
        return session

    few, many = session_with(3), session_with(40)

    few_shown, few_work = _resolutions(monkeypatch, few, lambda s: s.eval_entry("Y"))
    many_shown, many_work = _resolutions(monkeypatch, many, lambda s: s.eval_entry("Y"))

    assert many_work == few_work
    for shown in (few_shown, many_shown):
        assert shown.ok
        # Y retains the old X, which has no reachable spelling after redeclaration.
        assert shown.type_display == "exception Y extends X"


def test_a_base_is_spelled_by_an_alias_when_its_own_name_is_out_of_reach() -> None:
    session = _session()
    assert session.eval_entry("scope sc\n  exception E\nend sc").ok
    assert session.eval_entry("type Alias = sc::E").ok
    assert session.eval_entry("exception F extends Alias").ok

    shown = session.info_of("F")

    assert shown is not None
    declaration = next(line.strip() for line in shown.splitlines() if "extends" in line)
    assert session.eval_entry(declaration.replace("exception F", "exception G", 1)).ok


def test_a_base_is_spelled_by_an_alias_of_an_alias() -> None:
    session = _session()
    for entry in [
        "scope sc\n  exception E\nend sc",
        "exception Other",
        "type OtherAlias = Other",
        "type OtherAgain = OtherAlias",
        "type First = sc::E",
        "type Second = First",
        "exception F extends Second",
    ]:
        assert session.eval_entry(entry).ok

    shown = session.info_of("F")

    assert shown is not None
    declaration = next(line.strip() for line in shown.splitlines() if "extends" in line)
    assert session.eval_entry(declaration.replace("exception F", "exception G", 1)).ok


def _extends_line(shown: str | None) -> str:
    assert shown is not None
    return next(line.strip() for line in shown.splitlines() if "extends" in line)


def _base_of(session: ReplSession, name: str) -> object:
    """The base of exception *name*, as the type table registers it."""
    shown = session.eval_entry(name)
    assert isinstance(shown.value_type, ExceptionType) and shown.type_table is not None
    return shown.type_table.exception_def(shown.value_type).base


def _redefines_same_base(
    session: ReplSession, name: str, *, declaration: str | None = None
) -> None:
    """Re-evaluating *name*'s printed declaration defines an exception with *name*'s base."""
    if declaration is None:
        declaration = _extends_line(session.info_of(name))
    assert session.eval_entry(declaration.replace(f"exception {name}", "exception Again", 1)).ok
    assert _base_of(session, "Again") == _base_of(session, name)


def test_a_base_is_spelled_by_an_alias_chain_whose_middle_alias_was_redeclared() -> None:
    session = _session()
    for entry in [
        "exception X",
        "type K0 = X",
        "type K1 = K0",
        "exception Y extends X",
        "exception X",
        "type K0 = int",
    ]:
        assert session.eval_entry(entry).ok

    _redefines_same_base(session, "Y")


def test_a_base_is_spelled_by_an_alias_of_an_imported_alias(tmp_path: Path) -> None:
    (tmp_path / "lib.agl").write_text("exception LE\ntype LA = LE\n", encoding="utf-8")
    session = ReplSession(cwd=tmp_path)
    session.open()
    for entry in ["import lib", "type B = lib::LA", "exception G extends B"]:
        assert session.eval_entry(entry).ok

    _redefines_same_base(session, "G")


def test_a_rejected_info_name_probes_the_session_scope_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    probes = 0
    resolve = EntryPipeline.resolve_program

    def counting(
        self: EntryPipeline,
        program: Program,
        next_node_id: int,
        *,
        source_text: str,
        spaced_qualifiers: tuple[SpacedQualifier, ...] = (),
    ) -> ResolvedProgram:
        nonlocal probes
        probes += source_text == ""
        return resolve(
            self,
            program,
            next_node_id,
            source_text=source_text,
            spaced_qualifiers=spaced_qualifiers,
        )

    monkeypatch.setattr(EntryPipeline, "resolve_program", counting)

    with pytest.raises(AglError):
        session.info_of("std/option::Nope")

    assert probes == 1


def test_a_base_is_spelled_afresh_after_a_reset() -> None:
    session = _session()
    for entry in ["exception X", "type K = X", "exception Y extends K"]:
        assert session.eval_entry(entry).ok
    session.reset()
    session.open()
    for entry in ["exception Z", "type K = Z", "exception Y extends K"]:
        assert session.eval_entry(entry).ok

    shown = _extends_line(session.info_of("Y"))

    assert shown.endswith(("Z", "K"))
