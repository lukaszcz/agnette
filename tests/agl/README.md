# AgL end-to-end test corpus

Data files for `tests/test_agl_e2e.py`, the AgL specification suite. `scenario_harness.py`
runs a program under a scenario and asserts its expectations.

- `programs/**/*.agl` — valid programs, each declaring a `program def` (or marked
  `inline_entry`) with a sidecar `<name>.scenarios.json`. Every program runs under
  **multiple scenarios**: combinations of parameters, scripted agent responses, and
  scripted shell results, each driving a different path to its own expected outcome.
- `rejections/**/*.agl` — invalid programs the static pipeline must reject before running
  anything, each with a sidecar `<name>.expect.json`.
- `program_modules/` — library roots for `module_roots`/`stdlib_root` (`ps_support/` serves the
  `ps/` programs: file-flag synchronization and a companion failing at process exit);
  `multi_file/` and `packages/` — fixtures for the module and package tests.

## Program categories

| Directory | What it exercises |
|-----------|-------------------|
| `attributes/` | Declaration attributes and `@arg-*` zones |
| `basics/` | `let`/`var`/`:=`, parameters, agent calls, printing |
| `calls/` | `ask` parse-error retries and format options |
| `canonical/` | Multi-agent review/fix workflows |
| `casts/` | `as`/`as?` casts, `CastError`, `JsonParseError` |
| `control/` | `if`/`case`/loops/`try…catch`/`raise` |
| `errors/` | Exception types, catch field access, rethrow |
| `exec/` | Shell execution and `ExecResult` |
| `exprs/` | Arithmetic, comparisons, string operations |
| `externs/` | `extern def` Python companions and boundary crossing |
| `functions/` | Recursion, default arguments, first-class functions, lambdas |
| `generics/` | Inference, `::[…]` overrides, erasure, higher-order and imported generics |
| `inline/` | Single-expression programs and `inline_entry` sources |
| `methods/` | Record and enum methods: direct and bound calls, generic receivers |
| `modules/` | Multi-file programs (via `module_roots`) |
| `packages/` | Repository packages mounted as installed, e.g. `sysone/jev` |
| `params/` | Program and `@param` module parameters |
| `partial/` | Partial application placeholders |
| `ps/` | `std/ps` process parallelism and lifetime: values, waiting, handles, per-process state, sessions, run ends, trace attribution (isolated scenarios with `forks`) |
| `rendering/` | Console and value rendering |
| `resources/` | Package resources and filesystem paths |
| `return/` | `return` |
| `scope/` | Named scopes, lookup, and `use` |
| `sessions/` | Agent sessions and their native operations |
| `stdlib/` | Standard-library modules and builtin methods |
| `templates/`, `text/` | Interpolation, concatenation, escapes |
| `types/` | Records, enums, `json`, `array`, `dict` |
| `values/` | `std/value::parse`/`try-parse`, `ValueParseError` |

## `<name>.scenarios.json`

