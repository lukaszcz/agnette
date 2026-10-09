# Exceptions

[← Index](index.md)

Runtime failures in AgL are **exceptions** — typed values that propagate up
through expressions, blocks, and calls until caught by a `try`/`catch` or,
uncaught, terminate the program. There are no sentinel return values.

## The exception model

Every exception is a value of a concrete built-in or user-declared exception
type. Every exception type extends the abstract base type `Exception`, which
declares one named-only field present on every exception:

```text
message: text     # human-readable description
```

`Exception` itself is **not constructible** — `raise Exception(…)` is a
static error. It exists for typing: a wildcard catch binds its variable as
`Exception`, so only `message`, methods declared on `Exception` (it declares
none), and whole-value operations such as `%{e}` are available there. A field
declared on a subtype, such as `CastError`'s `raw`, requires catching or
narrowing to that subtype.

Programs declare concrete exception types:

```agl
exception DeployError extends Exception
  service: text
  exit-code: int = 1

exception RollbackError extends DeployError
```

An exception extends exactly one base exception type; omitting `extends` means
`extends Exception`. Constructor fields are the inherited fields first,
followed by the subtype's own. A field with `= <constant expr>` is optional,
inherited defaults included. An exception with no own fields omits the body.
Unlike a record or enum member, an exception is never constructible from a
bare reference, even when every field has a default (see [Fieldless and
all-defaulted constructor
references](expressions.md#fieldless-and-all-defaulted-constructor-references)):
construction always needs an explicit call, `raise Abort(...)`.

Exception fields do not accept `var` and cannot be reassigned. `builtin
exception` is the standard-library form declaring a host-recognized exception
type; its name, base, and fields must match the recognized shape exactly.

### Casts and `is`

An exception value can be widened to an ancestor in its `extends` chain
(including the root `Exception`) or narrowed to a descendant with `as`/`as?`;
widening is a no-op, and narrowing is checked against the value's runtime
type. `is`/`is not` test the same relation without converting: `x is T`
holds exactly when `x as? T` is `Some`. An unrelated type, including a
sibling, is a static error in all three forms. See
[Casts and convertibility](types.md#casts-and-convertibility) for the full
rule and matrix.

```agl
exception Problem extends Exception
  code: int

exception Detailed extends Problem
  detail: text

def describe(e: Problem) -> text =
  case e as? Detailed of
    | Some(value) => "detailed: %{value.detail}"
    | None => "problem: %{e.code}"
```

### Methods

An exception type may declare methods; they are available on every
descendant. An exception's method candidates form one level: its own methods
plus every ancestor's. Exactly one visible candidate with the wanted name
wins; two or more are a static ambiguity. A base method and a descendant
method of the same name in one module are therefore rejected at the later
declaration; across modules the clash surfaces where both are visible on a
descendant-typed receiver, and a qualified call naming either owner, a
rename, or `hiding` one route repairs it. A base-typed receiver never sees a
descendant's methods, so changing a receiver's static type (by an annotation
or an upcast) can turn a call into an error or back, but never changes which
function it selects. Visibility follows the general method rules
([Methods](functions.md#methods)).

```agl
exception Problem extends Exception
  code: int

exception DetailedProblem extends Problem
  detail: text

def Problem::label(self) -> text = "problem %{self.code}"
def DetailedProblem::detail-label(self) -> text = self.label() ++ ": %{self.detail}"

program def main() -> unit =
  let problem = DetailedProblem(message = "failed", code = 7, detail = "network")
  print(problem.label())
  print(problem.detail-label())
```

### Recursive exceptions

An exception's fields may reference its own type and take part in mutual
recursion with records, enums, and other exceptions, under the inhabitation
rule of [Recursive types](types.md#recursive-types): recursion through a
required field must be guarded by a type with a finite base value, such as
`array`/`dict` or an enum with a base-case member. An `extends` cycle is
rejected as uninhabitable for the same reason.

```agl
exception ValidationError extends Exception
  field-name: text
  causes: array[ValidationError]   # legal: guarded by array[...]
```

<!-- agl-check: error -->
```agl
exception Broken extends Exception
  child: Broken   # rejected: uninhabitable, no array/dict guard
```

### Values and rendering

Exception values support field access, equality, and rendering. In
interpolation and `print` an exception renders in **AgL record form**, fields
ordered across the whole `extends` chain, base fields first (see [Uniform
rendering rules](strings-and-interpolation.md#uniform-rendering-rules)):

```text
KeyError(message = "Dict key \"b\" is missing", key = "\"b\"")
```

`e as text` yields the same string; `e as json` yields the JSON object of the
fields of *e*'s runtime type (see [Nominal types
`as json`](types.md#nominal-types-as-json-structural-encoding)). A field value
with no JSON representation (such as a function or a cyclic value) makes the
cast raise `CastError` (`as?` yields `None`); only the report of an uncaught
exception replaces such a field with a marker.

## `try` / `catch`

`try … catch …` is an **expression** — its type is the unified type of the
body and all handler bodies (with `int → decimal` widening). Each handler is
`catch Pattern => body`, where the body is the same form an `if` or `case`
branch takes. Because `catch` marks where a `try` body ends, an inline `try`
body is a full `;` sequence, binders and `:=` assignments included; if its
final item is a `let` or `var`, the body has type `unit` (bottom if the
initializer exits). The body may end in `if`, `case`, or a loop; only a
nested `try` or a lambda is barred in final position. See [Grammar: `try` /
`catch`](grammar.md#try-catch) and [Inline bodies](grammar.md#inline-bodies).

```agl
record Review
  verdict: text
  summary: text

program def main(artifact: text) -> unit =
  try
    let review: Review = ask("Review %{artifact}", parse-error-retries = 2)
    print "verdict: %{review.verdict}"
  catch AgentParseError as e =>
    print "invalid review after %{e.attempts} attempts:\n%{e.raw}"
    raise e
  catch _ as e =>
    print "unexpected: %{e.message}"
```

Semantics:

1. The `try` body executes.
2. If it completes without an exception, every `catch` is skipped; the
   body's value is the result.
3. If an exception is raised, handlers are tested **in order**; the first
   whose pattern matches handles it.
4. The handler body runs in a fresh scope; `as name` binds the exception as
   an immutable, handler-local value typed by the caught type.
5. If no handler matches, the exception propagates outward.
6. An exception raised inside a handler body propagates normally.

A `return` inside a `try` body or a handler is not an exception and is never
caught; it unwinds to the nearest enclosing function. There is no `finally`.

Catch patterns:

- `catch SomeError` / `catch SomeError as e` — matches a raised value whose
  runtime type is `SomeError` or any descendant of it, built-in or
  user-declared; `e` is typed `SomeError`. `SomeError` names the declaration
  in scope where the clause is written. In the REPL, a clause written before
  a redeclaration of `SomeError` keeps naming (and matching descendants of)
  the original declaration; a clause written afterward names the new one.
- `catch _` / `catch _ as e` — matches anything; `e` has type `Exception`.
- `catch Exception as e` — equivalent to `catch _ as e`.

A handler that can never fire is a **static error**: one for the same type as
an earlier handler, for a type an earlier handler already covers (a
descendant of it), or after a catch-all (`_` or `Exception`). Order handlers
from most to least specific:

```agl
exception Problem extends Exception
  code: int

exception Detailed extends Problem
  detail: text

def check(code: int) -> unit =
  if code > 0 => raise Detailed(code, "disk full", message = "check failed")

program def main(code: int) -> unit =
  try
    check(code)
  catch Detailed as d =>
    print(d.detail)
  catch Problem as p =>       # Problem and any other descendant
    print(p.code)
  catch _ as e =>
    print(e.message)
```

## `raise`

`raise e` takes an `or_expr` operand, so an open form such as `if`, `case`,
`try`, a loop, another `raise`, or a lambda must be parenthesized. The operand
must be an exception value (statically checked). `raise` **diverges**: its
type is the bottom type, assignable to any expected type.

```agl
program def main(ready: bool) -> unit =
  let attempts: int = if ready => 1 else => raise Abort(message = "not ready")
  print(attempts)
```

An exception's own fields are **standard by default** — supplied positionally
or by name — and the `@arg-pos`, `@arg-std`, and `@arg-named`
[zone attributes](attributes.md#zone-attributes) constrain them as for record
fields ([Record types](types.md#record-types)). The inherited `message` is
named-only. Built-in exception types are constructed the same way; `Abort` is
the conventional type for user-initiated failures.

```agl
exception DeployError extends Exception
  service: text
  exit-code: int

program def main() -> unit =
  raise DeployError("api", 1, message = "deployment failed")
```

Exception values support [record update](expressions.md#record-update).
Field names are checked against the static exception type, but the copy keeps
the value's **runtime** type, so updating through a `catch Exception as e`
binding and re-raising keeps the original type catchable:

```agl
exception DeployError extends Exception
  service: text

def deploy(service: text) -> unit =
  raise DeployError(service, message = "health check failed")

program def main() -> unit =
  try
    deploy("api")
  catch Exception as e =>
    raise (e with message = "while deploying: %{e.message}")
```

## Built-in exception catalog

All built-in exceptions are reachable through the
[prelude](modules.md#prelude). Library functions also raise them where their
own documentation says so. Field lists below are in addition to `message`.

### `AgentCallError`

An agent **transport** failure: the agent could not run. Never retried by
`parse-error-retries` ([Agent calls](agent-calls.md)).

```text
agent: Agent      # the selected backend
cause: text       # "spawn_failure" | "nonzero_exit" | "timeout" | "interpolation_failure" | "protocol_failure" | "invalid_agent"
metadata: json    # host details: exit code, stderr tail, elapsed seconds
```

`cause = "protocol_failure"` also covers agent output that is not valid
UTF-8; the message gives the byte offset of the first invalid byte, and an
undecodable stderr tail in `metadata` is `""`.

### `AgentParseError`

Structured agent output failed parsing or validation after all attempts
allowed by `parse-error-retries`.

```text
agent: Agent            # selected backend
target-type: text       # the contract's target type, e.g. "Review"
expected-schema: json   # the derived JSON Schema
raw: text               # the last attempt's raw output
normalized-raw: text    # the recovered/normalized JSON text (or the raw output)
validation-errors: json # structured error records (category, message, path, field)
attempts: int           # total attempts made
metadata: json
```

### `ExecError`

A shell command failed to spawn, timed out, or produced output that is not
valid UTF-8 — in every form of `exec` — or exited nonzero in the **parsed or
unit form** ([Shell execution](shell-execution.md)); the structured form
represents a nonzero exit as `ExecResult` data. A `sandbox`-prepared command
that fails to prepare raises it in every form, like a spawn failure; no shell
process is started.

```text
command: text     # the rendered command
exit-code: int    # -1 for spawn failure, sandbox preparation failure, or a
                  # timeout without an exit status
stdout: text      # "" if captured but not valid UTF-8
stderr: text      # "" if captured but not valid UTF-8
timed-out: bool
```

For an undecodable stream the message names the stream and the byte offset
of the first invalid byte. A decode failure in the parsed form is never
retried by `parse-error-retries`.

### `SessionError`

A session lifecycle or capability failure: opening or obtaining the default
session, use of a closed or unknown session, an unsupported transport or
operation, or a host failure while running a session operation. Backend
capabilities are listed in [Session runners](../../commands/agl.md#session-runners). A prompt
transport failure from `Session::ask` is `AgentCallError` instead, and
output failing its contract is `AgentParseError`.

```text
operation: text   # the failed host operation, e.g. "open", "default",
                  # "ask", "compact", "reset", "fork", "stats",
                  # "set-name", or "close"
```

### `ExternError`

An `extern def` call failed: the companion Python callable raised, or
returned a value with no AgL boundary representation ([Python FFI](ffi.md)).

```text
function: text       # the extern's declared name
python-type: text    # the raising Python exception's class name; empty for
                     # a return-value contract violation
```

### `MaxIterationsExceeded`

A loop with a positive `[n]` bound exhausted it before an exit condition
triggered ([Loops](control-flow.md)).

```text
limit: int                  # the bound in effect
condition: text             # source text of the until-condition
last-condition-value: bool  # the condition's final value
metadata: json
```

### `RecursionError`

A chain of user-function activations exceeded the host's call-depth limit
(default 256; [Recursion and the call-depth
limit](functions.md#recursion-and-the-call-depth-limit)).

```text
limit: int    # the depth limit that was exceeded
```

### `MatchError`

Raised only explicitly: `case` expressions are exhaustive and never raise it
implicitly ([Pattern matching](pattern-matching.md)).

```text
scrutinee-type: text   # type name of the rejected value
scrutinee: json        # structural JSON encoding of the rejected value
```

### `ArithmeticError`

Division by zero (`int` or `decimal`), or decimal arithmetic or an
`int`-to-`decimal` conversion whose result leaves the fixed decimal range or
is otherwise invalid ([Numbers: int and decimal](types.md#numbers-int-and-decimal)).

```text
operation: text    # the operator or conversion, e.g. "/" or "as decimal"
```

### `TypeError`

An engine-setting write the host rejects — a `timeout` whose text is not a
duration, or a blank `trace-file` ([Engine
settings](host-environment.md#engine-settings)). Other type errors are static.
Carries only `message`.

### `IndexError`

Out-of-range array or text indexing, or out-of-range indexed array
assignment.

```text
index: int
length: int
```

### `KeyError`

A missing key in dictionary indexing or indexed dictionary assignment.

```text
key: text
```

`key` is the missing key rendered in AgL value syntax, exactly as it renders
inside a dict ([Rendering](strings-and-interpolation.md#uniform-rendering-rules)):
a missing `"a"` gives `"\"a\""`, `42` gives `"42"`, `Color::Red` gives
`"Color::Red"`, and `Point(x = 1, y = 2)` gives `"Point(x = 1, y = 2)"`. The
rendering parses back to an equal key with `parse::[K](e.key)` (see
[`ValueParseError`](#valueparseerror)) for any key type `K` other than `text`
that `parse` accepts; a `text` key parses back wherever value syntax reads a
quoted literal, since a top-level `parse::[text]` takes its input verbatim.
Keys that are or contain exceptions render but do not parse.

### `DuplicateKeyError`

A dict literal whose key expressions compute an equal key twice at run time
(two equal constant keys are a static error instead). `key` is rendered as for
[`KeyError`](#keyerror).

```text
key: text
```

### `UndefinedVariableError` and `ImmutableBindingError`

```text
name: text                  # both
operation: text             # ImmutableBindingError only
```

Reading an undefined name and `:=` on an immutable binding are static errors,
so a well-typed program raises these only explicitly.

### `CastError`

A fallible `as` cast found a value not conforming to the target type
([Casts and convertibility](types.md#casts-and-convertibility)), including a
failed enum-to-member downcast, an overlapping-enum cast whose member is
absent from the target, and an exception downcast whose value's runtime type
is not the target or a descendant of it. `as?` never raises: it yields `None`
where `as` would raise. A cast from `text` reads JSON, so a lone surrogate
escape in the source text fails the cast.

```text
source-type: text   # name of the source type, e.g. "json"
target-type: text   # name of the target type, e.g. "int"
raw: text           # text representation of the value that failed to convert
```

### `ValueParseError`

The prelude function `parse` (`std/value::parse`; see [Parsing
values](types.md#parsing-values)) received text that is neither strict JSON
nor an AgL [value-syntax](host-environment.md#value-syntax) literal, or that
does not conform to the target type. A lone surrogate
escape, in either syntax, is malformed; an adjacent high and low escape pair
denotes one character. `ValueParseError` is distinct from `CastError`: `catch
CastError` does not catch it.

```text
source-type: text   # always "text"
target-type: text   # name of the target type, e.g. "int"
raw: text           # the input text that failed to parse
```

### `JsonParseError`

A `std/json` parsing function received text that is not a well-formed JSON
document: malformed syntax, a duplicate object member name, a lone
`\uD800`–`\uDFFF` escape, or a number a `json` value cannot hold (`NaN`, an
infinity, or one no `decimal` can hold; see
[Numbers](types.md#numbers-int-and-decimal)).

```text
raw: text   # the input text that failed to parse
```

### `RangeError`

A range `for` whose `step` evaluates to a non-positive `int` at loop entry
([Loops](control-flow.md)), a negative `parse-error-retries` write
([Engine settings](host-environment.md#engine-settings)), or a negative
`parse-error-retries` call option, raised before dispatch
([Agent calls](agent-calls.md#parse-error-retries)). Carries only `message`.

### `Abort`

The general-purpose user abort. Carries only `message`.

### `CyclicValueError`

Rendering (`print`, `render`, interpolation, REPL echo), `as text`, or
`as json` met a reference cycle, including one closed through a `var` field;
also raised when an extern companion calls Python `repr()` on a view of a
cyclic value. Carries only `message`. See [Cycles](types.md#cycles) for which
operations tolerate a cycle instead.

## Where exceptions come from

| Source | Exception |
| ------ | --------- |
| Out-of-range array/text index access or array indexed assignment | `IndexError` |
| Missing dictionary key access or assignment | `KeyError` |
| A dict literal computes an equal key twice at run time | `DuplicateKeyError` |
| Agent transport failure, including output that is not valid UTF-8 | `AgentCallError` |
| Invalid structured agent output after all attempts | `AgentParseError` |
| Shell spawn failure, timeout, or undecodable output (any `exec` form); nonzero exit (parsed or unit form) | `ExecError` |
| Session lifecycle or capability failure | `SessionError` |
| Session prompt transport failure | `AgentCallError` |
| Extern companion raised, or its return value violated the contract | `ExternError` |
| `[n]` loop bound exhausted | `MaxIterationsExceeded` |
| Non-positive range `step`; negative `parse-error-retries` write or call option | `RangeError` |
| Call-depth limit exceeded | `RecursionError` |
| Division by zero; decimal result or `int`-to-`decimal` conversion outside the decimal range | `ArithmeticError` |
| Engine-setting write the host rejects (unparseable `timeout`, blank `trace-file`) | `TypeError` |
| Fallible `as` cast — source does not conform to the target type | `CastError` |
| `parse` — input is not JSON or AgL value syntax, or does not conform to the target type | `ValueParseError` |
| `std/json` parsing — input is not well-formed JSON | `JsonParseError` |
| Rendering, `as text`, or `as json` meets a reference cycle | `CyclicValueError` |
| `raise` of a constructed or re-raised value, including `MatchError` | any concrete type |

Library modules declare further exception types, often as a family under one
base type; a `catch` of a base type handles every descendant.

An exception that reaches the top of the program uncaught terminates the
run; the host reports the exception's type, fields, and source location, and
records it in the trace ([Results and
termination](host-environment.md#results-and-termination)).
