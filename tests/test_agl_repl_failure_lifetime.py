"""Returned static errors retain semantic data, not compilation frames."""

from __future__ import annotations

from collections.abc import Callable
from weakref import ref

import pytest

from agnette.agl.diagnostics import AglError, AglSyntaxError, AglTypeError
from agnette.agl.repl import ReplSession
from agnette.agl.repl.session import _static_verdict
from agnette.agl.semantics.values import IntValue
from tests._agl_helpers import dummy_span

pytestmark = pytest.mark.usefixtures("no_cyclic_gc")


class _Payload:
    pass


def _raised[T: BaseException](error: T, payload: _Payload) -> T:
    try:
        raise error
    except BaseException:
        return error


@pytest.mark.parametrize("text", ["missing_name", 'let value: int = "text"', "let ="])
def test_static_failure_data_does_not_keep_a_closed_session_alive(text: str) -> None:
    session = ReplSession(default_stdlib=False)
    owner = ref(session)
    session.open()
    result = session.eval_entry(text)
    assert not result.ok
    assert result.failure is not None
    diagnostics = result.failure.diagnostics()
    displayed = tuple(result.diagnostics)
    session.close()
    del session

    assert owner() is None
    assert result.failure.__traceback__ is None
    assert result.failure.diagnostics() == diagnostics
    assert tuple(result.diagnostics) == displayed


def test_successful_result_does_not_keep_a_closed_session_alive() -> None:
    session = ReplSession(default_stdlib=False)
    owner = ref(session)
    session.open()
    result = session.eval_entry("7")
    session.close()
    del session

    assert result.ok and result.value == IntValue(7)
    assert owner() is None


def test_retained_public_session_callback_keeps_its_context_alive() -> None:
    session = ReplSession(default_stdlib=False)
    owner = ref(session)
    session.open()
    callback = session.type_of
    del session

    assert owner() is not None
    assert callback("7") == "int"
    retained = owner()
    assert retained is not None
    retained.close()
    del callback, retained
    assert owner() is None


def test_host_debug_callback_retains_only_the_live_register_across_reset() -> None:
    session = ReplSession()
    owner = ref(session)
    assert not session.open()
    assert session.eval_entry("import std/config").ok
    assert session.eval_entry("std/config::debug := true").ok
    host = session._host
    assert host.debug()
    session.reset()
    assert host.debug() == session._host.debug()
    assert session.eval_entry("import std/config").ok
    assert session.eval_entry("std/config::debug := false").ok
    assert not host.debug()
    session.close()
    del session

    assert owner() is None
    assert not host.debug()


def test_static_verdict_preserves_diagnostics_and_causal_objects_without_frames() -> None:
    payload = _Payload()
    owner = ref(payload)
    cause = _raised(ValueError("cause"), payload)
    context = _raised(RuntimeError("context"), payload)
    failure = AglTypeError("rejected")
    failure.__cause__ = cause
    failure.__context__ = context
    failure.add_note("semantic note")
    diagnostics = failure.diagnostics()

    def reject() -> None:
        raise failure

    verdict = _static_verdict(reject)
    del payload
    assert verdict is failure
    assert verdict.diagnostics() == diagnostics
    assert verdict.__cause__ is cause and verdict.__context__ is context
    assert verdict.__notes__ == ["semantic note"]
    assert verdict.__traceback__ is cause.__traceback__ is context.__traceback__ is None
    assert owner() is None


def test_static_verdict_handles_shared_and_cyclic_causal_links() -> None:
    failure = AglSyntaxError("rejected", span=dummy_span())
    cause = AglError("cause")
    failure.__cause__ = failure.__context__ = cause
    cause.__cause__ = failure

    def reject() -> None:
        raise failure

    assert _static_verdict(reject) is failure
    assert failure.__cause__ is failure.__context__ is cause
    assert cause.__cause__ is failure
    assert failure.__traceback__ is None


def test_unexpected_probe_exception_keeps_its_traceback() -> None:
    def reject() -> None:
        raise RuntimeError("unexpected")

    with pytest.raises(RuntimeError) as caught:
        _static_verdict(reject)
    assert caught.value.__traceback__ is not None


def test_reraising_a_static_verdict_produces_a_new_traceback() -> None:
    def reject() -> None:
        raise AglTypeError("rejected")

    verdict = _static_verdict(reject)
    assert isinstance(verdict, AglError)
    assert verdict.__traceback__ is None
    with pytest.raises(AglError) as caught:
        raise verdict
    assert caught.value is verdict
    assert verdict.__traceback__ is not None


def test_static_failure_does_not_prevent_later_evaluation() -> None:
    session = ReplSession(default_stdlib=False)
    session.open()
    try:
        assert not session.eval_entry("missing_name").ok
        assert session.eval_entry("let value = 7").ok
        assert session.eval_entry("value").value == IntValue(7)
    finally:
        session.close()


def test_expected_probe_callback_remains_callable() -> None:
    def reject() -> None:
        raise AglTypeError("rejected")

    callback: Callable[[], None] = reject
    first = _static_verdict(callback)
    second = _static_verdict(callback)
    assert isinstance(first, AglError) and isinstance(second, AglError)
    assert first is not second
    assert first.diagnostics() == second.diagnostics()