```json
{
  "scenarios": [
    {
      "name": "snake_case_scenario_id",
      "module_roots": ["program_modules/example"],
      "params": {"spec": "verbatim text", "rounds": 3},
      "positional": ["verbatim text", 3],
      "agents": {
        "reviewer": ["first response", "second response"],
        "impl": {
          "responses": ["fix"],
          "repeat_last": true,
          "interrupt_on_call": 2,
          "session": {
            "capabilities": ["ask", "compact", "fork", "stats"],
            "operations": {
              "compact": ["success"],
              "stats": [{"input_tokens": 8, "output_tokens": 3,
                         "cost": "0.02", "context_percent": "5"}]
            }
          }
        }
      },
      "shell": [{"command": "printf done", "stdout": "done"},
                {"command": "make test",
                 "sandbox_wrapped": {"profile": "make", "memory": "1G"}},
                {"command": "echo hi"}],
      "sandbox_home": {"extra_settings_files": ["make"],
                        "run_toml": "[run.make]\nmemory = \"1G\"\n"},
      "http": [{"expect": {"method": "GET", "url": "https://x/y"},
                "status": 200, "body": "ok"}],
      "jev": [{"expect": {"body": {"state": "text", "model": "jev-latest",
                                   "questions": {"question": {"type": "noul",
                                                              "instructions": "Urgent?"}}}},
               "json": {"model": "jev-1", "answers": {"question": {"type": "noul", "noul": 0.9}},
                        "usage": {"input_tokens": 12}}}],
      "filesystem": {"directories": ["work"]},
      "expect": {
        "stdout": "exact full stdout",
        "stdout_contains": ["fragment"],
        "stdout_not_contains": ["fragment"],
        "stderr": "exact full stderr, ${1} being the pid of the first fork",
        "stderr_contains": ["fragment"],
        "stderr_not_contains": ["fragment"],
        "trace": [{"pid": "${root}", "kind": "fork", "child_pid": "${1}"}],
        "trace_absent": [{"kind": "process_unconsumed"}],
        "calls": {"reviewer": 2},
        "prompts": [
          {"agent": "reviewer", "call": 0,
           "equals": "exact rendered prompt",
           "contains": ["fragment"], "not_contains": ["fragment"],
           "schema_contains": ["fragment"],
           "schema_paths": [
             {"path": ["$ref"], "equals": "#/$defs/Task"}
           ],
           "sandbox": "native"}
        ],
        "sessions": [
          {"agent": "impl", "tag": "session-1", "opened": true, "closed": true},
          {"agent": "impl", "tag": "session-2", "parent": "session-1"}
        ],
        "session_prompts": [
          {"agent": "impl", "session": "session-1", "call": 0, "equals": "first"},
          {"agent": "impl", "session": "session-1", "call": 1,
           "follow_up": {"contains": ["correct"]}}
        ],
        "session_operations": [
          {"agent": "impl", "session": "session-1", "operation": "compact",
           "arg": "summarize"}
        ],
        "warnings": [{"name": "noop-parse-retries", "line": 6},
                     {"name": "tab-indentation", "line": 2, "source": "<code>"}],
        "raises": {"type": "MaxIterationsExceeded",
                   "fields": {"limit": 3},
                   "message_contains": ["fragment"],
                   "line": 7},
        "exit_code": 42,
        "interrupted": "AgentCancelled",
        "host_error": {"message_contains": ["spec"]}
      }
    }
  ]
}
```

Inputs:

- `params` — the entry's named value-parameter arguments; `positional` — its positional
  ones, in order. JSON decimals load as `decimal.Decimal`.
- `module_params` — `@param` seeds keyed by declaration path (`<entry>::name` or
  `module/path::name`); they outrank every other tier, `@config` included.
