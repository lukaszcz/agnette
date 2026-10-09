"""AgL lowering phase — compile-time IR emission.

Transforms a successful ``MatchCompiledProgram`` into an ``ExecutableProgram``
for evaluation. All implicit coercions are resolved at compile time and
emitted as explicit ``IrCoerce`` nodes; the evaluator switches only on
pre-resolved ``Coercion`` descriptors and never sniffs value types at
runtime.

Public API
----------
- :func:`lower_program` — whole-program lowering entry point.
- :func:`lower_repl_program` — incremental whole-program lowering for one
  REPL entry.
"""

from agnette.agl.lower.program import lower_program
from agnette.agl.lower.repl import LinkImage, lower_repl_program

__all__ = [
    "LinkImage",
    "lower_program",
    "lower_repl_program",
]
