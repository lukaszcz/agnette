# Core Primitives

Two shared foundation packages serve both `agnette.agm` and `agnette.agl`. `agnette.core` holds the OS-facing building blocks; AgL's host runtime runs shell commands and agents, writes files, makes HTTP requests, and logs through them. `agnette.util` holds pure, stdlib-only helpers with no higher-layer imports.

## Process Execution

All subprocess work goes through `core/process.py`: terminal-inheriting and captured runs, require-success variants, and process-group management so an interruption tears down descendants.

- Callers may register a cleanup command for a resource they own but do not contain (`agm run`'s systemd scope); AgL hosts map terminating signals to `SystemExit` so session cleanup runs.
- A foreground variant ignores interactive signals while the child runs (editor behavior).
- Captured output is held as bytes and decoded once: strictly for AgL-facing captures (invalid UTF-8 is a typed error), leniently for display and internal plumbing.
- Captured stdout can stream through a temporary file instead of a pipe for CLIs that cannot tolerate pipe backpressure.
- The persistent Pi RPC session ([agents.md](agents.md)) is the one deliberate owner of its own long-lived process.

## Environment Handling

`core/env.py` clones and resolves environments, sources bash env files, and locates the installation prefix. Environments are passed explicitly as dictionaries, so each command controls what its subprocesses see; dotenv interpolation never mutates the process environment.

## Filesystem, TOML, and Dotenv I/O

Filesystem mutations and TOML/dotenv reads and writes share one dry-run-aware interface. TOML is parsed round-trip through a single entry, so updating one key preserves the rest of a file; floats load as exact `Decimal`s. Writes are atomic (temporary sibling, then replace) through one byte-writer shared with streamed destinations such as HTTP downloads.

## HTTP Transport

`core/http.py` is the only importer of `requests`: pooled sessions, streamed requests and atomic downloads with optional digest and size cap, transport failures classified into one typed error, and strict charset decoding. It never persists cookies or reads `~/.netrc`. Higher layers (`std/http`'s companion, the package fetcher) never see `requests` types.

## Python Environment

Package companions run in AgM's own interpreter. `core/pyenv.py` classifies PEP 508 requirements against that interpreter and installs them (`uv`, else `pip`); package installation, `agm pkg check`, and companion loading all use it.

## Dry Run and Cleanup

`--dry-run` sets an invocation mode consulted by the process and filesystem primitives, so planned and real operations share one implementation. Only project-management commands support it; AgL hosts never enable it. `core/cleanup.py` releases resources without masking an in-flight exception.

## Generic Utilities

`util/` is a dependency-free leaf usable from any layer: graph algorithms, newline and Unicode scalar-text rules, the AgL identifier grammar, `%{name}` interpolation, `ContextVar` scoping, a thread-safe recursion-limit raise, and the pinned AgL decimal context.

## Code Entry Points

- `src/agnette/core/process.py`, `env.py` — subprocess execution and cleanup registration; environment handling.
- `src/agnette/core/fs.py`, `path.py`, `toml.py`, `dotenv.py` — dry-run-aware filesystem, path, TOML, and dotenv I/O.
- `src/agnette/core/http.py` — the HTTP transport seam; `pyenv.py` — interpreter requirements.
- `src/agnette/core/dry_run.py`, `cleanup.py`, `log.py` — dry-run state, error-preserving cleanup, logging.
- `src/agnette/__init__.py` — lifts Python's int digit limit process-wide, since AgL ints are unbounded.
- `src/agnette/util/` — the pure helpers.
