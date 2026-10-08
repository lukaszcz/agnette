# AgL Host Runtime

The runtime package is the eval-free services layer: agent dispatch and session bridging, codecs, parameter conversion, host-environment types, rendering, serialization, tracing, and the Python FFI registry. It imports neither the evaluator nor the pipeline, and builds on the shared agent and core layers.

## Agents and Sessions

- `runtime/agents.py` decodes an `Agent` value into a host spec exactly once; `AgentRequest` and session values carry the decoded spec ([../../agents.md](../../agents.md)).
- `runtime/sessions.py` bridges AgL `Session` values to the shared session service and supplies a dispatcher-backed fallback host when no service is present.
- `runtime/sandbox_values.py` decodes every `sandbox` operand into a closed host mode and is the one place it becomes a permission mode plus `SandboxLimits` ([../../sandbox.md](../../sandbox.md)). A session's sandbox is fixed at open.
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

`builtin var` bindings are identified by module, scope path, and name. Root `std/config` bindings are engine-setting registers with live effects ([../hosting.md](../hosting.md)); others are ambient values a host may seed (e.g. the environment snapshot). The trace store (`runtime/trace.py`) writes best-effort JSONL for run boundaries, `print`, agent requests and responses, shell executions, escaping exceptions, and companion `runtime.trace` records ([ffi.md](ffi.md)) — never ordinary evaluation. Writes to `trace`/`trace-file` repoint it live (`runtime/host_settings.py`).

## Code Entry Points

- `src/agnette/agl/runtime/agents.py`, `sessions.py`, `request.py`, `sandbox_values.py`, `host_agents.py` — agent dispatch, sessions, requests, sandbox operands.
- `src/agnette/agl/runtime/codec.py`, `contract.py`, `convert.py`, `value_decode.py`, `engine_config.py` — codecs, contracts, conversion, host-value decoding, engine settings.
- `src/agnette/agl/runtime/render.py`, `serialize.py` — rendering and JSON serialization.
- `src/agnette/agl/runtime/trace.py`, `host_settings.py`, `types.py`, `option.py` — tracing, live settings, host types, `Option` construction.
- `src/agnette/agl/runtime/externs.py`, `boundary.py` — the FFI ([ffi.md](ffi.md)).
