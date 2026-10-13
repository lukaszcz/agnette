# AgL Type System and Checking

The semantic type model lives in `semantics/` and is consumed by `typecheck/`. Besides scalars, containers, records, enums, and exceptions it has unit, function types, a bottom type for divergence, and rigid type variables for generics. Solver inference variables are distinct from source variables and never escape checked output.

## Nominal Types Are Handles

Record, enum, and exception types are handles: identity is the declaration (`decl_id`) plus type arguments, never structure or path. All shape lookup — fields, enum membership, exception hierarchy, methods — goes through the shared `TypeTable` (`semantics/type_table.py`), keyed by declaration identity.

- Handles resolve regardless of build order, so recursive declarations (mutual, generic, cross-module) need no acyclicity rule.
- A later declaration can take a path's name without invalidating earlier handles, which lets the REPL supersede declarations ([repl.md](../repl.md)).
- Named types and qualified owners are read from scope's `owner_declarations`, never re-resolved ([scope.md](scope.md#declarations-and-aliases)).
- An enum is a closed nominal union of member record declarations; widening (member → enum, enum → superset enum) is directed and does not propagate through containers.
- Built-in and host-contract shapes (`ExecResult`, `AgentRequest`, `Option`, the exception hierarchy) live in the same table: loaded standard-library declarations are authoritative and validated against contracts in `typecheck/builtin_contracts.py`. Lowering derives its host nominal table from the same queries.

## Whole-Table Analyses

Recursive types make some questions global; `semantics/analyses.py` answers them over the whole table as cached declaration-graph analyses:

- **Inhabitation** — a declaration that can never produce a finite value is rejected.
- **Data properties** — `Eq`, `Hashable`, JSON convertibility, and extern keyability share one parameterised fixpoint; key-sensitive properties propagate through declarations' key parameters.
- **Finite-schema closure** — whether a type admits a finite JSON schema, required wherever a schema is needed (agent and `exec` outputs, fallible casts, parameters, extern signatures). `as json` encode planning consults it ([execution/runtime.md](../execution/runtime.md)).

## The Typecheck Pass

The checker selects the concrete behavior the evaluator relies on and publishes it in checked-program side tables, so lowering never re-infers:

- **Member selection** — one field/method rule for selection and assignment targets; method candidates form one flat level — a record's own plus its owning enums', an exception's own plus its ancestors'; `method_selections` is consumed by lowering unchanged.
- **Built-ins** — dispatched from scope's classification; `ask`/`exec` take result types from context, with documented defaults applied only after the inference region closes. A built-in's named options come from its resolved `builtin def`, validated against the canonical signatures in `typecheck/checker.py`. Output contracts (schema and format metadata) are computed here.
- **Casts and parsing** — one classification (`cast_classification`, beside the type table) gives each source/target pair a cast kind: total, nominal up/downcast, fallible, or a static error; exception casts and `catch` use `extends` ancestry; `as json` becomes a static encode plan.
- **Generics** — rank-1: rigid at the definition, freshly instantiated per use inside a solver region (`typecheck/inference.py`), erased after checking. Constraint bounds are checked as deferred obligations; one structural predicate decides `Eq`/`Hashable`.
- **Functions** — declared headers go through `function_inference.py` and carry the declaration's `@exempts` set on the `FunctionSignature`, so imported and cached functions keep it; unannotated results are inferred per import SCC, then every body is rechecked against the complete signature table, and only the recheck publishes artifacts.
- **Module-level bindings** — a program-wide pre-pass (`typecheck/program.py`) types every static-root module's exported bindings, after headers and before function inference, ordering same-module constants by dependency (`typecheck/constant_bindings.py`).
- **Arguments and patterns** — one zone-binding algorithm (`semantics/arguments.py`, AST-free) serves calls, constructors, and constructor patterns; `typecheck/arguments.py` wraps it. Exhaustiveness belongs to match compilation.
- **Blocks** — take their last item's type; discarded items must be unit or bottom.
- **`exec` command holes** — `typecheck/shell_holes.py` walks the templates in a direct `exec` call's command result positions (parentheses, arms, a block's final item, `try`/`catch` results; never bindings, pipes or operators) and warns `unquoted-exec-hole` on each hole that is not `int`/`decimal`/`bool`, a constant expression over fixed bindings (`ModuleConstantBindings.is_fixed`: no `var`, no `@param`), or a direct call of a function whose signature `exempts` it. It also warns `shell-quoted-hole` on a direct call of `std/exec`'s `shell-quote`/`shell-join` (identified by resolved declaration, so every `%!{…}`; not by `@exempts`) inside shell quotes the author wrote. The quote state comes from the pure `util/posix_shell.py` scan over the template's decoded text: fixed-constant holes are folded (`fold_hole_text`) into the text, scalar and quoting-call holes are words, any other hole is opaque and ends judgement, as does any construct the scan does not model. It runs at region close, when hole types are final, and a module's warnings are reported in source order.
- **`extern def`** — shares the body-less signature path; type parameters no value parameter mentions are target parameters resolved from context like `ask`'s ([execution/ffi.md](../execution/ffi.md)).
- **`@config`** — keys resolve by ordinary scope lookup; values are checked as constants against the target's type ([hosting.md](../hosting.md)).
- **Nominal metadata** — external names, docs, field zones, and field-default *presence* are stored on each `TypeDef`; default expressions themselves are lowered with their declaration ([execution/lowering.md](../execution/lowering.md)).

Checked artifacts hold concrete types and no solver state; that closure invariant is a self-check under the self-validation toggle ([testing.md](../../testing.md)). While a module is checked, its own declarations are journaled as replayable facts, which lets a checked module persist and rehydrate onto a later compilation's environment ([modules.md](../modules.md)). Checked facts are final once sealed.

## Code Entry Points

- `src/agnette/agl/semantics/` — values, semantic types, `TypeTable`, whole-table analyses, exceptions, the pure argument binder, arithmetic classification (over `src/agnette/util/decimal.py`).
- `src/agnette/agl/typecheck/` — the checker, builtin rules and contracts, `exec` hole warnings, the inference solver, function-header inference, binding pre-pass, declaration validation.
- `src/agnette/agl/type_schema.py` — compile-time JSON schema, decode, and encode-plan derivation.
