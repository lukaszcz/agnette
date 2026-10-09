# AgL Modules

AgL programs are file-based module graphs. A module is addressed by a slash path such as `tools/format`; the loader resolves the entry module and its transitive import and re-export dependencies into a `ModuleGraph` before the whole-program passes run.

## Roots and Visibility

- **Loose roots** — a loose entry file's directory (or the working directory for inline source and the REPL), the global library in the AgM home, `[modules]` config, and CLI module paths — resolve any path beneath them; loose modules import freely.
- **Packages** are not roots: each selected package, and the standard library as `std`, mounts its `src/` tree under its name. Package modules may import only their own tree, `std`, and declared dependencies ([packages.md](../packages.md)).
- A path must resolve to exactly one `.agl` file across all roots.
- An entry file owned by a package keeps that package's module id; an unowned entry (inline, REPL, loose file) is anonymous and cannot be imported.

Import, `use`, `hiding`, re-exports, and scoped imports are resolved by scope ([frontend/scope.md](frontend/scope.md#imports-and-use)); operator fixity travels with the declaring module ([frontend/scope.md](frontend/scope.md#operators)).

## Prelude and Built-in Declarations

- Every module except `std/prelude` receives the prelude unless the host disables it; the prelude only re-exports other standard modules.
- The standard declaration of a built-in name is its `builtin` declaration in any standard-library module; a non-stdlib `builtin` declaration overrides it. Undeclared built-ins (e.g. under `--no-stdlib`) fall back to reserved identities.
- Standard modules may declare host-backed `builtin var` bindings; only root `std/config` bindings are engine settings ([hosting.md](hosting.md)).
- Modules with `extern def` declarations have Python companions ([execution/ffi.md](execution/ffi.md)).

Library naming conventions are documented for users in `docs/agl/reference/modules.md`.

## Graph Adjacency

The graph keeps full import/export adjacency (used to narrow a program's linked modules) and source-authored adjacency without loader injections (used for a `program def`'s parameter closure), plus each module's transitive dependency closure.

## Compilation Caches

Imported modules are precompiled on demand and reused in memory and across processes; entry modules are compiled afresh.

- **Stages.** Parsed syntax, declaration metadata, export surfaces, resolution, type/function interfaces, checked bodies, match sites, and lowered module IR are retained per module.
- **Validation.** `artifact_cache.py` validates each stage against the module's source and transitive dependencies.
- **Identity.** `modules/parsed_module_cache.py` assigns content-addressed node namespaces, so unchanged declarations keep identity across processes. AST-only callers (package command discovery) parse through the same cache.
- **Checked bodies** persist as their own declarations plus a journal of type-environment mutations, replayed on restore, never the whole-program environment.
- **Lowered modules** (`lower/module.py`) persist bodies and linkable tables; linking reassembles them per invocation. Companion imports, configuration values, initializer evaluation, and runtime state stay per invocation.
- **Storage.** `artifact_storage.py` owns the versioned on-disk slot under `$XDG_CACHE_HOME/agm/agl`, compiler-digest validation, and atomic writes. Any unusable entry falls back to ordinary compilation.

## Code Entry Points

- `src/agnette/agl/modules/` — module ids, roots, resolution, graph loading, the parsed-module and disk caches.
- `src/agnette/agl/artifact_cache.py`, `artifact_serialization.py`, `artifact_storage.py` — cross-compilation artifact cache and its storage.
- `src/agnette/config/module_roots.py`, `src/agnette/packages/` — configured and package-mounted roots.
- `packages/stdlib/src/` — the standard library.
