# AgL Agent Runtime

AgL runs native agents and custom commands through a shared subprocess and session layer that owns command construction, prompt delivery, sandbox preparation, output capture, and conversation lifecycles. AgL orchestrates agents from typed programs through this layer ([agl/index.md](agl/index.md)).

## Agent Specs and the Runner

- An agent is an immutable host *spec* (`agent/spec.py`): one per native kind, each building its own argv, plus a verbatim custom command. AgL's `Agent` enum decodes into this catalog.
- `agent/spec.py` holds each native CLI's permission-mode flag table (`PermissionMode`: native, unrestricted, none); the only other argv edit is sandbox preparation's `--no-daemon` for Codex.
- The runner (`agent/runner.py`) delivers rendered prompts (placeholder, `@<path>` argument, or stdin), prepares the sandbox from a `SandboxRun` ([sandbox.md](sandbox.md)), captures output, and enforces an idle timeout. Results keep streams undecoded until their text is needed. Arguments interpolate `%{name}` holes through `util/interp.py`.
- Interactive chats are ordinary stdlib externs reusing spec-owned interactive argv builders; they inherit the terminal, with no capture or idle timeout.

## Host Agent Values

Every CLI token or TOML string typed as `Agent` (including `--default-agent`) decodes through the shared host value decoder: tagged JSON, AgL value syntax, compact shorthand (`agent/values.py`: `claude[/MODEL][:EFFORT]`, `pi[/PROVIDER[/MODEL]][:EFFORT]`, …), else a verbatim command.

## Agent Defaults

Empty native fields (Pi provider, model, effort) are filled when an agent is *used*, from the most specific `[agent.<cli>[.<provider>][.<model>]]` table (`agent/spec_defaults.py`). AgL hosts validate `[agent]` strictly at startup and pass the resolver to the interpreter as a host service.

## Dispatch and Sessions

Every AgL `ask` dispatches through a session: a one-shot call opens an ephemeral session. Production hosts back sessions with `SessionService`, so asks run through its CLI adapters or Pi RPC; `agl/runtime/agents.py:value_driven_agent_factory` is the dispatcher only for hosts without a session service. A decoded `AgentRequest` carries the call's evaluated environment.

`SessionService` (`agent/session/service.py`) gives one backend-neutral lifecycle for continuing conversations: opaque handles, backend instances, and release of every owned process at the command or interpreter boundary. Permission mode, sandbox limits, and environment are fixed when a session opens, with no per-ask override. Two backend families implement it:

- **CLI adapters** map the lifecycle to each agent CLI's create/continue/compact/fork flags, preparing a fresh sandboxed command per prompt.
- **Pi RPC** owns one persistent streaming JSONL child process (`agent/session/rpc.py`); one `poll`-driven pump on the calling thread multiplexes stdin writes with stdout/stderr reads, so the backend starts no Python thread; signals wake the pump (`core.signals`), so Ctrl-C is prompt without an idle deadline.

Sessions are owned by the process that opened them: in a forked child every inherited session is inert (its backend drops only its descriptor copies, never the shared process), the default session reopens fresh, and sweeps close only the child's own sessions.

**Portable sessions** let a fork carry chosen sessions into its child. `SessionBackend.portable()` snapshots a conversation as a `PortableSession` (`agent/session/protocol.py`): data only, holding no process or descriptor, so it crosses `os.fork` by memory inheritance and needs no release. `materialize()` opens the child's own backend, independent of its source, on first use:

- **CLI adapters** (Claude, Pi): the snapshot is the data-only native fork; `materialize()` returns it. Backends without a native fork (Codex, custom commands) raise `SessionHostError` for the `fork` operation.
- **Pi RPC**: a temporary Pi process resumes the session and clones it, and the clone's id is the snapshot; the parent's process is untouched. `materialize()` spawns a Pi process resuming that id (verified), so the child is its OS parent.

`SessionService.portable(handles)` returns a `PortableSet` (snapshots by handle, plus the default handle if listed) and rejects unknown, closed or foreign handles. Every snapshot is taken from a live backend, so a handle the caller never used is materialized first and passing a snapshot on yields an independent one. `enter_forked_child(portables)` rebinds each listed handle to the service's entry for its lazily materialized snapshot, which a child that never uses it neither opens nor closes; the default handle is the snapshot if listed, else it reopens fresh. `SessionParticipant` (`agl/runtime/fork_participants.py`) takes the snapshots in `prepare` and installs them in the child; `AgentDispatcherSessionHost` has no fork and refuses any listed session. The `agnette.agent.session` façade exports lazily, so the AgL runtime uses the protocol without loading the service.

A handle reports whether later prompts continue the conversation, which decides whether the evaluator sends a short corrective follow-up or a full prompt. Each spec names its default transport. `agl/runtime/sessions.py` defines the firewall-safe host protocol; `AglSessionHost` (`agent/session/service.py`) adapts the service to it.

## Code Entry Points

- `src/agnette/agent/spec.py`, `values.py`, `spec_defaults.py` — specs, host shorthand, configured defaults.
- `src/agnette/agent/runner.py` — prompt delivery, sandboxed execution, run results.
- `src/agnette/agent/session/` — session protocol, service, CLI adapters, Pi RPC backend.
- `src/agnette/agl/runtime/agents.py`, `host_agents.py`, `sessions.py` — AgL host services that resolve agents and dispatch calls.
