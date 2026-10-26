"""Spelling an exception's base never recurses into display and runs few pipelines."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agnette.agl.repl.entry_pipeline import EntryPipeline
from agnette.agl.repl.session import ReplSession


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
    assert session.eval_entry(declaration.replace("F", "G")).ok


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
    assert session.eval_entry(declaration.replace("F", "G")).ok