- `function_params` — `@param` `def` parameter seeds keyed by `scope::function::param` in the
  entry module (`name::param` at module root; a method's scope is its receiver type).
- `process_environment` — the `std/env::environ` snapshot, replacing the real environment.
- `module_roots` — library roots relative to `tests/agl/`, run through the module graph;
  `stdlib_root` replaces the standard library root; `no_stdlib` disables the prelude.
  `packages` mounts package roots (with a `package.toml`) as development checkouts and
  `installed_packages` as store-installed ones; `stdlib_installed` makes `stdlib_root` an
  installed library. Warnings anchored in installed modules are capped.
- `inline_entry` — no `program def`: run through the same synthetic-entry transform as
  `agm exec -c`.
- `warning_levels` — static-warning levels by name (`allow`, `warn`, `deny`), simulating
  `--warning NAME=LEVEL`; unlisted warnings keep their catalog level.
- `runtime` — `PipelineDriver` overrides (`default_call_depth_limit`,
  `default_strict_json`) and the `parse_error_retries` engine seed.
- `isolated` — `true` runs the scenario in a single-threaded process of its own
  (`isolated_runner.py`: a forked child of the worker's warm zygote, which shares its imports,
  compile caches and hash seed, or else a cold subprocess) instead of the pytest process, so
  companions may `os.fork`; `stdout` is captured at fd level and includes children's output.
  The process and its children are measured by `tests/isolated_coverage.toml` when coverage is
  active.
- `forks` — scripts for forked processes (use with `isolated`): a map from fork path to a
  section. A path names the n-th fork of a process: `"1"`, `"2"` are the root's first and
  second forks, `"1.1"` the first fork of the first one. A section holds the child's own
  `agents`, `shell` and `http` scripts (same format as the scenario's) and an `expect` with
  its `calls`, `prompts`, `sessions`, `session_prompts` and `session_operations`, and `ends`:
  how the child's teardown ends its own processes, `"settle"` after it delivered a result and
  `"terminate"` after an AgL exception, `std/os::exit` or a signal. The child starts from
  these alone, not from the parent's scripts; a process without a section gets empty scripts,
  which allow no calls. As it exits, each child checks that its scripts were consumed and its
  expectations hold, and writes a report. The scenario fails unless every scripted path
  reports (a forked one is waited for up to a bounded deadline: a scripted child may be
  detached and still running, and one killed before it reports fails the scenario; a path that
  was never forked fails at once) and every report that exists passes. Unscripted forked
  processes need not report (one terminated right after the fork never does), but one that
  made a call fails when it reports. A scripted child must have ended (waited for, settled, or
  terminated) by the end of the run. Children's stdout joins the scenario's `stdout`, and
  their stderr its `stderr`.
- `filesystem` — a fixture in a temporary root: `directories`, UTF-8 `text_files`,
  `hex_files`, `directory_symlinks`; a `"$TEMP_ROOT"` parameter value becomes that root.

Scripted boundaries (every script must be fully consumed; an absent key allows no calls):

- `agents` — response queues keyed by the call site's `Agent` value (`ask` for the default
  non-command variants), consumed in order. A list is strict; the object form adds
  `repeat_last` and `interrupt_on_call` (the n-th call, 1-based, counted like `calls`,
  raises `KeyboardInterrupt`, surfacing as `AgentCancelled`). Session and ordinary asks share
  the queue. `session.capabilities` lists supported operations (`ask`, `compact`, `fork`,
  `set-name`, `stats`; default all) — omit one to script a capability rejection.
  `session.operations` maps native operations
  (`compact`, `reset`, `fork`, `stats`, `set-name`) to ordered outcomes: `success`, a
  `stats` object, or (legacy, prefer `capabilities`) `unsupported`; `reset` cannot be
  unsupported. `session.ask` scripts each session prompt as `success` or a transport
  failure (`cause`, optional `exit_code`, `stderr_tail`, `elapsed`).
- `shell` — ordered calls: the rendered `command` plus optional `stdout`, `stderr`
  (or `stdout_hex`/`stderr_hex` for non-UTF-8 bytes, taking precedence), `returncode`,
  `timed_out`, `spawn_error`. Unexpected commands fail. `sandbox_wrapped` asserts the real
  `systemd-run`/`srt` wrap by `profile` (`null` for the `default.json` fallback) or exact
  `settings_suffix` — exactly one — plus optional `memory`/`swap`; without it the call must
  be unwrapped (`["sh", "-c", <command>]`).
- `sandbox_home` — materializes a real `.agm/sandbox` tree (`run_toml`,
  `extra_settings_files`) with never-invoked `systemd-run`/`srt` shims, so sandboxed `exec`
  resolves through the real sandbox library. `unavailable: true` empties `PATH` instead;
  `proj_dir: true` adds a project directory (needed to observe `patch`). A
  `"$SANDBOX_SETTINGS_FILE"` parameter value becomes a real settings file path.
- `http` — ordered `FakeHttp` exchanges (`tests/_http_helpers.py`): optional `expect`
  (method, url, header subset with `null` for absence, body, `timeout` as
  `[connect, read]` or `null`, `verify`), then `status`, `headers` (mapping or
  `[name, value]` pairs), `body` (+ `charset`) or `body_hex`, or a failure: `fail`
  (`url`, `connection`, `timeout`, `tls`, `redirect` with optional `redirects`) before the
  response, `fail_mid_stream` (`connection`, `timeout`, `tls`, `decode`) after the body.
- `jev` — ordered TypeSafe exchanges (`tests/_jev_helpers.py`): optional `expect` (exact
  decoded `body`, `url`, header subset, `timeout`), then `status`, `json` or `content`,
  `headers`, or `fail: "connection" | "timeout"`. Mounts `packages/sysone` (excludes
  `module_roots`) with a test API key; seed `"sysone/jev::max-retries": 0` in
  `module_params` when scripting a retryable failure.

Expectations:

- `stdout` (exact), `stdout_contains`, `stdout_not_contains` — pin exact output only where
  rendering is fixed by design; use fragments for pretty JSON and boundary-marked prompts.
- `stderr` (exact), `stderr_contains`, `stderr_not_contains` — everything the process and its
  children wrote to stderr during the run (isolated scenarios only). Pids are not known in
  advance: `${root}` stands for the pid of the running process and `${<fork path>}` (`${1}`,
  `${1.1}`) for that of a forked one, in `stderr*` strings and in whole `trace` values. A
  placeholder naming a process that was never forked fails the scenario.
- `trace` — trace records as field subsets, each naming its writer's `pid`. An entry must be
  found after the previous entry of the same pid: a process's records are ordered, those of
  different processes are not. `trace_absent` lists subsets that match no record. A scenario
  with either runs with a trace file.
- `calls` — exact ordinary (non-session) call count per agent, retries included.
- `prompts` — checks on an agent's 0-based `call`: the rendered prompt (`equals`,
  `starts_with`, `contains`, `not_contains`), its output-contract JSON Schema
  (`schema_contains`, exact `schema_paths`), and `sandbox` — a permission mode or an
  object of `mode` plus `memory`/`swap`/`patch`/`settings` limits.
