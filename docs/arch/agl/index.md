# AgL Language Implementation

AgL (Agnette Language) is the statically typed workflow DSL run by AgM (Agnette Manager); agent and shell calls are ordinary typed expressions. The implementation is a conventional compiler frontend, a typeless execution layer, and a host runtime, driven by one pipeline orchestrator that `agm exec`, `agm repl`, `agm check`, package validation, and registered package commands all share.

## Compilation Pipeline

Every AgL program flows through one pipeline, whether run whole or one REPL entry at a time:

```
source (.agl)
  → lexer        (INDENT/DEDENT, string and environment interpolation, one NAME token class)
  → parser       (Lark LALR grammar)
  → AST          (frozen dataclasses — the firewall)
  → scope        (whole-program name resolution, user-operator grouping)
  → typecheck    (whole-program checking; selects concrete operations)
  → match compile (exhaustiveness, redundancy, decision DAGs)
  → lower + link (closed, typeless executable program)
  → IR eval      (interpreter over the linked program)
        ↘ host runtime: agents and sessions, shell, Python FFI, codecs, rendering, tracing
```

The linked IR is the only execution format; checked frontend objects never reach the evaluator. Each pass wraps its input artifact and records its conclusions in side tables keyed by stable node ids. No pass mutates the AST or rewrites another pass's tables; where scope cannot decide (a field-directed pattern name, an ambiguous bare constructor), it emits a slot or candidate set that typecheck resolves and later passes read through checked-artifact accessors.

## The Firewall

The lexer and parser are the only Lark-aware code. Every pass from scope onward depends solely on the AST dataclasses, so the front end is replaceable without touching the static passes or the evaluator. One consequence shapes the whole codebase: identifier case carries no semantic category. Types, constructors, and variables are told apart by their declaration and binding namespace, never by spelling, and no pass branches on capitalization.

## Shared Agnette Layers

The firewall is semantic, not an I/O boundary. `agnette.agl` and `agnette.agm` use one shared service layer under `agnette`, with no duplicated implementation:

- **Agents** come from `agnette.agent`: `Agent` values decode into host specs, asks dispatch through the shared runner or the session service, and `runtime/sessions.py` bridges AgL session values to it ([agents.md](../agents.md)).
- **Primitives** come from `agnette.core`: shell `exec` and CLI agent subprocesses use `core.process`, environments `core.env`, files and trace logs `core.fs`/`core.log`, outbound HTTP `core.http` (the `std/http` companion's transport). `agnette.util` supplies pure helpers.
- **Configuration** comes from `agnette.config`: the engine-key catalog and qualified config tables feed the hosts' seeds ([hosting.md](hosting.md)).
- **Packages** come from `agnette.packages`: manifests, mounted identities, and standard-library selection share the manager's package model ([packages.md](../packages.md)).
- **Sandboxing** comes from `agnette.sandbox`, shared by agent and shell execution ([sandbox.md](../sandbox.md)).

The lazy `agnette.agl` façade preserves leaf imports without loading the execution stack. The temporary `agl` module is the Python companion API, not the implementation namespace ([execution/ffi.md](execution/ffi.md)).

## Expression-Oriented Design

AgL has no statement category. Bindings, assignment, loops, and `if` without `else` are expressions with a type, and a block yields its last item. Built-ins such as `print`, `exec`, and `ask` are ordinary calls classified during resolution; runtime built-in references are first-class, occurrence-specialized function values. A `$` literal is a template like a quoted string, so it reaches `exec`/`ask` through the same single-argument call sugar as any other template. Methods are selected from reachable declarations by receiver type for nominal and builtin receivers alike; lowering consumes the checker's `method_selections` unchanged. This uniformity is why the AST has a single call node and why the type system carries a unit type.

## Programs and Modules

A **program** is the entry module plus its transitive import and re-export dependencies; the pipeline assembles and validates the graph while reusing precompiled module interfaces and bodies. A `program def` is an ordinary function that is also a host-discoverable entry. Every module except `std/prelude` receives the `std/prelude` prelude unless the host disables it. Module loading, visibility, and the compilation caches are in [modules.md](modules.md).

## Package Map

| Stage | Package |
|---|---|
| Lexer | `src/agnette/agl/lexer/` |
| Parser / grammar | `src/agnette/agl/parser/`, `src/agnette/agl/grammar/` |
| AST | `src/agnette/agl/syntax/` |
| Scope / name resolution | `src/agnette/agl/scope/`, `src/agnette/agl/infix.py` |
| Type checking | `src/agnette/agl/typecheck/` |
| Match compilation | `src/agnette/agl/matchcompile/` |
| Semantic foundation (values, types, type table, analyses, exceptions) | `src/agnette/agl/semantics/` |
| Literal lexical rules and the value-syntax reader | `src/agnette/agl/value_syntax/` |
| Lowering / linking | `src/agnette/agl/lower/` |
| Execution IR | `src/agnette/agl/ir/` |
| Evaluator | `src/agnette/agl/eval/` |
| Host runtime services and FFI | `src/agnette/agl/runtime/` |
| Module loading | `src/agnette/agl/modules/` |
| REPL | `src/agnette/agl/repl/` |
| Pipeline orchestrator and host leaves | `src/agnette/agl/pipeline.py`, `capabilities.py`, `diagnostics.py`, `type_schema.py`, `artifact_cache.py`, `artifact_storage.py`, `self_validation.py` |

Layering is enforced by `tests/test_agl_dependencies.py`: `semantics` is the foundation, `syntax` is an AST-only leaf, `typecheck` reaches only scope's output and the layers beneath it, `matchcompile` imports nothing downstream, the IR depends only on its own data, the engine-key catalog, and the constraint-kind leaf, the evaluator never imports the frontend, the runtime is eval-free, and the pipeline sits on top. `agl/value_syntax/` is a leaf below the lexer, match compiler, and runtime, holding the literal scanning rules (text escapes, numbers, identifiers, environment holes) and a reader for AgL's data-only value syntax; it imports only `agnette.util` and `agl/keywords.py`. `agl/zones.py`, `agl/attributes.py` (the built-in attribute catalog, which names zones and carries the host-facing shapes the `@opt-*` and `@command` attributes describe; it uses the shared root leaf `agnette.command_catalog` to hold a `@command` path to the CLI's own reserved-name rule), `agl/modules/ids.py`, and `agl/constraints.py` (the `Eq`/`Hashable` structural constraint kinds) are the vocabulary leaves below every pass, so both scope and the IR can name a parameter's zone and a module's identity without seeing each other, and `syntax`/`semantics`/`ir` can each name a constraint kind — carried on a function signature or descriptor's `bounds` — without importing each other. `artifact_storage.py` is a further leaf: the disk-cache envelope shared by the module cache, artifact serialization, and the runtime's companion bytecode cache, importing no manager code under `agnette.agm`.

## What To Read Next

- [frontend/index.md](frontend/index.md) — lexer, parser, AST, scope, typecheck, match compilation.
- [execution/index.md](execution/index.md) — lowering, the IR, the evaluator, the host runtime, and the Python FFI.
- [modules.md](modules.md) — the file-based module system, the standard library's surfaces, and the compilation caches.
- [hosting.md](hosting.md) — the pipeline orchestrator, host capabilities, parameters, engine settings, diagnostics.
- [repl.md](repl.md) — the incremental REPL session and its front ends.

The language itself is documented for users in `docs/agl/reference/`; the standard library's sources under `packages/stdlib/src/` are its own reference.
