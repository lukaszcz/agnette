# AgL REPL

`agm repl` evaluates one entry at a time with the same passes and runtime as `agm exec`. This document covers what makes it incremental.

## Incremental Sessions

`ReplSession` keeps a persistent environment: each entry is compiled and evaluated exactly once against accumulated declarations, bindings, imports, `use`s, fixities, and runtime values, so agent calls never replay.

- **File-mode equivalence.** An entry resolves in source order exactly as a file does; verdicts never depend on how source is split into entries.
- **Supersession.** A redeclaration at the same path is a fresh declaration identity; retained values, aliases, and methods keep the shape they were checked against. Imports and `use`s replace retained ones as scope decides.
- **Failure.** A runtime-failed entry promotes only what its initializer reached and leaves the prior generation intact. `check_only` entries check without evaluating or promoting.
- **State.** Each entry runs in a fresh interpreter; engine-setting writes and seeded module parameters persist as ordinary state. Companion `runtime.state` is one session-owned bag ([execution/ffi.md](execution/ffi.md)). `:reset` clears everything, closing agent sessions and that bag.

## Retained Library Image

Every entry compiles a whole program, but library modules are compiled once: the session reuses each module's checked artifacts while the graph holds the same AST object; a fresh default-stdlib session without a parameter-seed resolver (so never `agm repl` itself) starts from a cached checked bootstrap image. Lowering allocates into a persistent `LinkImage` (`lower/repl.py`); allocations of modules a failed entry left incomplete are kept for reuse but never linked ([execution/lowering.md](execution/lowering.md#the-ir)).

## Front-End Seam

The UI-free `repl/loop.py` owns the loop, parameterized by a reader and writer. `repl/console.py` and `repl/themes.py` alone import prompt_toolkit: the console maps shared lexical [highlighting](../highlighting.md) categories using live type/constructor names, retaining legacy error fallback and line clipping. It does not run scope-resolved highlighting. Completion combines session state with the scanner's raw-quote compatibility check. `repl/plain_console.py` serves pipes, `TERM=dumb`, and `--plain`; Emacs uses it under `comint-mode` with independent font-lock.

## Introspection and Rendering

Entry results carry the compiled program's `ValueDescriptors`, so `repl/render.py` renders without a global registry. Echo is decided from the checked static type (unit echoes nothing unless `echo-unit`). `:info` resolves a spelling through scope's ordinary lookup and the checker's constructor selection, so it reports exactly what an entry would see.

## Setting Persistence

`[repl]` settings (`theme`, `echo`, `echo-unit`) persist through one path: meta-commands report a change and `commands/repl.py` saves it via `config/general.py`. `--quiet` is session-only.

## Code Entry Points

- `src/agnette/agl/repl/session.py` — the incremental session; `entry_pipeline.py` — the incremental entry pipeline, reusing `PipelineDriver`'s host environment and helpers.
- `src/agnette/agl/repl/loop.py`, `console.py`, `plain_console.py` — the shared loop and front ends; `meta.py`, `render.py`, `themes.py` — meta-commands, rendering, themes.
- `src/agnette/agl/lower/repl.py` — incremental linking.
- `src/agnette/agm/commands/repl.py` — the CLI host.
