# Sandboxed Execution

Commands run inside a sandbox with an explicit filesystem/network policy and optional memory limits, giving agent-driven and untrusted commands least privilege by default. `agm run` is the CLI entry point; the preparation library is an in-process API shared by every sandboxing caller (agent runs, sessions, AgL `exec`).

## Preparation Library

- `request.py` is the leaf data layer. `SandboxLimits` is the command-independent shape an AgL `Sandbox` value decodes to; `SandboxSpec` adds the profile name; `SandboxRequest` separates the command's `cwd` from `config_cwd`, where sandbox configuration resolves.
- `prepare.py:prepare()` turns a request into a `PreparedSandboxCommand` (resource-limit prefix, optional PTY relay, backend wrapper); `close()` removes its temporary artifacts. `resolve_limits()` is the one place memory/swap limits resolve (`[run.<name>]` → `[run]` → built-in floor). The library never prints or exits; it raises typed errors.
- `SandboxContext` bundles ambient inputs (home, project, cwd, run config) for non-CLI callers; a per-call working directory never changes where configuration resolves. `SandboxRun` pairs limits with a context; the profile binds later from the real argv. A host builds one lazy context (`lazy_sandbox_context`) and shares it across the agent factory, session host, and execution services, so sandbox config loads at most once per invocation.
- Interactive agent chats use the PTY relay; captured calls do not. Sandboxed Codex runs have its host daemon disabled.

## Backends

`backend.py` defines the `SandboxBackend` protocol; `srt.py:SrtBackend` is the shipped implementation, delegating isolation to the external `srt` tool (which receives a quoted shell string). `profile.py` derives the profile name — selecting the per-command settings file and `[run.<name>]` overrides — from an executable path or a shell command's first word.

## Settings Resolution

Settings merge across the install/home/project/workspace scopes. A per-command file is selected by profile name, falling back to a default; sections merge by key, lists append, later deny lists subtract from earlier allow lists.

## `agm run`

`commands/run.py` is a thin client: it maps flags and config to a request (command aliases apply here only), calls `prepare()`, and runs the result in the foreground or prints it under `--dry-run`. Interactive commands use the PTY relay by default; sandbox and PTY can each be bypassed.

## Code Entry Points

- `src/agnette/sandbox/request.py`, `prepare.py` — data types; preparation and limit resolution.
- `src/agnette/sandbox/backend.py`, `srt.py`, `profile.py` — backend protocol, SRT backend, profile names.
- `src/agnette/sandbox/pty.py` — controlling-terminal relay.
- `src/agnette/agm/commands/run.py` — `agm run`; `src/agnette/config/sandbox/` — settings discovery and merging.
