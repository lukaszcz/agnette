"""Interpreter scratch ownership must not retain completed run inputs."""

from __future__ import annotations

from pathlib import Path
from weakref import ref

import pytest

from agnette.agl.eval.ir_interpreter import IrInterpreter
from agnette.agl.ir.ids import FunctionId, SymbolId
from agnette.agl.runtime.host_session import HostSession
from agnette.agl.runtime.host_settings import HostSettingsPolicy, HostSettingsReconfigurer
from agnette.agl.runtime.sessions import AgentDispatcherSessionHost
from agnette.agl.runtime.trace import TraceStore
from agnette.agl.semantics.values import UNIT_VALUE, BoolValue, Cell, Frame, IrClosureValue
from tests.agl.ir_harness import lower_inline_ir
from tests.test_agl_ir_interpreter import _make_program

pytestmark = pytest.mark.usefixtures("no_cyclic_gc")


class _TraceOwner:
    def place(self, file: str | None, line: int, col: int | None) -> tuple[str, int, int | None]:
        return file or "<test>", line, col


@pytest.mark.parametrize("run", [False, True])
@pytest.mark.parametrize("shared_host", [False, True])
def test_dropped_interpreter_releases_trace_and_its_callback_owner(
    run: bool, shared_host: bool
) -> None:
    owner = _TraceOwner()
    owner_ref = ref(owner)
    trace = TraceStore(None, place=owner.place)
    trace_ref = ref(trace)
    host = (
        HostSession(AgentDispatcherSessionHost(None), fallback_debug=lambda: False)
        if shared_host
        else None
    )
    interpreter = IrInterpreter(_make_program(()), trace=trace, host=host)
    if run:
        assert interpreter.run() == {}
    del owner, trace, interpreter

    assert trace_ref() is None
    assert owner_ref() is None
    if host is not None:
        assert not host.debug()


def test_shared_host_keeps_opaque_companion_state_after_interpreter_drop() -> None:
    host = HostSession(AgentDispatcherSessionHost(None), fallback_debug=lambda: False)
    handle = host.state.get_or_create("opaque", _TraceOwner)
    handle_ref = ref(handle)
    trace = TraceStore(None)
    trace_ref = ref(trace)
    interpreter = IrInterpreter(_make_program(()), trace=trace, host=host)
    interpreter.run()
    del interpreter, trace, handle

    assert trace_ref() is None
    assert host.state.get_or_create("opaque", _TraceOwner) is handle_ref()
    host.state.close_all()
    assert handle_ref() is None


def test_borrowed_frame_and_recursive_function_value_are_not_cleared() -> None:
    symbol = SymbolId(99)
    cell = Cell(UNIT_VALUE)
    closure = IrClosureValue(FunctionId(99), ((symbol, cell),))
    cell.value = closure
    frame: Frame = {symbol: cell}
    trace = TraceStore(None)
    trace_ref = ref(trace)
    interpreter = IrInterpreter(_make_program(()), trace=trace, base_frame=frame)
    interpreter.run()
    del interpreter, trace

    assert trace_ref() is None
    assert frame[symbol] is cell
    assert cell.value is closure
    assert closure.captures == ((symbol, cell),)


def test_escaped_bound_callback_keeps_interpreter_context_until_dropped() -> None:
    trace = TraceStore(None)
    trace_ref = ref(trace)
    interpreter = IrInterpreter(_make_program(()), trace=trace)
    callback = interpreter.run
    del interpreter, trace

    assert trace_ref() is not None
    assert callback() == {}
    del callback
    assert trace_ref() is None


def test_owned_host_debug_retains_registers_not_the_completed_interpreter() -> None:
    executable = lower_inline_ir("import std/config\nstd/config::debug := true\n()")
    trace = TraceStore(None)
    trace_ref = ref(trace)
    interpreter = IrInterpreter(executable, trace=trace)
    host = interpreter._host
    assert not host.debug()
    interpreter.run(program_symbol=executable.synthetic_main_symbol)
    del interpreter, trace

    assert host.debug()
    assert trace_ref() is None


def test_owned_host_debug_stays_live_after_trace_reconfiguration_rollback() -> None:
    executable = lower_inline_ir(
        "import std/config\n"
        "if std/config::debug =>\n"
        "  std/config::debug := false\n"
        "else =>\n"
        "  std/config::debug := true\n"
        "  std/config::trace := true\n"
        "()"
    )

    def resolve_trace_path(enabled: bool, trace_file: str | None) -> Path | None:
        if enabled:
            raise RuntimeError("trace service unavailable")
        return None

    trace = TraceStore(None)
    reconfigurer = HostSettingsReconfigurer(
        trace=trace, policy=HostSettingsPolicy(resolve_trace_path)
    )
    interpreter = IrInterpreter(executable, trace=trace, host_reconfigurer=reconfigurer)
    host = interpreter._host
    with pytest.raises(RuntimeError):
        interpreter.run(program_symbol=executable.synthetic_main_symbol)
    assert interpreter.builtin_host_settings["trace"] == BoolValue(False)
    assert interpreter.debug and host.debug()

    interpreter.run(program_symbol=executable.synthetic_main_symbol)
    assert not interpreter.debug
    assert not host.debug()


def test_returned_closure_keeps_captures_without_retaining_run_scratch() -> None:
    executable = lower_inline_ir(
        "def make(x: int) -> (int) -> int = fn(y: int) => x + y\nlet add = make(7)\n()"
    )
    trace = TraceStore(None)
    trace_ref = ref(trace)
    interpreter = IrInterpreter(executable, trace=trace)
    result = interpreter.run(program_symbol=executable.synthetic_main_symbol)
    del interpreter, trace

    assert trace_ref() is None
    caller_trace = TraceStore(None)
    caller_trace_ref = ref(caller_trace)
    caller = IrInterpreter(executable, trace=caller_trace)
    closure = result["add"]
    assert isinstance(closure, IrClosureValue)
    proxy = caller._make_extern_callable_proxy(closure)
    window = caller._extern_call_window()
    del caller, caller_trace

    assert caller_trace_ref() is not None
    with window:
        assert proxy(5) == 12
    del proxy
    assert caller_trace_ref() is None
