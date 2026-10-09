# AgL Execution

Execution is everything after match compilation: lowering into a closed, typeless program, evaluating it, and the host runtime behind agents, shell calls, the Python FFI, codecs, and rendering. Lowering is the boundary between the frontend and the evaluator.

## Design Invariant: Typeless Downstream

Every type-dependent decision — built-in route, codec, decode schema, conversion, nominal — is resolved during lowering and baked into typeless descriptors. The evaluator interprets closed nodes and the runtime stays eval-free, keeping the dependency graph acyclic.

## What To Read Next

- [lowering.md](lowering.md) — lowering, linking, and the execution IR.
- [evaluator.md](evaluator.md) — the interpreter, values, control flow, and host-backed operations.
- [runtime.md](runtime.md) — agents and sessions, codecs, rendering, serialization, tracing, host-backed values.
- [ffi.md](ffi.md) — the Python FFI: companion loading, the value boundary, and target contracts.
