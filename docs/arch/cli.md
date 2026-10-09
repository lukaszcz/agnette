# CLI and Command Dispatch

AgM's entry point is a Typer application that defines the command tree, parses arguments into typed containers, and dispatches to command modules. The CLI layer is thin: it validates and packages inputs; the command module owns the work.

## Command Tree

A root app plus one sub-app per command group. `src/agnette/agm/commands/` mirrors the tree (`agm pkg check` → `commands/pkg/check.py`); the Typer wiring stays in `cli.py`. Handlers are imported at dispatch, so unrelated command domains do not add to startup.

When no built-in command matches, a lazy fallback (`cli_dispatch.py`) loads the package command index (the activation index's cached registry, re-derived from manifests under project pins or editable packages), resolves the longest registered path, and shows group help or runs the program through the same host as `agm exec` ([packages.md](packages.md)). Resolving a built-in command never loads that index or AgL; only the command overview and `agm help` read the index, without compiling programs.

## Argument Handling

- Options are parsed into one typed dataclass per command (`cli_support/args.py`) — the contract between CLI surface and implementation.
- `--dry-run` sets the mode consulted by shared primitives ([core.md](core.md)).
- A selected `program def` gets its own Click command (`cli_support/program_options.py:build_program_command`), so its signature parameters and the module parameters in its source-reachable closure become ordinary flags with conventional option syntax. Parameter types map to flags through one type-directed projection; values decode through the shared host value decoder ([agl/hosting.md](agl/hosting.md)). Value precedence is described in [config.md](config.md).
- `agm exec` takes its whole tail as one catch-all and derives FILE from it in one place (`split_exec_tail`), because a program flag's arity is unknown until FILE selects the program. Execution, help, and completion share that derivation and one memoized program discovery per invocation, whose compiled artifacts are reused for execution.
- Program flags may not collide with the invoking surface's own flags (`EXEC_RESERVED_FLAGS`, `REGISTERED_RESERVED_FLAGS`). The execution flags shared by `exec`, `repl`, and registered commands are declared once (`cli_support/execution_options.py`).

## Help and Completion

Help texts and the command overview live in `parser.py`, keyed by command path; the overview appends registered package commands. A selected program renders its own help from its `@doc` and parameter docs. Shell completion (`completion.py`) discovers dynamic values — branches, dependencies, project paths, tmux sessions, registered command paths, and program parameters — from git, the project layout, the package index, and AgL source. Completion failures degrade to no suggestions, except under self-validation ([testing.md](testing.md)). Help, completion, and execution of a program are derived from the same built command, so they never disagree.

## Code Entry Points

- `src/agnette/agm/cli.py` — Typer apps, global flags, dispatch; `cli_dispatch.py` — registered-command fallback.
- `src/agnette/agm/parser.py` — help texts; `completion.py` — dynamic completions; `src/agnette/command_catalog.py` — command-path shape and reserved names.
- `src/agnette/agm/commands/` — implementations; `commands/exec_program.py` is the shared host for file, installed-reference, and registered AgL programs; `commands/check.py` runs the static pipeline only.
- `src/agnette/agm/cli_support/` — argument containers (`args.py`), `exec` target classification and module roots (`exec_target.py`, `exec_roots.py`), program discovery and CLI projection (`program_discovery.py`, `program_options.py`), module-parameter spelling and config resolution (`param_surface.py`, `param_config.py`), engine seeds (`engine_seeds.py`), and shared run options (`execution_options.py`, `run_options.py`).
- `docs/commands/` — the user-facing command reference.
