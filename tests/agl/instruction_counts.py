"""Count the bytecode instructions a function (and the code nested in it) executes."""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import CodeType, FunctionType


def _nested_code(code: CodeType) -> list[CodeType]:
    """*code* and every code object nested in it (generator expressions, inner functions)."""
    found = [code]
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            found.extend(_nested_code(constant))
    return found


def instructions_run_in(function: FunctionType, action: Callable[[], object]) -> int:
    """Return how many instructions *function* and its nested code raise while *action* runs."""
    monitoring = sys.monitoring
    tool = monitoring.PROFILER_ID
    codes = _nested_code(function.__code__)
    instructions = 0

    def on_instruction(_code: CodeType, _offset: int) -> None:
        nonlocal instructions
        instructions += 1

    monitoring.use_tool_id(tool, "instruction-counts")
    try:
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, on_instruction)
        for code in codes:
            monitoring.set_local_events(tool, code, monitoring.events.INSTRUCTION)
        action()
    finally:
        for code in codes:
            monitoring.set_local_events(tool, code, 0)
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, None)
        monitoring.free_tool_id(tool)
    return instructions
