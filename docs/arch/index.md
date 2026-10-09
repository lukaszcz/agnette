# Agnette Architecture Overview

Agnette (Agentic Notation & Execution Toolkit) provides CLI agent management facilities and a complete agent workflow DSL. **AgM (Agnette Manager)** manages agent-oriented projects (workspaces, git worktrees, dependencies, sandboxes, tmux sessions). **AgL (Agnette Language)** is the statically typed workflow DSL for orchestrating agents and shell commands. The `agnette` distribution has one root package: `agnette.agm` owns the manager, `agnette.agl` the language, and both use shared services under `agnette`. The executable is `agm`; configuration uses `.agm` and `AGM_*`; AgL sources use `.agl`.

Start here for the system shape, then read only the subsystem documents relevant to the task.

## System Shape

- **CLI layer** — a Typer command tree mirrored by the directory tree; parses arguments into typed containers and dispatches.
- **Command layer** — one module per command or group; orchestrates domain logic, holds little of its own.
- **Domain layer** — project layout, packages, git, sandboxing, tmux, configuration, and agent invocation.
- **Primitive layer** — process, environment, filesystem, TOML, HTTP, and dry-run primitives, plus pure utilities.
- **AgL subsystem** — a complete language implementation (lexer → parser → AST → scope → typecheck → match compilation → lower → IR eval) and its host runtime, used by `agm exec`, `agm repl`, `agm check`, package validation, and package-registered commands.

## Architecture and Design Decisions

- **The command tree is the directory tree.** `src/agnette/agm/commands/` mirrors the CLI hierarchy one-to-one; finding a command is a path lookup.
- **Commands orchestrate; primitives do.** Manager-specific behavior lives in `agnette.agm`'s project and tmux services; shared behavior lives in `agnette`'s core, utilities, VCS, configuration, agents, packages, and sandboxing — never in commands. Configuration is the exception to strict layering: it uses `agnette.agm.project.layout` for project discovery and config paths.
- **Configuration is layered TOML.** Install, home, project, and invocation-directory scopes merge; AgL source writes and CLI flags override file layers where relevant.
- **The filesystem is the project model.** A project is a directory layout plus git worktrees and dependency checkouts, detected from disk with no separate database.
- **Agent calls share one host boundary.** AgL dispatches typed agent values through shared subprocess and session services.
- **AgL is firewalled, not isolated.** Both packages share one service layer. Import layering and the lazy `agnette.agl` façade let lower domains use AgL leaves without loading the execution stack; static passes depend on the AST, not the parser. The Python companion API is `agl`, separate from the implementation namespace.

## What To Read Next

- [cli.md](cli.md) — command tree, dispatch, argument containers, help, completion.
- [core.md](core.md) — shared primitives and pure utilities.
- [config.md](config.md) — configuration layering, AgM home, stdlib location, AgL setting precedence.
- [workspaces.md](workspaces.md) — project layout, worktrees, dependencies, sync, tmux.
- [packages.md](packages.md) — package model, validation, registered commands; [package-store.md](package-store.md) — store, installation, managed `std`.
- [sandbox.md](sandbox.md) — sandbox preparation, `agm run`, the SRT backend.
- [agents.md](agents.md) — agent specs, defaults, and sessions.
- [agl/index.md](agl/index.md) — start here for any AgL task.
- [testing.md](testing.md) — test strategy and quality gates.

## Code Entry Points

- `src/agnette/agm/cli.py`, `cli_dispatch.py`, `parser.py`, `completion.py` — CLI definition, registered-command fallback, help, completion; `src/agnette/command_catalog.py` — pure command-path leaf.
- `src/agnette/agm/commands/` — command implementations; `src/agnette/agm/cli_support/` — argument containers and shared AgL CLI support.
- `src/agnette/agm/project/`, `src/agnette/agm/tmux/` — manager-specific project layout and tmux.
- `src/agnette/core/`, `src/agnette/util/` — primitives and pure helpers.
- `src/agnette/config/`, `src/agnette/stdlib_locator.py` — configuration; shipped stdlib location.
- `src/agnette/agent/`, `src/agnette/vcs/`, `src/agnette/packages/`, `src/agnette/sandbox/` — agents, git, packages, sandboxing.
- `src/agnette/agl/` — AgL implementation and host runtime; `packages/stdlib/src/` — the AgL standard library and its Python companions; `packages/sysone/` — the `sysone/jev` package; `packages/devel/` — the development-workflow package.
- `src/agnette/version.py` — release version, kept in lockstep with package and project metadata.
- `tests/`, `docs/`, `config/` (templates, editor modes), `stubs/` (typing stubs), `tools/` (repository tooling).