- `sessions` — the exact set of session tags (`session-1`, `session-2`, … in creation
  order), with optional `opened`, `closed`, `transport`, `single_prompt`, `parent`, and
  `sandbox`.
- `session_prompts` — every prompt of every created session, exactly once, by `session`
  tag and 0-based `call`, with the `prompts` text checks; `follow_up` applies them to a
  required later call on that session.
- `session_operations` — when present, every native operation on every session exactly
  once, matched by `operation`, optional `call`, `arg`, and `outcome`.
- `raises` — the uncaught exception: `type`, a recursive exact-match subset of `fields`,
  `message_contains`, optional 1-based `line` and `source_contains` (source display name).
- `warnings` — the exact list of static warnings as `{"name": …, "line": …}` objects, in
  report order; absent means the program emits none. An optional `"column"` also pins the
  1-based start column (use it on lines with several holes); an optional `"source"` pins the
  module the warning is attributed to: a path relative to `tests/agl/`, or a label such as
  `<code>` for the entry.
- `exit_code` — termination through `SystemExit` with this status.
- `interrupted` — the run ended through a host interrupt: `"KeyboardInterrupt"` or
  `"AgentCancelled"` (an interrupted agent call). Stdout, boundaries and warnings are still
  checked.
- `host_error` — a pre-execution failure (argument binding or decoding): no agent call, no
  AgL exception, diagnostics containing `message_contains`.
- `shell_sandbox_units_distinct` — every sandboxed shell call has a distinct
  `systemd-run --unit` name (retries re-prepare).

## `<name>.expect.json`

```json
{"diagnostic": {"line": 2, "message_contains": ["equality"]}}
```

The program must be rejected statically with nothing executed, with a diagnostic on `line`
(1-based, optional) containing the `message_contains` fragments (case-insensitive,
optional). `module_roots`, `packages`, `stdlib_root` and `warning_levels` work as in scenarios; the
optional `"warning"` names a denied warning the matching diagnostic must carry.

## Rejection categories

| Directory | What it tests |
|-----------|---------------|
| `lex/` | Lexical errors |
| `parse/` | Syntax errors |
| `infix/` | Operator fixity and import errors |
| `scope/` | Undefined names, duplicate declarations |
| `type/` | Type mismatches, arity errors, operator type rules, opacity |
| `match/` | Non-exhaustive and redundant matches |
| `warnings/` | Warnings denied by `warning_levels` |
