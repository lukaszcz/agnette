# AgL Pipeline and Hosting

`PipelineDriver` (`agl/pipeline.py`) is the public entry point to AgL. It drives parse → scope → typecheck → match compile → lower/link → evaluate, assembles the host environment, and hands every artifact forward so a program compiles and lowers once however often a host resumes it. `agm exec`, registered commands (`commands/exec_program.py`), and `agm check` sit on it; the REPL drives the same passes incrementally ([repl.md](repl.md)), and package validation runs only module loading and scope. There is never a second pipeline: anything a host wants compiled is compiled with the program, and artifacts produced during CLI program discovery are handed to execution rather than recomputed.

## Programs, Entries, and Parameters

- **Discovery.** `discover_programs` runs the static pipeline and reports each `program def`'s typed signature, `@doc`, and per-parameter CLI option specs, plus each program's source-reachable module closure and its static module parameters (`@param`). Scope validates attributes, so hosts build command lines from these specs alone ([cli.md](../cli.md)).
- **Binding.** `agl/runtime/arguments.py` binds a host's raw program arguments through the shared zone binder and decodes each value. `preflight_arguments` binds program arguments and module-parameter seeds against one selected, lowered program, aggregating all failures into pre-execution diagnostics; CLI hosts render argument failures as usage errors.
- **Decoding.** `decode_param_value` is the single host-value decode entry: `text` crosses verbatim; everything else is strict JSON, else one [AgL value syntax](../../agl/reference/host-environment.md#value-syntax) literal read against the slot's decode schema; plain enums also accept a bare member name. The standard `Agent` replaces this with its own parser, which adds the host shorthand ([agents.md](../agents.md)). Behavior is selected by the decode schema alone, so dispatch cannot drift from the type.
- **`@config`.** A selected program's `@config` entries are evaluated as constants during preflight and folded into the module-parameter and engine-setting precedence chains at their own rank ([config.md](../config.md)).
- **Execution.** `run_prepared` passes module-parameter seeds and engine seeds to the interpreter; a seed replaces only that binding's initializer. Timeout- and config-dependent services (agent dispatcher, session host, sandbox context, agent defaults) are built only after preflight succeeds. Hosts run under `core.process.terminating_signals_exit`, so SIGTERM/SIGHUP exit through normal cleanup.
- **Check only.** `agm check` (`check_prepared`) stops after lowering: it binds no program arguments and imports no companions.
- **Navigation.** `agm dev definition` runs load/scope and `typecheck_resolved` (`None` on failure), then `agl/navigation.py` maps a source position to declaration locations from scope side tables, refined by type-check results for fields, methods, and patterns. Name occurrences come from the semantic highlighter ([highlighting.md](../highlighting.md)).

The REPL accepts an optional parameter-seed resolver, invoked once per entry for the modules that entry first loads ([repl.md](repl.md)).

## Engine Settings and Host Seeds

Engine settings are root `builtin var` bindings of `std/config`, catalogued in `config/engine_keys.py` ([config.md](../config.md)). Hosts seed them by engine key (`builtin_host_settings`) and any host-backed binding by qualified key (`builtin_var_seeds`, which wins on overlap); a source write overrides a seed from that program point on, and the evaluator routes each write to an interpreter field or a host register per the catalog. Enum-backed settings are restamped between the reserved fallback identity and the running program's nominal identity by one helper (`runtime/engine_config.py`). Agent output echo is a host option, not an engine setting.

## Diagnostics and Recursion

All passes report through `agl/diagnostics.py`. A static warning is a `Diagnostic` carrying its catalog name (`agl/warnings.py`), rendered with a ` [NAME]` suffix; the parse labels lexer warnings with their module's source like span-based diagnostics. Only `AglError`s become diagnostics; internal compiler failures propagate. `agl/recursion.py` turns stack exhaustion on over-deep source into a pre-execution diagnostic around every frontend pass and lowering.

## Warning Policy

Static passes and caches keep every warning raw (`CheckedModuleImage.warnings` included); `agl/warning_policy.py` decides per invocation which reach the user. An invocation resolves one `WarningPolicy` (levels by name over the catalog defaults; `--warning` over `[warnings]` over the default, see [../config.md](../config.md)) in `cli_support/warning_levels.py` and gives it to every driver it creates — discovery, run, check, REPL — so they filter alike. Help and completion resolve it advisorily (defaults on an invalid flag or table) so they discover under the same policy the run will. `PipelineDriver` applies it right after type checking, to lexer, scope and checker warnings alike, in order:

1. **Dependency cap** — drops warnings anchored in *installed* files at any level: modules of store packages (incl. the managed `std`) and of the shipped standard library, the entry included, so a user `deny` cannot block an installed command. A file under no installed package or installed `std` tree (loose roots, development and editable checkouts, a development `std`, `AGM_STDLIB`) is *local*. Origin is decided where packages and the standard library are selected (`PackageInfo.installed`, `StdlibSelection.installed`) and read from the invocation's roots (`RootSet.is_installed`), never from cached module images. Each host's policy call site passes its roots, so lexer warnings of modules scope never resolved are capped too.
2. **Source regions** — drops a warning inside an `@allow` region that names it. Scope files the regions in its side table: a declaration from its attributes to its end, reaching back to its line's start only when blanks alone precede it. They match by source label, so a region never hides another module's warning.
3. **Levels** — `allow` drops, `warn` reports, `deny` turns the warning into an error that keeps its name.

Denied warnings follow the errors of the failing pass and join the failure before match compilation, lowering and evaluation, in discovery, preflight and `run_prepared` alike: `agm exec` exits 1 with nothing run, `agm check` exits 1. Every path applies the policy with the regions of the modules scope resolved (`PreparedProgram.resolutions`; scope's `resolutions` sink keeps them past a later failure); on a failure path a warning of any other module is withheld, since its `@allow` cannot be honoured, while after a complete scope a source without regions has none (`scope_complete`). Policy input is typed `(name, diagnostic)` pairs (`named_warnings`). The REPL does the same per entry (`ReplSession(warning_policy=…)`), including the lexer warnings of modules the entry loads, reports a library module's checker warnings once, on the entry that first loads it, and fails the entry before it promotes anything.

## Self-Validation

Compiler self-checks (checker closure, match artifacts, deep IR validation, FFI class shapes) are gated by one toggle in `agl/self_validation.py`: on in tests, off in production ([testing.md](../testing.md)).

## Code Entry Points

- `src/agnette/agl/pipeline.py` — `PipelineDriver`: preparation, discovery, preflight, execution.
- `src/agnette/agl/diagnostics.py`, `recursion.py`, `self_validation.py` — host-facing leaves; `warning_policy.py` — levels, `@allow` regions, denial.
- `src/agnette/agl/navigation.py` — position → declaration locations for editors.
- `src/agnette/agl/runtime/arguments.py`, `value_decode.py`, `engine_config.py` — argument binding, host-value decoding, engine settings.
- `src/agnette/agm/commands/exec_program.py`, `check.py`, `repl.py` — the CLI hosts; `src/agnette/agm/cli_support/` — discovery, parameter routes, engine seeds.
