# Shell Execution: `exec`

[← Index](index.md)

`exec` runs a shell command and yields its output. It is a built-in
**function** called with the uniform call syntax; its result depends on the
[target type](#the-three-forms-of-exec):

```agl
record Stats
  files: int
  lines: int

program def main() -> unit =
  let dir = "src"
  let listing = exec "ls -la %!{dir}"                    # ExecResult; nonzero exit is data
  let notes: text = exec "cat %!{dir ++ "/NOTES.md"}"    # stdout; raises on nonzero exit
  let stats: Stats = exec "compute-stats --json %!{dir}" # stdout parsed as JSON
  exec "make build"                                      # unit; raises on nonzero exit
  print(listing.stdout)
```

`exec` is a built-in call name ([Keywords](lexical-structure.md#keywords));
it remains legal as a field or method name. Referenced without a call, it is
a `text -> T` function value with default options, `T` defaulting to
`ExecResult` ([Built-in functions](functions.md#built-in-functions)). A host
may statically disallow shell execution, in which case every `exec` call or
value is a static error.

## Spawn parameters

```text
@arg-named
exec[T](
  @arg-pos command: text,
  env: Environ = std/env::environ,
  cwd: Option[path] = None,
  timeout: Option[text] = std/config::timeout,
  sandbox: Option[Sandbox] = None,
  format: text = "",
  strict-json: bool = false,
  parse-error-retries: int = 0,
) -> T
```

`command` is positional-only and every option named-only ([Built-in
functions](functions.md#built-in-functions)). `T` defaults to `ExecResult`; the
parsing options `format`, `strict-json`, and `parse-error-retries` apply only to
a parsed target and are described under [Named parameters](#named-parameters).

- `env` is the complete environment of the shell; it replaces rather than
  merges with the host process environment. The default is the current
  ambient environment.
- `cwd` is the working directory the command runs in; `None` means the
  process working directory.
- `timeout` is an optional idle-timeout duration such as `Some("10m")`.
- `sandbox` is a `Sandbox` record ([Types](types.md#sandbox)) naming resource
  limits, or `None` for an unsandboxed run.

With a `Sandbox`, the command runs wrapped by the sandbox runtime, which
applies the record's limits. `memory` and `swap` are `Optional[text]`:
`Some("8G")` applies that limit, `None` means no limit, and `Default` takes
the host's configured limit ([`agm run`](../../commands/run.md)). The host
selects its configuration by the command's **first shell word** — `make` for
`"make test"`, never the `sh` that runs it; a command whose first word cannot
be determined selects the general settings. The configuration, including a
relative `settings` path, always resolves from the host's own location, never
from `cwd`, so a call site cannot supply the settings that confine it. A
sandbox preparation failure raises `ExecError` exactly like a spawn failure.

```agl
program def main() -> unit =
  let tests: text = exec("make test", sandbox = Some(Sandbox()))
  let suite: text = exec(
    "make integration-test",
    sandbox = Some(Sandbox(memory = Some("8G"))),
    timeout = Some("30m"),
  )
```

## Single-argument sugar and `$` commands

With no named arguments, the command may follow `exec` without parentheses,
as a quoted template or a [`$` literal](strings-and-interpolation.md#the-literal),
whose payload reaches the shell verbatim apart from `%{…}`, `%!{…}`, `%${…}`,
and `%!${…}` holes. Dollar forms (`$HOME`, `${name}`, `$(date)`) are expanded by
the shell at run time, using `exec`'s `env`; `%${NAME}` and `getenv` read the
program's own environment snapshot. So `%!${HOME}` and the shell's `"${HOME}"`
agree only while `env` leaves `HOME` as the program sees it:

```agl
program def main() -> unit =
  let directory = "."
  let listing: text = exec $ printf '%s\n' %!{directory}
  let home: text = exec $ printf '%s' "${HOME}"
  let scripts: text = exec $
    for file in "$HOME"/bin/*; do
      printf '%s\n' "$file"
    done
  print <| exec::[text] $ date
```

## Interpolation in shell templates

The command template renders interpolations like every other template
([Uniform rendering rules](strings-and-interpolation.md#uniform-rendering-rules));
use `%{value as json}` to pass a structured value as JSON. Interpolated values
are inserted **verbatim** by a plain hole; a [shell-quoted
hole](strings-and-interpolation.md#shell-quoted-holes) (`%!{value}`,
`%!${NAME}`) inserts the value as one quoted shell word. Text from models or
users inside a shell command is an injection hazard unless it is quoted.

### Quoting functions

`shell-quote(value)` returns `value` as one literal POSIX shell word: always
single-quoted, each `'` written as `'\''`, empty text as `''`; `%!{value}` is
shorthand for `%{shell-quote("%{value}")}`. `shell-join(values)` quotes each text of
an array and joins the words with spaces, so one array hole yields one word per
element. `shell-raw(value)` returns `value` unchanged: it marks a deliberate
unquoted splice. All three come from `std/exec` through the prelude.

```agl
program def main() -> unit =
  let name = "it's here"
  let files = ["a b.txt", "c.txt"]
  let found: text = exec "grep -l %{shell-quote(name)} %{shell-join(files)}"
  print(found)
```

### Hole warnings

Two [static warnings](warnings.md) check the holes of an `exec` command;
[`@allow`](attributes.md#allow-and-exempts) hides either within a declaration.

`unquoted-exec-hole` reports a hole that inserts runtime text verbatim, anchored
at the hole. Exempt are holes of type `int`, `decimal` or `bool`; [constant
expressions](bindings-and-scope.md#constant-expressions), such as literals,
constructors, templates of those, and references to root or scope-region `let`
constants of the same module; and direct calls of a function or method declared
[`@exempts("unquoted-exec-hole")`](attributes.md#allow-and-exempts), which includes
every `%!{…}` and `%!${NAME}` hole and `shell-quote`, `shell-join`, `shell-raw`
(`shell-raw` marks a deliberate raw splice). An environment hole `%${NAME}` is
reported. A `var`, an `@param let`, and a function-local binding are never constant
here, so a hole that refers to one is reported, directly or through other bindings.

`shell-quoted-hole` reports a call of `shell-quote` or `shell-join` (so every
`%!{…}` and `%!${NAME}` hole) placed inside quotes the command text itself opens,
anchored at the hole: in `'%!{value}'` or `"%!{value}"` the hole's own quotes cancel
or join the surrounding ones and the value is not protected. `shell-raw` and
other `@exempts` functions never warn here, since a raw splice inside quotes can be
deliberate. A plain hole inside quotes keeps the `unquoted-exec-hole` warning,
noting that the surrounding quotes do not protect its value.

Quote state is judged conservatively from the literal text: `'…'`, `"…"` and backslash
escapes, with each constant hole counted as its text and each `int`, `decimal`, `bool`
or quoting-function hole as words. Judging stops for the rest of the command at a hole of
unknown text, a command or parameter expansion (`$(`, a backtick, `${`), `$'`, a heredoc
(`<<`), a comment (`#` at the start of a word), a backslash-newline outside `'…'`, or a
`\`, `$` or `<` directly before a hole (which, if empty, could join the next text).

Only the template literal given as the command is checked: directly, through
parentheses, as the result of an `if` or `case` arm, of a block's final item, or
of a `try` body or `catch` arm. A command reached through a binding, a pipe, or an
operator is not checked.

## The three forms of `exec`

`exec`'s behavior depends on its **target type**, determined from context as
for `ask` ([Target types](agent-calls.md#target-types-types-as-contracts)),
except that the default is `ExecResult` instead of `text`.

### Structured form — target is `ExecResult`

Without an expected type (also as in `print <| exec "…"`; see [Built-in
functions](functions.md#built-in-functions)) or with the annotation
`ExecResult`, `exec` returns the
[`ExecResult`](types.md#execresult) record (`stdout`, `exit-code`, `stderr`,
`timed-out`). A **nonzero exit does not raise**; branch on `exit-code`:

```agl
program def main() -> unit =
  let res = exec "ls -la"
  print(res.stdout)
  if res.exit-code != 0 =>
    print("command failed: %{res.stderr}")
```

A spawn failure or timeout raises `ExecError` in this form; a timeout never
produces an `ExecResult`.

### Parsed form — any other non-`unit` target

`exec` parses stdout into the target type (honouring `format`,
`strict-json`, and `parse-error-retries`). A nonzero exit and unparseable
output both raise `ExecError`:

```agl
program def main() -> unit =
  let file = "README.md"
  let out: text = exec "cat %!{file}"         # stdout verbatim
  let counts: dict[text, int] = exec(         # JSON parsed
    "compute-stats --json",
    parse-error-retries = 1,
  )
  print(counts)
```

### Unit form — target is `unit`

When context requires `unit` — a non-final bare expression in a block or a
binding annotated `unit` — a nonzero exit raises `ExecError`, successful
stdout is discarded, and the call returns `()`. `format`, `strict-json`, and
`parse-error-retries` are static errors in this form:

```agl
program def main() -> unit =
  exec "make build"
  let completed: unit = exec "make lint"
```

## Execution semantics

1. The rendered command runs via the host shell (`sh -c` semantics) with the
   user's privileges, using its `env`, `cwd`, `timeout`, and `sandbox`
   arguments.
2. Standard output and standard error are captured.
3. In the parsed form, on exit status 0, trailing newlines are stripped from
   stdout — as in `$(…)` command substitution — before parsing. The
   structured form returns stdout and stderr as-is.
4. While tracing is active, every execution, each retry included, is
   recorded ([Tracing](host-environment.md#tracing)).

## Output decoding

Output is decoded as strict UTF-8, and only the streams a form and outcome
turn into an AgL value are decoded. There is no decoding parameter and no
`bytes` type; convert on the shell side (`cmd | iconv -f latin1 -t utf-8`,
`cmd | base64`).

| Form / outcome | Streams decoded | On undecodable bytes |
|-----------------|------------------|-----------------------|
| Structured, any exit | stdout and stderr | raises `ExecError` |
| Parsed, exit 0 | stdout | raises `ExecError` immediately, never a parse retry |
| Parsed or unit, nonzero exit | stdout and stderr | the nonzero-exit `ExecError` is raised as usual |
| Unit, exit 0 | none — stdout is discarded | no error |
| Timeout (any form) | stdout and stderr, truncated | the timeout `ExecError` is raised; an incomplete **trailing** UTF-8 sequence is dropped, an invalid byte elsewhere still fails |
| Spawn failure | none | unchanged |

In every case an undecodable stream's `ExecError` field is `""`, and the
message names the stream and the byte offset of the first invalid byte
([`ExecError`](exceptions.md#execerror)).

## Named parameters

`exec` accepts the same parsing options as `ask`
([Named parameters](agent-calls.md#named-parameters)), only in the parsed
form; in the structured and unit forms each is a static error, except that
passing a default (`format = ""`, `strict-json = false`) equals omitting it.

- `format` — codec name, a `"text"` or `"json"` literal matching the target.
- `strict-json` — `bool`; opts the JSON codec into strict parsing.
- `parse-error-retries` — `int`; the number of retries after a parse
  failure. It defaults to `0` and, unlike `ask`'s, never reads
  `std/config::parse-error-retries`. A negative count raises `RangeError`
  before the command is spawned.

## Retries

**Retries rerun the command.** Unlike an `ask` retry, which sends corrective
feedback ([Parse retries](agent-calls.md#parse-retries)), an `exec` retry
executes the command again with the same evaluated spawn parameters. If every
attempt fails to parse, `ExecError` is raised.

## Exceptions

`ExecError` reports a failing, timed-out, undecodable, or unparseable command
as described for each form above; catch it like any exception
([`ExecError`](exceptions.md#execerror)):

```agl
program def main() -> unit =
  try
    let counts: dict[text, int] = exec "compute-stats --json"
    print(counts)
  catch ExecError as e =>
    print "command failed (%{e.exit-code}): %{e.stderr}"
```
