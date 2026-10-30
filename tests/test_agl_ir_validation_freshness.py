"""Validation-local metadata must never outlive a mutable program table."""

from dataclasses import replace

import pytest

from agnette.agl.ir.ids import Location, SourceId, SymbolId
from agnette.agl.ir.nodes import IrAssign, IrConstInt
from agnette.agl.ir.program import (
    ExecutableModule,
    ExecutableProgram,
    SourceFile,
    SymbolDescriptor,
)
from agnette.agl.ir.validate import InvalidIrError, validate_ir
from agnette.agl.modules.ids import ModuleId


def _program() -> ExecutableProgram:
    module = ModuleId.from_path("validation")
    source = SourceId(0)
    symbol = SymbolId(0)
    location = Location(source, 0, 4, 1, 0)
    value = IrConstInt(location, 1234)
    assignment = IrAssign(location, symbol, value)
    return ExecutableProgram(
        entry_module=module,
        modules={module: ExecutableModule(module, (assignment, assignment))},
        sources={source: SourceFile("validation.agl", "1234")},
        symbols={symbol: SymbolDescriptor(symbol, True, "value", module)},
        nominals={},
    )


@pytest.mark.parametrize("remove", [False, True])
def test_shared_expression_rechecks_replaced_or_removed_source(remove: bool) -> None:
    program = _program()
    validate_ir(program)

    if remove:
        program.sources.clear()
    else:
        program.sources[SourceId(0)] = SourceFile("short.agl", "1")

    with pytest.raises(InvalidIrError):
        validate_ir(program)
    validate_ir(program, deep=False)

    program.sources[SourceId(0)] = SourceFile("restored.agl", "1234")
    validate_ir(program)


def test_shared_expression_rechecks_symbol_mutability() -> None:
    program = _program()
    symbol = SymbolId(0)
    descriptor = program.symbols[symbol]
    validate_ir(program)

    program.symbols[symbol] = replace(descriptor, mutable=False)
    with pytest.raises(InvalidIrError):
        validate_ir(program)
    validate_ir(program, deep=False)

    program.symbols[symbol] = descriptor
    validate_ir(program)


def test_shared_expression_has_no_validity_in_another_program() -> None:
    program = _program()
    validate_ir(program)

    other = replace(program, sources={SourceId(0): SourceFile("short.agl", "")})
    with pytest.raises(InvalidIrError):
        validate_ir(other)
    validate_ir(program)
