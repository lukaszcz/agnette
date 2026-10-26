"""Spelling an exception's base never recurses into display and runs few pipelines."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agnette.agl.repl.entry_pipeline import EntryPipeline
from agnette.agl.repl.session import ReplSession
from agnette.agl.semantics.types import ExceptionType


def _session() -> ReplSession:
    session = ReplSession()
    session.open()
    return session


def _resolutions(
    monkeypatch: pytest.MonkeyPatch, session: ReplSession, action: Callable[[ReplSession], object]
) -> int:
    """Count the programs *action* resolves."""
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
        action(session)
    return count


def test_a_redeclared_cycle_of_bases_is_displayed() -> None:
    session = _session()
    for entry in ["exception B", "exception C extends B", "exception B extends C"]:
        assert session.eval_entry(entry).ok

    shown = session.eval_entry("C")
    info = session.info_of("C")

    assert shown.ok
    assert shown.type_display is not None
    assert info is not None


def test_a_deep_chain_of_bases_is_spelled_with_constant_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def chain(length: int) -> ReplSession:
        session = _session()
        assert session.eval_entry("exception E0").ok
        for i in range(1, length):
            assert session.eval_entry(f"exception E{i} extends E{i - 1}").ok
        return session

    short, long = chain(40), chain(80)

    short_work = _resolutions(monkeypatch, short, lambda s: s.eval_entry("E39"))
    long_work = _resolutions(monkeypatch, long, lambda s: s.eval_entry("E79"))

    assert long_work == short_work


def test_many_aliases_do_not_multiply_the_work_of_an_unspellable_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def session_with(aliases: int) -> ReplSession:
        session = _session()
        assert session.eval_entry("scope sc\n  exception E\n  exception F extends E\nend sc").ok
        for i in range(aliases):
            assert session.eval_entry(f"type A{i} = int").ok
        return session

    few, many = session_with(5), session_with(100)

    few_work = _resolutions(monkeypatch, few, lambda s: s.info_of("sc::F"))
    many_work = _resolutions(monkeypatch, many, lambda s: s.info_of("sc::F"))

    assert many_work == few_work


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


def _redefines_same_base(session: ReplSession, name: str) -> None:
    """Re-evaluating *name*'s printed declaration defines an exception with *name*'s base."""
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
