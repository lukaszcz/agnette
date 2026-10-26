# AgL Python FFI

An `extern def` declares a function implemented in a Python *companion* beside its module with a `.py` suffix. `runtime/externs.py` owns companion loading, the registry, dispatch, host-session-scoped state, the call window, and callable proxies; `runtime/boundary.py` owns value conversion, nominal class synthesis, and live views. The evaluator hands each extern call to the registry.

## Loading

- Companions import after static passes and before evaluation; `agm check` never imports them.
- Before import, the owning package's `[python]` requirements are checked; an unsatisfied one is a diagnostic pointing to its repair ([../../package-store.md](../../package-store.md)).
- The registry reuses a companion while its path and file stamp are unchanged; bytecode is cached in the AgL artifact store ([../modules.md](../modules.md)), never beside the source.
- The Python lookup name is scope's `extern_names` fact ([../frontend/scope.md](../frontend/scope.md)).

## Companion State and Tracing

Companion globals are registry-scoped; `runtime.state` reaches a per-host-session bag activated around each call, so concurrent interpreters never share state. Values registered with `close` are closed deterministically when the bag closes (except those kept under `debug`) (run end, or REPL reset/exit). State is per process: `ExternRuntimeState.prepare_fork` runs the `fork` hooks registered with `runtime.state` in the parent and returns the child's entries (closed in the parent on abort); `enter_forked_child` makes the current entries inert and installs the forked ones, so `close_all` in the child closes only its own. `std/random` is the only hooked stdlib companion; `std/ps` drives `runtime.processes` and caches decoded outcomes in `runtime.state` (unhooked, so empty in a child). `runtime.processes` is the call's `ActiveCall.processes`, the host session's `ProcessHost` ([runtime.md](runtime.md)), with its outcome classes as attributes (so they never reserve names in `agl`) and `ProcessHostError` exposed on `agl`; outside a call it raises. `runtime.trace` writes to the run's trace store ([runtime.md](runtime.md)), attributed to the nearest call site outside the extern's own package. The call window also scopes host agent services (`runtime/host_agents.py`). Standard-library companions reach the network only through `core.http` ([../../core.md](../../core.md)).

## Value Boundary

- **Value-directed conversion.** Arrays, dicts, and records with `var` fields cross as live views that write through; immutable records and exceptions cross as snapshots. `json` payloads cross uncopied and unchecked.
- **Nominal classes.** One Python class is synthesized per nominal identity, carrying its descriptor, so decoding needs no call-scoped state. Classes and helpers are exposed through the temporary `agl` module during companion import; it exposes the linked program's named nominals, so a companion can rely only on those its module (transitively) imports. An `@opaque` record needs no special path: companions mint its values through the same class, and the checker alone keeps source from constructing them.
- **Target contracts.** A type-directed extern's leading contract operands arrive as immutable `agl.TypeContract` objects (`runtime/type_contracts.py`) built from the IR's `target_contracts`. Extern dict keys must be `Hashable` statically.

## Callables and Exceptions

- AgL function values cross as callable proxies valid only on the owning interpreter thread during the extern call.
- AgL raises cross companion frames in the `AglException` carrier, keeping their span; a `SessionHostError` (a `fork` that cannot snapshot a listed session) becomes `SessionError` (`runtime.sessions.session_error_value`, shared with the evaluator); other Python `Exception`s become catchable `ExternError`; `BaseException`s propagate.
- Return values are trusted: the declared signature, valid Unicode text, and finite JSON-shaped payloads are companion obligations. A Python value with no boundary representation, or an out-of-range or non-finite decimal, is rejected as `ExternError`.

## Value Transfer

`agl.transfer` (`runtime/transfer.py`) moves any value between processes of one program image as bytes: an envelope of magic, the registry's **image token**, and a pickled `(carrier flag, root, span, node table)`. Raw values name program entities only by linker ids, so a copy references the image rather than duplicating it. The value graph is flattened iteratively into a flat table (one entry per heap node, identity-memoized, so sharing and cycles survive) and rebuilt iteratively (mutable shells, closures in capture order, then fills and dict entries); no step recurses over value depth. The table is plain builtins plus `Decimal`, and the unpickler resolves nothing else. `TransferError` covers a foreign or missing header, a payload that fails to unpickle or has trailing data, a disallowed class, an unrepresentable value, and `loads` outside an extern call; once the header is valid the table is trusted like any companion return value, so the rebuild does no structural validation. The token is random per `ExternRegistry` (one per host session; a REPL `:reset` mints a new one), is shared by forked children, and is checked before unpickling. Boundary conversion on either end follows the evaluator's recursion budget like every other value walk; `loads` re-encodes through the boundary, so it follows the function-crossing call window.

## Code Entry Points

- `src/agnette/agl/runtime/externs.py` — registry, loading, dispatch, state activation, callable proxies.
- `src/agnette/agl/runtime/boundary.py` — conversion, class synthesis, views, the exception carrier.
- `src/agnette/agl/runtime/transfer.py` — `agl.transfer`, `agl.TransferError`.
- `src/agnette/agl/runtime/type_contracts.py` — `agl.TypeContract`.
- `packages/stdlib/src/*.py` — the standard library's companions.
