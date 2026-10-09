# Agent Calls

[← Index](index.md)

An agent call sends a rendered prompt to a host-provided agent and yields a
**typed** result. Call the built-in `ask` function directly, or select it as a
method on the `Agent` value that should receive the request:

```agl
record Review
  approved: bool
  issues: array[text]

program def main(artifact: text) -> unit =
  let reviewer = AgentClaude("sonnet", "medium")
  let summary = ask "Summarize %{artifact} in one paragraph."
  let review: Review = reviewer.ask("Review this artifact:\n%{artifact}")
  let recheck = reviewer.ask::[Review]("Review %{artifact} again.", parse-error-retries = 2)
  print(summary)
  if not review.approved => print(review.issues)
```

## `ask` — the agent call function

`ask` is a built-in function with this effective call signature:

```text
@arg-named
ask[T](@arg-pos prompt: text, agent: Agent = std/config::default-agent,
       format: text = "", strict-json: bool = false,
       parse-error-retries: int = std/config::parse-error-retries,
       sandbox: AgentSandbox = std/config::default-sandbox,
       env: Environ = std/env::environ) -> T
```

`T` is the [target type](#target-types-types-as-contracts). `prompt` is
positional-only; every other parameter is optional and named-only ([Built-in
functions](functions.md#built-in-functions)). An `Agent` value also provides
the method form `Agent::ask(self, prompt, …)`, with the same parameters except
`agent`.

Free `ask` without `agent` uses the lazy [default session](#the-default-agent).
Supplying `agent`, or using the receiver form `reviewer.ask(...)`, selects an
explicit agent for that one call: the call opens a short-lived session for
itself and all of its parse retries, then closes it. Both forms accept a
contextual or explicit `::[T]` target and the named parse options.

`ask` is a built-in call name
([Keywords](lexical-structure.md#keywords)); it remains legal as a field or
method name. Referenced without a call, `ask`, `reviewer.ask`, and
`session.ask` are `text -> T` function values with default options, `T`
defaulting to `text` ([Built-in functions](functions.md#built-in-functions));
to capture other options, wrap a direct call in a lambda:

```agl
record Review
  approved: bool

program def main() -> unit =
  let reviewer = AgentCodex("o3", "high")
  let count: text -> int = ask
  let review = reviewer.ask::[Review]
  let careful: text -> Review = fn(prompt: text) => reviewer.ask(prompt, parse-error-retries = 3)
  ()
```

With no named arguments, the prompt may follow the callee without
parentheses, as a quoted template or a `$` literal ([single-argument
sugar](functions.md#calling-functions)):

```agl
record Review
  summary: text

program def main() -> unit =
  let reviewer: Agent = AgentCommand("reviewer")
  let subject = "the release notes"
  let summary: text = ask $ Summarize %{subject}.
  let review: Review = reviewer.ask::[Review] $
    Review %{subject} and provide a concise summary.
```

### `Session::ask`

```text
@arg-named
Session::ask[T](self, @arg-pos prompt: text, format: text = "",
                strict-json: bool = false,
                parse-error-retries: int = std/config::parse-error-retries) -> T
```

`session.ask(...)` sends the prompt through that live
[session](#sessions), so it has no `agent`, `sandbox`, or `env` argument. Targets, parse options, and output
contracts work as for `ask`; parse retries stay in the session's conversation.

## Agents as values

`Agent` is a built-in enum whose member records select the backend:

```agl
def review-with(agent: Agent, artifact: text) -> text =
  agent.ask("Review this artifact:\n%{artifact}")

program def main() -> unit =
  let reviewer: Agent = AgentClaude("sonnet", "medium")
  let second-opinion = AgentCodex("o3", "high")
  let scripted = AgentCommand("claude -p")
  let hosted = AgentPi("openai", "gpt-5") # thinking defaults to ""
  let configured = AgentClaude() # model and effort from [agent] at dispatch
  let candidates: array[Agent] = [reviewer, second-opinion, scripted, hosted, configured]
  let first-pass: text = review-with(reviewer, "the release notes")
  let second-pass: text = review-with(second-opinion, first-pass)
  print("%{candidates.size()} agents available")
  print(second-pass)
```

`AgentCommand(command)` runs a shell-like command string;
`AgentClaude(model, thinking)`, `AgentCodex(model, thinking)`, and
`AgentPi(provider, model, thinking)` run the respective agent CLI. Every field
except `command` defaults to `""` (`AgentClaude()`, `AgentPi(thinking =
"low")`). An empty field passes no setting to the agent unless the host
configures a default ([agent defaults](../../commands/agl.md#agent-defaults));
the value itself keeps `""`. How the host invokes each member is described
under [Session runners](../../commands/agl.md#session-runners).

`Agent` values are ordinary enum data: they can be stored, passed, rendered,
compared, and JSON-encoded. A CLI or TOML value of type `Agent` also accepts
the host's compact forms ([Host Agent
syntax](../../commands/agl.md#host-agent-syntax)). Being data, `Agent` is also
decodable: an `ask` whose target is `Agent`, or a cast of foreign JSON to
`Agent`, can produce an `AgentCommand` whose command the next call through it
spawns. Construct `Agent` values in source, or from trusted data, when that
matters.

### The default agent

Free `ask` uses `Session::default()`, which opens one session lazily on first
use and snapshots `std/config::default-agent`, `std/config::default-sandbox`,
and the ambient environment at that moment. Every later free `ask` in that run
or REPL session continues the same conversation with the same agent; a later
write to either setting does not switch it. The standard library's default is
`AgentPi()`; the host's `--default-agent` and `[exec] default-agent` seeds
override it, and a source write made before the first free `ask` takes effect:

```agl
import std/config

program def main() -> unit =
  std/config::default-agent := AgentClaude("sonnet", "medium")
  let answer: text = ask("Summarize")
```

## Sessions

Open an explicit conversation when several calls or lifecycle operations must
share it:

```agl
program def main() -> unit =
  let session = Session::open(AgentClaude("sonnet", "medium"), name = "review")
  let summary: text = session.ask("Read CHANGELOG.md and summarize it.")
  let risks: text = session.ask $ Now list the release risks.
  let branch = session.fork()
  let alternative: text = branch.ask("Propose a safer rollout.")
  session.close()
  branch.close()
  print(risks)
```

`Session::open(agent, transport = None, name = "", sandbox =
std/config::default-sandbox, env = std/env::environ)` opens a session, and
`Session::default()` returns the default session that free `ask` uses. The
session operations are `ask`, `compact(instructions = "")`, `reset()`,
`fork()`, `stats()`, `set-name(name)`, and `close()`:

- `reset` keeps the session value but starts a fresh conversation;
- `fork` returns a new session whose history begins from the parent's and
  which inherits its sandboxing and environment;
- `close` is idempotent; any later use of the closed session raises
  `SessionError`.

`ask`, `reset`, and `close` always work. Support for `compact`, `fork`,
`stats`, `set-name`, and a non-empty `name` depends on the backend
([Session runners](../../commands/agl.md#session-runners)); an unsupported
operation raises `SessionError`.

`sandbox` and `env` select the session's sandboxing mode and process
environment once, at open, with the same meaning as for `ask`; neither changes
for the session's lifetime. The session's `.sandbox` field reports the mode,
with a `Sandbox` `settings` path normalized (trailing separators and `./`
segments collapse), so it may differ textually from the path written.

`transport` is a `SessionTransport`: `Cli` (prompt-by-prompt command-line
continuation) or `Rpc` (a persistent RPC process). When omitted or `None`,
`AgentPi` uses `Rpc` and every other agent `Cli`; an explicit-agent `ask`
uses the same default. `Rpc` is supported only for `AgentPi`; selecting it
for another agent raises `SessionError` at open.

## Target types: types as contracts

Every `ask` call has a **target type**, determined from context as expected
types propagate ([Expected-type
propagation](expressions.md#expected-type-propagation)):

1. an explicit `::[T]` type argument,
2. the annotation of the enclosing `let`/`var`,
3. the declared type of the binding in a `:=`,
4. an expected type propagated from a larger expression (for example a
   function parameter type),
5. otherwise **`text`**.

```agl
record Review
  approved: bool
  issues: array[text]

def is-approved(review: Review) -> bool = review.approved

program def main() -> unit =
  let reviewer = AgentClaude()
  let summary = ask "Summarize the changes."                # text
  let review: Review = reviewer.ask("Review the changes.")   # Review
  var count: int = ask("How many files changed?")            # int
  count := ask("How many tests changed?")                    # int
  let approved = is-approved(reviewer.ask("Review again."))  # Review
  let completed: unit = ask("Apply the fixes.")              # unit
```

The target type drives the call's **output contract**:

- the codec that parses the raw output (`text` or `json`),
- a JSON Schema derived from the type,
- format instructions delivered to the agent alongside the prompt,
- validation, corrective retries on failure, and the typed value bound on
  success.

You never write "Return JSON matching …" yourself — the type is the source of
truth. A `text` target binds the raw output verbatim, with no parsing or
validation.

A `unit` target has no output contract: the call is dispatched once, its
response is ignored, and the expression evaluates to `()`. A bare `ask` in a
discarded-value position is therefore a fire-and-forget call (`ask "Notify the
reviewer."`). `format`, `strict-json`, and `parse-error-retries` are static
errors on a `unit` target.

When another part of the same enclosing expression constrains the target, AgL
first resolves that expression's types and builds the contract from the
resolved concrete target, so `select(ask("Choose a count."), 1)` with
`def select[T](first: T, second: T) -> T` asks for an `int`.

The target must be concrete — it may not be or contain a type variable of an
enclosing generic `def` ([No generic agent
targets](generics.md#no-generic-agent-targets)) — and must have a finite JSON
Schema, which every non-generic [recursive type](types.md#recursive-types) has
([The finite-schema boundary](generics.md#the-finite-schema-boundary)). The
same holds for an `exec` target.

## Named parameters

```agl
record Review
  approved: bool
  issues: array[text]

program def main() -> unit =
  let reviewer = AgentCodex("o3", "high")
  let a: Review = reviewer.ask("Review the diff.", format = "json", strict-json = true)
  let b: Review = reviewer.ask("Review the diff.", parse-error-retries = 2)
  let c: Review = reviewer.ask("Review the diff.", sandbox = Native)
  let d: Review = ask("Review the diff.", agent = reviewer, sandbox = Sandbox(memory = Some("8G")))
```

### `format`

Names the output codec with a text literal, `"text"` or `"json"`. It is
redundant: `text` targets use the `text` codec, and every other target —
`json`, records, enums, arrays, dicts, numbers, and `bool` — the `json` codec.
A non-literal value, an unknown name, or a codec that does not support the
target type is a static error; the default `""` equals omitting the option.

### `strict-json`

A `bool` that opts a JSON-codec call into [strict parsing](#strict-parsing);
`false` explicitly selects lenient parsing, overriding a host default. `true`
is a static error unless the codec is `json`; `false` has no effect on a
`text` codec. When omitted, the host default applies; the portable default is
[lenient recovery](#lenient-recovery).

### `parse-error-retries`

An `int`, evaluated once per call: the number of corrective retries after
invalid structured output ([Parse retries](#parse-retries)). When omitted, it
reads `std/config::parse-error-retries` at the call, so an earlier write to
that setting applies. A negative count raises `RangeError` before dispatch.
On a `text` target it draws a static warning, since text never fails parsing.

### `sandbox`

The call's sandboxing mode, an `AgentSandbox` value
([Types](types.md#agentsandbox)); it defaults to
`std/config::default-sandbox`:

- `Sandbox(…)` (the default) — the agent process runs wrapped by the sandbox
  runtime, which applies the resource limits the record names; `AgentClaude`
  and `AgentCodex` run in their all-permissions mode inside it.
- `Native` — the agent process runs unwrapped; `AgentClaude` and `AgentCodex`
  run in their own don't-ask/auto-approve mode.
- `Disabled` — the agent process runs unwrapped with no permission setting.

`AgentPi` and `AgentCommand` never receive a permission setting, so for them
`Native` and `Disabled` are the same and `Sandbox` keeps their own default
permissions.

### `env`

The complete process environment of the agent process, an `Environ` value.
When omitted, it is the current ambient environment — the same default `exec`
uses.

`sandbox` and `env` require an explicit agent: a free `ask` without `agent`
runs in the [default session](#the-default-agent), so either option there is
a static error. Pass `agent`, or use the
receiver form.

## The prompt

The `prompt` argument is a template rendered by the uniform interpolation
rules ([Strings and interpolation](strings-and-interpolation.md)). The
rendered prompt reaches the agent verbatim, together with the contract's
format instructions, through the call's session; the host performs no further
template or environment-variable expansion over it.

## The JSON wire format

Structured outputs travel as **JSON**:

1. The agent must return exactly one JSON value.
2. The response must not include Markdown fences, prose, or other text
   (this is what the format instructions request; lenient parsing forgives
   violations, strict parsing does not).
3. Records are JSON objects with exactly the fields, each keyed by its
   effective JSON name (declared name unless overridden by
   [`@name`/`@json-name`](attributes.md#name-and-json-name)). A field with a
   declared [default](types.md#record-types) may be omitted; it then fills
   from that default.
4. A **plain enum** — one whose every member is fieldless — is the JSON
   string of the member's effective JSON tag (declared name unless overridden
   by [`@name`/`@json-name`](attributes.md#name-and-json-name)). No other
   shape names a member: a `"$case"` object is rejected.
5. Any other enum is a JSON object with a reserved **`"$case"`** tag naming
   the member's effective JSON tag, plus that member record's fields, each
   keyed by its effective JSON name, with the same default-omission rule as a
   record field. Every member takes this shape, fieldless ones included.
   `@json-name` rejects `"$case"` as a field's JSON name, so a user field can
   never collide with it.
6. An enum's shape follows the slot's enum type, not the value. A record that
   is a member of both a plain and a non-plain enum is a string in the first
   slot and a tagged object in the second; in a record-typed slot it is a
   plain object with no `"$case"` tag.
7. Unknown fields are rejected.
8. Missing fields without a declared default are rejected.
9. A dict takes its key type's [wire form](types.md#dict-wire-forms).
10. Numbers are read exactly, as `int` or `decimal`
    ([Numbers](types.md#numbers-int-and-decimal)).

For

```agl
enum Verdict
  | Approve
  | @json-name("needs-work") Revise

enum Review
  | Pass(note: text = "ok")
  | Fail(issues: array[text])
```

the valid `Verdict` responses are `"Approve"` and `"needs-work"`, and valid
`Review` responses include:

```json
{ "$case": "Pass" }
```

```json
{ "$case": "Fail", "issues": ["missing tests", "unclear API"] }
```

### Derived JSON Schema

The schema sent to agents and used for validation is derived from the target
type:

| AgL type | Schema |
| -------- | ------ |
| `text` | `{"type": "string"}` |
| `int` | `{"type": "integer"}` |
| `decimal` | `{"type": "number"}` |
| `bool` | `{"type": "boolean"}` |
| `json` | `{}` (any JSON value) |
| `array[T]` | `{"type": "array", "items": <T>}` |
| `dict[K, V]`, `K` `text` or a text alias | `{"type": "object", "additionalProperties": <V>}` |
| `dict[K, V]`, `K` an `int`, `decimal`, `bool`, or plain enum | as above plus `"propertyNames"`: a number-text `pattern`, `{"enum": ["true", "false"]}`, or `{"enum": [<tags>]}` |
| `dict[K, V]`, any other `Hashable` `K` | `{"type": "array", "items": <a closed object with required "key" and "value">}` |
| record | object schema: `additionalProperties: false`, `required` lists every field without a declared default, per-field `properties` keyed by effective JSON name |
| plain enum | `{"enum": [<tag>, …]}` listing the members' effective JSON tags; when any constructor carries `@doc`, instead `oneOf` of per-member `{"const": <tag>}` schemas, each with its constructor's `@doc` as `description` when present |
| any other enum | `oneOf` of per-member-record schemas, each with the constructor's `@doc` as `description` when present, a `"$case"` `const` holding the member's effective JSON tag, record fields keyed by effective JSON name with the same `required` treatment as a record, and `additionalProperties: false` |

A dict key in a target must be `Hashable` and itself decodable (never a type
variable or an exception). The schema of a record or enum that is recursive,
or that occurs in more than one place, is an entry of the schema's top-level
`"$defs"` object, and every occurrence is `{"$ref": "#/$defs/<key>"}`; every
other schema is inline, as the table shows.

### Format instructions

Alongside the prompt the agent receives instructions derived from the target
type. For a JSON target they embed the derived JSON Schema itself:

````text
Return exactly one JSON value conforming to the following JSON Schema.
Do not include Markdown, prose, or code fences.

```json
<derived JSON Schema>
```
````

For the permissive `json` type (schema `{}`) only the preamble is emitted. A
`text` target has no format instructions. The instructions always describe
the **strict** shape, whatever the parsing mode.

## Parsing: lenient and strict

### Lenient recovery

Lenient mode, the portable default, recovers **exactly one** JSON value from a
possibly chatty response, then validates it strictly:

1. If the (whitespace-stripped) response is already a single valid JSON
   value, it is used as-is.
2. Otherwise, if the response contains a Markdown code fence, the fenced
   content is parsed, with trivial-malformation repair if needed.
3. Otherwise the whole response undergoes the same repair, which strips
   surrounding prose such as `Here you go: {…}`.
4. As a last resort, a single bare scalar embedded in prose is recovered —
   but only when exactly one such token is present.

Two or more top-level JSON values are ambiguous, and recovery fails. Recovery
repairs format damage but never resolves ambiguous content: an object that
repeats a member name is rejected in both modes, including a repaired one
such as `Here: {"a": 1, "a": 2,}`.

When the target is a plain enum and the recovered value is not one of its
members, the whole response is searched for its member tags. A response
naming exactly one member — any number of times, as a whole word, in the
tag's exact spelling — recovers that member, so `The verdict is Approve.` and
`{"verdict": "Approve"}` both read as `"Approve"`. Naming two or more
different members is ambiguous. Only the target itself is searched for: a
plain enum nested in a record or array takes its exact string.

### Strict parsing

With `strict-json = true` (or a host default of strict), the response must be
exactly one bare JSON value with nothing but surrounding whitespace.

## Parse retries

A call with `parse-error-retries = N` makes at most `N + 1` attempts. Attempt
1 sends the rendered prompt with its format instructions; the output is then
parsed and validated, and each failure triggers a corrective retry until the
attempts run out. Success returns the typed value; exhaustion raises
**`AgentParseError`** — with `N = 0`, directly on the first failure.

Every attempt of a call shares the call's session. When that session
continues a conversation — the default session, every explicit session, and
every explicit-agent ask except through an `AgentCommand` without
`%{SESSION_ID}` — a retry is a short follow-up containing only a
category-based validation summary and a reminder to return valid JSON. When
it cannot continue, each retry resends the complete prompt: the original
prompt, its format instructions, the failed response, the validation summary,
and the reminder. The summary never contains response-derived paths, keys, or
values.

A call that can never retry — `N = 0`, or a `text` or `unit` target — sends
exactly one prompt. Transport failures are never retried. An `exec` retry
instead reruns the command ([Retries](shell-execution.md#retries)).

## Transport and session failures

- **`AgentCallError`** — the agent could not run or answer: a spawn or sandbox
  preparation failure, nonzero exit, idle timeout, output that is not valid
  UTF-8, or a failed RPC prompt. Its message includes the captured stderr
  tail when available.
- **`SessionError`** — a session lifecycle or capability failure: opening or
  using a closed session, sandbox preparation at open, an unsupported
  operation or transport, or a failed `compact`, `fork`, `reset`, `set-name`,
  or `stats`.
- **`AgentParseError`** — output arrived but could not meet the structured
  contract.

See the [exception catalog](exceptions.md#built-in-exception-catalog) for
their fields, and [Host environment](host-environment.md#agents) for what the
host delivers per dispatch.

## `ask-request` — the request builder

`ask-request` is the side-effect-free twin of `ask`: it builds the
first-attempt `AgentRequest` that the matching `ask` would dispatch,
**without invoking an agent**:

```text
@arg-named
ask-request[T](
  @arg-pos prompt: text,
  agent: Agent = std/config::default-agent,
  format: text = "",
  strict-json: bool = false,
  parse-error-retries: int = std/config::parse-error-retries,
  sandbox: AgentSandbox = std/config::default-sandbox,
) -> AgentRequest
```

`agent` is captured in the request; `reviewer.ask-request(...)` captures its
receiver instead. The type argument and parse options select the output
contract exactly as for `ask`, but the target comes from the type argument
alone — the expected type here is the request record — and defaults to
`text`. `sandbox` is recorded unchanged and is accepted without an explicit
`agent`, since nothing is dispatched. There is no `env` parameter, and the
request carries no environment, so printing or inspecting it never leaks
secrets. The builder never dispatches, retries, or parses.

```agl
record Summary
  title: text
  bullets: array[text]

program def main() -> unit =
  let request = ask-request::[Summary]("Summarize the release notes.", agent = AgentClaude())
  print(request.json-schema)
```

The result is an [`AgentRequest`](types.md#agentrequest) with `attempt` set
to `0`, `previous-error` set to `None`, and the requested output contract
recorded for inspection; `format-instructions` and `json-schema` are `None`
for a `text` target.
