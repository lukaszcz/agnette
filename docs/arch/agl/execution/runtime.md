# AgL Host Runtime

The runtime package is the eval-free services layer: agent dispatch and session bridging, codecs, parameter conversion, host-environment types, rendering, serialization, tracing, and the Python FFI registry. It imports neither the evaluator nor the pipeline, and builds on the shared agent and core layers.

## Agents and Sessions

- `runtime/agents.py` converts between `Agent` values and host specs; effects decode once per call, so host requests and the session table carry the spec while AgL `Session`/`AgentRequest` values keep the `Agent` value ([../../agents.md](../../agents.md)).
- `runtime/sessions.py` defines the session-host protocol the evaluator drives (implemented by the shared service's `AglSessionHost`) and supplies a dispatcher-backed fallback host when no service is present.
- `runtime/sandbox_values.py` decodes agent `sandbox` operands into a closed host mode (the one place it becomes a permission mode plus `SandboxLimits`) and `exec` operands into optional `SandboxLimits` ([../../sandbox.md](../../sandbox.md)). A session's sandbox is fixed at open.
- With echo or tracing on, requests carry an output callback; backends separate progress (`intermediate_output`) from final content, which follows the normal decode path.

## Codecs

`runtime/codec.py` keeps strict parsing and lenient recovery separate: agent and shell output use the configured policy, casts and value parsing are always strict (strict JSON or one [AgL value-syntax](../../../agl/reference/host-environment.md#value-syntax) literal via `runtime/value_decode.py`). Contracts consume the typeless schemas and decode walks compiled during lowering. Every JSON/TOML decoder reads numbers exactly and rejects non-finite values and duplicate object keys. Extern `AglJson` payloads are a companion obligation, not walked.

## Rendering and Serialization

- **Values carry only identity and data.** Display names, field labels, and signatures come from a `ValueDescriptors` view of the compiled program, threaded to every render site (evaluator, FFI, REPL).
- **One renderer** (`runtime/render.py`) serves interpolation, `print`, `as text`, and REPL echo. Fields are normalized to declaration order at construction, so rendering, JSON, and equality agree without type information.
- **Plan-directed JSON** (`runtime/serialize.py`) follows encode plans from `type_schema.py`: plain enums as tag strings, other enums `$case`-tagged, effective `@name`/`@json-name` keys, and one dict wire-form classifier (`dict_key_form`, `ir/contracts.py`) shared by encoding, decoding, and schema derivation.
- **Decoding** (`runtime/convert.py`) builds values from the typeless decode schema; omitted defaulted fields are filled on demand from the linked nominal descriptor.
- **Untyped walks** (uncaught-error fields, trace payloads) degrade cycles and non-data to markers; casts raise instead.

## Host-Backed Values and Tracing

`builtin var` bindings are identified by module, scope path, and name. Root `std/config` bindings are engine-setting registers with live effects ([../hosting.md](../hosting.md)); others are ambient values a host may seed (e.g. the environment snapshot). The trace store (`runtime/trace.py`) writes best-effort JSONL (each record tagged with the writer's `pid` and appended whole, so forked processes' records do not interleave) for run boundaries, `print`, agent requests and responses, shell executions, escaping exceptions, and companion `runtime.trace` records ([ffi.md](ffi.md)) — never ordinary evaluation. Writes to `trace`/`trace-file` repoint it live (`runtime/host_settings.py`).

## Process Host

`runtime/processes.py` is the eval-free `ProcessHost`: `os.fork`ed children (a single Python thread is required) that run a callable and report an opaque outcome over a private framed pipe — `Delivered` bytes, `Failed` (a Python exception), or `Ended` (exit status or signal); it knows no AgL values. A child leads its own process group, owns an empty process table, and never returns into the caller's stack. Terminating signals are held only across the short critical sections that keep host state consistent (fork, reaping, detach); blocking waits drain every attached child and wake on any signal (`core.signals`), so interrupts propagate promptly. Handles are valid only in the forking process while attached. The outcome types are `ProcessHost` class attributes, not names in `agl`.

`ForkParticipant`s (registered in order by the host's owner) extend a fork with other services' state: `prepare` in the parent (prepared states `abort`ed if the fork fails), then `parent`/`child`, and `child_exit` in reverse in the child. `runtime/fork_participants.py` holds the built-ins: stdio flush and companion state ([ffi.md](ffi.md) fork hooks). `IrInterpreter` owns a host with both and hands it to companions through `ActiveCall` as `runtime.processes`.

## Code Entry Points

- `src/agnette/agl/runtime/agents.py`, `sessions.py`, `request.py`, `sandbox_values.py` — agent dispatch, sessions, requests, sandbox operands; `arguments.py` — program-argument binding; `host_agents.py` — companion agent services.
- `src/agnette/agl/runtime/codec.py`, `convert.py`, `value_decode.py`, `engine_config.py` — output parsing, conversion, host-value decoding, engine settings.
- `src/agnette/agl/runtime/render.py`, `serialize.py` — rendering and JSON serialization.
- `src/agnette/agl/runtime/trace.py`, `host_settings.py`, `types.py`, `option.py` — tracing, live settings, host types, `Option` construction.
- `src/agnette/agl/runtime/processes.py`, `fork_participants.py` — process host and its built-in fork participants.
- `src/agnette/agl/runtime/externs.py`, `boundary.py` — the FFI ([ffi.md](ffi.md)).
