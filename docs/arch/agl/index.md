# AgL Language Implementation

AgL (Agnette Language) is the statically typed workflow DSL run by AgM; agent and shell calls are ordinary typed expressions. The implementation is a compiler frontend, a typeless execution layer, and a host runtime. `PipelineDriver` drives `agm exec`, `agm check`, and registered package commands; the REPL reuses its passes incrementally, and package validation stops after scope.

## Compilation Pipeline

Every program, whole or one REPL entry at a time, flows through one pipeline:

```
source (.agl)
  → lexer        (INDENT/DEDENT, interpolation, one NAME token class)
  → parser       (Lark LALR grammar)
  → AST          (frozen dataclasses — the firewall)
  → scope        (whole-program name resolution, operator grouping)
  → typecheck    (whole-program checking; selects concrete operations)
  → match compile (exhaustiveness, redundancy, decision DAGs)
  → lower + link (closed, typeless executable program)
  → IR eval      (interpreter over the linked program)
        ↘ host runtime: agents and sessions, shell, Python FFI, codecs, rendering, tracing
```

- The linked IR is the only execution format; checked frontend objects never reach the evaluator.
- Passes never mutate the AST or another pass's output: each records conclusions in side tables keyed by stable node ids. Where scope cannot decide, it emits candidates that typecheck resolves.

## The Firewall

The lexer and parser are the only Lark-aware code; every later pass depends only on the AST dataclasses. Identifier case carries no meaning: types, constructors, and variables are distinguished by declaration namespace, never by spelling.

## Shared Agnette Layers

The firewall is semantic, not an I/O boundary. AgL uses the shared services under `agnette` without duplicating them:

- **Agents** — `agnette.agent`: specs, runner, sessions ([agents.md](../agents.md)).
- **Primitives** — `agnette.core` (process, env, fs, log, HTTP) and `agnette.util` ([core.md](../core.md)).
- **Configuration** — `agnette.config`: the engine-key catalog and qualified config tables ([config.md](../config.md)).
- **Packages** — `agnette.packages`: manifests, mounting, standard-library selection ([packages.md](../packages.md)).
- **Sandboxing** — `agnette.sandbox`, shared by agent and shell execution ([sandbox.md](../sandbox.md)).

The lazy `agnette.agl` façade preserves leaf imports. The temporary `agl` module is the Python companion API, not the implementation namespace ([execution/ffi.md](execution/ffi.md)).

## Expression-Oriented Design

AgL has no statement category: bindings, assignment, loops, and `if` without `else` are typed expressions, and a block yields its last item. Built-ins such as `print`, `exec`, and `ask` are ordinary calls classified during resolution and are first-class function values. Methods are selected by receiver type for nominal and builtin receivers alike. Hence one call node in the AST and a unit type in the type system.

## Programs and Modules

A **program** is the entry module plus its transitive import and re-export dependencies. A `program def` is an ordinary function that is also a host-discoverable entry. See [modules.md](modules.md).

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

## Layering

A test enforces the import contract:

- The IR's data modules and the vocabulary leaves are the foundation; `semantics` builds on them; `syntax` is an AST-only leaf.
- `typecheck` reaches only scope's output and lower layers; `matchcompile` imports nothing downstream.
- The IR depends only on its own data, the engine-key catalog, and the vocabulary leaves; the evaluator imports no frontend module directly; the runtime is eval-free; the pipeline sits on top.
- Vocabulary leaves below every pass: `value_syntax/` (literal rules and the value-syntax reader), `keywords.py`, `zones.py`, `attributes.py` (built-in attribute catalog), `modules/ids.py`, `constraints.py` (`Eq`/`Hashable` kinds). They let scope, typecheck, and the IR share names without importing each other.
- `artifact_storage.py` is the disk-cache envelope shared by the module cache, artifact serialization, and companion bytecode.

## What To Read Next

- [frontend/index.md](frontend/index.md) — lexer, parser, AST, scope, typecheck, match compilation.
- [execution/index.md](execution/index.md) — lowering, the IR, the evaluator, the host runtime, and the Python FFI.
- [modules.md](modules.md) — the module system and the compilation caches.
- [hosting.md](hosting.md) — the pipeline orchestrator, host capabilities, parameters, engine settings, diagnostics.
- [repl.md](repl.md) — the incremental REPL session and its front ends.

The language is documented for users in `docs/agl/reference/`; the standard library's sources under `packages/stdlib/src/` are its own reference.
