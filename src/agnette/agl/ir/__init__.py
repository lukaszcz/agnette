"""AgL typeless execution IR.

This package contains the immutable data model for the execution IR emitted
by the lowering/linking phase and consumed by the evaluator. Names live in
the submodules (``nodes``, ``program``, ``contracts``, ``operations``, ``ids``,
``builtin_nominals``, ``validate``); the package re-exports nothing.

The IR carries no checker ``Type``, ``TypeEnvironment``, ``FunctionSignature``,
``CastSpec``, type expression, or ``node_types``/``binding_types`` table.

Imports: only stdlib, ``agnette.agl.modules.ids``, and the pure shared engine-key
catalog. No eval, runtime, values, syntax, scope, or typecheck imports are
permitted in this package.
"""
