# AgL Python FFI

An `extern def` declares a function implemented in a Python *companion* beside its module with a `.py` suffix. `runtime/externs.py` owns companion loading, the registry, dispatch, interpreter-scoped state, the call window, and callable proxies; `runtime/boundary.py` owns value conversion, nominal class synthesis, and live views. The evaluator hands each extern call to the registry.

## Loading

- Companions import after static passes and before evaluation; `agm check` never imports them. A host capability gates the FFI ([../hosting.md](../hosting.md)).
- Before import, the owning package's `[python]` requirements are checked; an unsatisfied one is a diagnostic pointing to its repair ([../../package-store.md](../../package-store.md)).
- The registry reuses a companion while its path and file stamp are unchanged; bytecode is cached in the AgL artifact store ([../modules.md](../modules.md)), never beside the source.
- The Python lookup name is scope's `extern_names` fact ([../frontend/scope.md](../frontend/scope.md)).

## Companion State and Tracing

Companion globals are registry-scoped; `runtime.state` reaches a per-host-session bag activated around each call, so concurrent interpreters never share state. Values registered with `close` are closed deterministically when the bag closes (run end, or REPL reset/exit). `runtime.trace` writes to the run's trace store ([runtime.md](runtime.md)), attributed to the nearest call site outside the extern's own package. The call window also scopes host agent services (`runtime/host_agents.py`). Companions reach the network only through `core.http` ([../../core.md](../../core.md)).

## Value Boundary

- **Value-directed conversion.** Arrays, dicts, and records with `var` fields cross as live views that write through; immutable records and exceptions cross as snapshots. `json` payloads cross uncopied and unchecked.
- **Nominal classes.** One Python class is synthesized per nominal identity, carrying its descriptor, so decoding needs no call-scoped state. Classes and helpers are exposed through the temporary `agl` module during companion import; a companion's module must import every nominal its companion addresses.
- **Target contracts.** A type-directed extern's leading contract operands arrive as immutable `agl.TypeContract` objects (`runtime/type_contracts.py`) built from the IR's `target_contracts`. Extern dict keys must be `Hashable` statically.

## Callables and Exceptions

- AgL function values cross as callable proxies valid only on the owning interpreter thread during the extern call.
- AgL raises cross companion frames in the `AglException` carrier, keeping their span; other Python `Exception`s become catchable `ExternError`; `BaseException`s propagate.
- Return values are trusted: the declared signature, valid Unicode text, and finite JSON-shaped payloads are companion obligations. Only out-of-range or non-finite decimals are rejected (`BoundaryViolation`).

## Code Entry Points

- `src/agnette/agl/runtime/externs.py` — registry, loading, dispatch, state activation, callable proxies.
- `src/agnette/agl/runtime/boundary.py` — conversion, class synthesis, views, the exception carrier.
- `src/agnette/agl/runtime/type_contracts.py` — `agl.TypeContract`.
- `packages/stdlib/src/*.py` — the standard library's companions.
