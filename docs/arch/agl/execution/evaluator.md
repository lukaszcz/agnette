# AgL Evaluator

The evaluator interprets the linked program and never imports the frontend. `run()` initializes modules in link order, then invokes the host-selected entry symbol with pre-evaluated arguments under a pinned decimal context and a recursion boundary; it never selects an entry or decodes host values itself. Host seeds replace a static binding's initializer. Frames hold `let` and parameters directly and `var` bindings in shared cells. The evaluator trusts the IR: it performs no runtime shape checks against static invariants ([lowering.md](lowering.md#the-ir)).

## Values and Aliasing

- Arrays, dicts, and records are shared by reference, so in-place updates are visible through every alias; text and exceptions are immutable. An enum-typed slot holds the member record directly.
- Cycles are possible: rendering and JSON encoding raise catchable `CyclicValueError`; equality and `copy` terminate (`semantics/copying.py`).
- `DictValue` encapsulates its storage, with a fast representation for `text` keys and a canonical-token representation for other hashable keys.
- Iterators see mutations ahead of the cursor but cannot be extended past their entry-time length.

## Control Flow

`break`, `continue`, and `return` are internal signals caught only by their owning construct, so they unwind through `try`/`catch`, which catches only AgL raises. A specific `catch` matches by nominal conformance along the `base` chain. Recursion is bounded by `max_call_depth`, raising a catchable `RecursionError` before Python's limit. A span-less raise takes the location of the innermost IR node it unwinds through.

## Host-Backed Operations

Effects dispatch through `eval/effects.py`, which composes prompts and records requests and responses for tracing:

- **Agents.** An explicit-agent `ask` opens an ephemeral session spanning its parse-retry loop; a free `ask` uses the default session; session nodes drive persistent handles ([runtime.md](runtime.md)).
- **Shell.** `exec` returns `ExecResult` or decodes stdout per its contract; spawn failures, timeouts, and sandbox-preparation failures raise `ExecError`. A `sandbox` operand routes through the shared sandbox library ([../../sandbox.md](../../sandbox.md)).
- **Conversions.** Casts and value parsing execute pre-resolved recipes and parse strictly, raising `CastError`/`ValueParseError`; agent and `exec` output use the configurable strict/lenient codec. Casts and `is` share one nominal conformance check.
- **Host-minted values** (built-in exceptions, `ExecResult`, `AgentRequest`, `Option` members) take identity from the program's builtin nominal table, so they behave like source-constructed values.
- **Externs** delegate to the FFI registry ([ffi.md](ffi.md)).

## Code Entry Points

- `src/agnette/agl/eval/ir_interpreter.py` — interpreter, frames, entry invocation, recursion boundary.
- `src/agnette/agl/eval/effects.py` — agent and shell effects; `conversions.py`, `arith.py`, `indexing.py` — casts, arithmetic, container access.
- `src/agnette/agl/semantics/values.py`, `copying.py`, `cycles.py`, `arithmetic.py` — runtime values and equality, copying, cycle guards, arithmetic signals; `src/agnette/util/decimal.py` — the pinned decimal context.
