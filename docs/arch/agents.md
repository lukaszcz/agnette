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
- **Pi RPC** owns one persistent streaming JSONL child process (`agent/session/rpc.py`).

A handle reports whether later prompts continue the conversation, which decides whether the evaluator sends a short corrective follow-up or a full prompt. Each spec names its default transport. `agl/runtime/sessions.py` defines the firewall-safe host protocol; `AglSessionHost` (`agent/session/service.py`) adapts the service to it.

## Code Entry Points

- `src/agnette/agent/spec.py`, `values.py`, `spec_defaults.py` — specs, host shorthand, configured defaults.
- `src/agnette/agent/runner.py` — prompt delivery, sandboxed execution, run results.
- `src/agnette/agent/session/` — session protocol, service, CLI adapters, Pi RPC backend.
- `src/agnette/agl/runtime/agents.py`, `host_agents.py`, `sessions.py` — AgL host services that resolve agents and dispatch calls.
