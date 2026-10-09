# AgL Lowering and the Execution IR

## Lowering and Linking

Lowering consumes the match-compiled program and emits one linked, typeless executable. It translates expressions directed by expected types, allocates program-local symbols, functions, contracts, and sources (nominal identities are declaration handles), and links modules in dependency order, ordering static initializers so a binding precedes its readers.

- **Type facts are read, never re-inferred.** Argument bindings, selected constructors and methods, cast kinds, and contract specs come from checker side tables; lowering compiles them into typeless recipes, schemas, and encode plans (`coercions.py`, `conversions.py`, `type_schema.py`), and type arguments are erased. Nominal identity survives as `NominalId`; shapes come from the shared `TypeTable`. Checker-guaranteed shapes are trusted, not re-checked.
- **Modular reuse.** Imported modules lower in stable allocation namespaces, so their IR persists independently (`lower/module.py`); resource paths are revalidated on reuse. REPL linking keeps a session-owned allocation image ([../repl.md](../repl.md)).
- **Host surface.** Each `program def` gets a host-facing parameter signature; `@param` bindings keep their binding symbols and decoders; `@config` entries lower to checked constant expressions ([../hosting.md](../hosting.md)).

Notable shapes:

- **Built-in and operator values** lower to synthetic closures; direct calls, closures, and bound methods converge on one operand-level dispatch, so every route emits the same host-operation IR.
- **Methods** lower to direct receiver-first calls; `Session` operations to dedicated session nodes; a free `ask` to the default session.
- **Match sites** traverse the compiled decision DAG: `IrCase` switches on member-record identity or a literal key, and its arms bind the fields they need; irrefutable decompositions project through `IrField`.
- **Catch clauses** lower to the caught type's nominal identity; `_` and `Exception` are catch-all.
- **Externs** carry the declared name (for diagnostics) and the companion name from scope's `extern_names` ([../frontend/scope.md](../frontend/scope.md)). Type-directed externs take leading `IrContract` operands; value occurrences eta-expand into closures.
- **Resources** resolve at link time to absolute paths against the declaring module; a missing or escaping target stops linking.
- **Defaults.** Omitted call arguments and omitted defaulted constructor fields share one `UseDefault` sentinel; field defaults live on `NominalDescriptor.field_defaults`.

## The IR

The IR (`agl/ir/`) is a runtime-neutral data model: identities and source locations, a closed family of expression nodes, and a program container holding modules, symbols, functions, entry symbols, sources, nominals, contracts, and host-backed binding identities.

- `ask`/`exec` nodes reference contract requests in `contracts` (unit, text, or JSON) with format instructions, JSON schema, and decode walk; the evaluator parses agent and shell output from these alone.
- `target_contracts` holds type-directed extern targets (label, JSON Schema, typeless `TypeTree`); `program_signatures` and `program_configs` hold host-facing program data.
- Nominal descriptors are closed over every referenced record and exception `base` chain, so runtime conformance never needs the frontend.
- Each program carries only the function and symbol descriptors of modules it actually links ([../repl.md](../repl.md#retained-library-image)).
- `validate_ir` self-checks lowering output only under the self-validation toggle ([../../testing.md](../../testing.md)); the evaluator trusts the IR.

## Code Entry Points

- `src/agnette/agl/lower/lowerer.py`, `program.py` — lowering and linking; `coercions.py`, `conversions.py` — coercion and cast recipes; `module.py` — persisted module IR; `repl.py` — incremental linking.
- `src/agnette/agl/ir/` — nodes, identities, contracts, the program container, builtin tables, `validate.py`.
