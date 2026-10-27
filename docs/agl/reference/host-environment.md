# Host Environment

[← Index](index.md)

An AgL program does not run in a vacuum: a **host** embeds the language,
supplies the agents, provides external values for a selected program's
signature and module parameters, executes shell commands, and records the
trace. This chapter specifies the contract between a program and its host —
what a program may assume, and which knobs are host-configurable. The `agm`
command line and configuration files that realize this contract are described
in the [command reference](../../commands/agl.md).

## The execution pipeline

A conforming host processes a program in this order:

1. **Parse** — syntax errors abort here.
2. **Name resolution** — scope errors abort here.
3. **Static validation** — any type error, non-exhaustive case, or redundant
   case arm aborts here. Independent advisory diagnostics are collected
   separately.
4. **Host-input validation** — externally provided values are checked against
   the selected `program def`'s own value parameters and every module and
   function parameter it reaches.
5. **Contract materialization** — every agent-call and `exec` site's output
   contract (codec, schema, format instructions) is built.
6. **Execution.**

A failure in steps 1–5 means **nothing executes**: no statement runs, no
agent is called, no shell command is spawned. [Static
warnings](warnings.md) (for example a useless `parse-error-retries` on a `text`
target) do not prevent execution, unless the host sets their level to `deny`, which
makes them static errors. A failed name resolution withholds the warnings of modules
it never reached. A host chooses warning levels and does not report warnings from
installed libraries.

## Agents

Each agent call dispatches an `Agent` value — the call's `agent` option, or
the `std/config::agent` setting — whose member selects the backend:
`AgentCommand` runs a command string, and each provider member carries its
provider-specific fields. The host contributes no agents of its own.

Per dispatch, an agent receives the rendered prompt, the output contract
(format instructions plus derived JSON Schema, so schema-capable backends
can use native structured output), the attempt number, and — on corrective
retries — a validation summary, plus the failed response when the session
cannot continue the conversation ([Parse retries](agent-calls.md#parse-retries)).
The agent process is spawned under the call's `env` argument, exactly as
`exec` spawns its command ([Shell
execution](shell-execution.md#spawn-parameters)). The agent returns raw text.
Hosts must pass the rendered prompt through verbatim, with no second template
or environment-variable expansion.

Transport failures (spawn failure, nonzero exit, timeout) surface as the
catchable `AgentCallError` with an enumerated `cause`; exit 0 with empty
output is a valid empty response ([Exceptions](exceptions.md#agentcallerror)).

## Program arguments

A `program def`'s own value parameters ([Functions](functions.md#parameters))
are its external inputs. The host supplies them as named external values at
run start, for the **selected** program only — a program reached through an
import keeps its parameters as ordinary function arguments, supplied by its
caller. Binding is ordinary call semantics: the host binds only the parameters
it has an external value for, and every other parameter uses its own default,
evaluated exactly as an omitted call argument is — at the entry call, after
every module's static bindings have initialized, so a default may read
module-level state and an exception it raises is an ordinary uncaught program
exception.

**Command-line projection.** A `program def`'s parameter list defaults to the
named-only AgL zone. On the command line, an unzoned required parameter such
as `name: text` is a positional slot, unless it carries `@opt-name` or
`@opt-short` or its type is `bool`, `Option[T]`, or `Optional[T]` (whose
negated form a positional slot cannot spell); an unzoned defaulted parameter
is named-only. An explicit `@arg-pos`, `@arg-std`, or `@arg-named` sets both
the AgL and the CLI zone. A positional-capable parameter takes a positional
token; a name-addressable one takes `--name value` or `--name=value`, and a
`bool`, `Option[T]`, or `Optional[T]` one also the negated `--no-name`. For
`Optional[T]`, the exact value `default` reads as `Default` and any other value
as `Some`. A positional token is read as the same parameter's `--name` value
is; a `bool` slot takes `true` or `false`. `@opt-short` adds a one-letter
spelling. Supplying a parameter twice, or naming no declared parameter, is a
usage error. A parameter may be named after an [engine
setting](#engine-settings): it claims the setting's bare flag and config leaf,
and the setting stays reachable through its qualified spellings. Token details
(short-flag groups, `--`): [`agm exec`](../../commands/agl.md#program-arguments).

**Validation.** Supplied values are validated after type checking and
**before any statement executes**; a default is not validated, only evaluated
later as above. A required parameter with no external value is a **host
invocation error** — reported as CLI usage, not catchable in-language. Each
parameter's value resolves through the chain in [Precedence](#precedence). An
explicitly positional-only parameter has no config key: a config entry
naming it is ignored with a warning, and a [`@config`](attributes.md#config) key naming it
is a static error; an unzoned required parameter keeps its
config key even though its CLI slot is positional.

**Reading values.** A `text` parameter takes its external value verbatim. Any
other type reads its external text as one **strict JSON value or AgL [value
syntax](#value-syntax) literal** (external values are not chatty agent
output, so no lenient recovery applies), validated against the declared type:
text that parses as strict JSON and decodes into the type is read as JSON, and
any other text as one value-syntax literal (so `{}` for a dict whose JSON form
is an entries array, see [Dict wire forms](types.md#dict-wire-forms), is an
empty dict literal). Text that is neither reports both reasons; text that
parses as JSON but neither decodes nor reads as a literal of the type reports
its JSON decode failure. A value for a plain enum — one whose every member is
fieldless — may also be a member's bare JSON name, which takes precedence over
another member's declared name of the same spelling. An `Agent`-typed value
reads a JSON object, then an `Agent` member constructor call, then compact
shorthand (`claude/opus:high`), and otherwise a verbatim command
([host Agent syntax](../../commands/agl.md#host-agent-syntax)). A config file
may give any structured host value — a program argument, module parameter,
or engine setting — natively, as a table or array in the value's [JSON
shape](agent-calls.md#the-json-wire-format)
([Configuration](../../commands/agl.md#configuration)).

For example, dict parameters with `int`, plain-enum, and record keys:

```agl
enum Level
  | Debug
  | Info

record Point
  x: int
  y: int

program def main(
  by-int: dict[int, text] = {},
  by-level: dict[Level, int] = {},
  by-point: dict[Point, text] = {}
) -> unit =
  print(by-int)
  print(by-level)
  print(by-point)
```

```text
--by-int '{1: "one", -2: "two"}'                # value syntax
--by-int '{"1": "one"}'                         # JSON object, stringified keys
--by-level '{Debug: 1, Info: 2}'                # bare names are constructors
--by-point '{Point(x = 1, y = 2): "a"}'         # value syntax
--by-point '[{"key": {"x": 1, "y": 2}, "value": "a"}]'   # JSON entries
```

With the program in `prog.agl`, its config table is `[prog.main]`:

```toml
[prog.main]
by-int = { "1" = "one" }
by-level = { Debug = 1 }
by-point = { "Point(x = 1, y = 2)" = "a" }
```

**Parameter types.** The declared type must be JSON-wire-serializable, even
for a parameter whose default is always used: `unit`, function types, and a
type reaching a dict whose key is not `Hashable` or not decodable (an
exception key) are rejected. A [recursive](types.md#recursive-types) record or
enum decodes normally, subject to the
[finite-schema boundary](generics.md#the-finite-schema-boundary). A parameter
annotated [`path`](types.md#type-aliases) — directly, as `Option[path]` or
`Optional[path]`, or through an alias — takes its value as the corresponding
`text` parameter does; a host presents it as a filesystem location (value
placeholder `PATH`, filesystem completion). A relative configured value
resolves against its defining config file (else the invocation directory when
that path does not exist; a package manifest's against its package root,
likewise), a CLI or environment value against the invocation directory. A configured
value also expands `%{VAR}` and a leading `~`, as a path-valued config field does.

**Unicode.** A CLI token, an `@opt-env` variable's value, and a config string
must be valid Unicode; a value holding a surrogate code point is a host
invocation error naming the parameter. The process environment the host
exposes through `std/env::environ` must be valid Unicode in names and values:
a host finding a variable it cannot decode fails before the program runs,
naming the variable.

### Value syntax

A value-syntax literal is a data-only subset of AgL's own expression syntax:
an integer, decimal, `true`/`false`, a quoted text literal, an `[item, ...]`
array, a `{key: value, ...}` dict whose keys are value-syntax literals read
against the declared key type, or a constructor reference or call — `Name`,
`Name()`, or `Name(arg, ..., field = value, ...)`. Nesting is unrestricted.
It is read by host inputs and by a `text`-to-structured-type cast or `parse`
([Parsing values](types.md#parsing-values)), each alongside strict JSON and
with no lenient recovery. A cast to `Agent` accepts a JSON object, a
member constructor call, or shorthand, but never falls back to a verbatim
command the way a host `Agent` input does.

- **Text.** A quoted text literal takes the [template
  escapes](strings-and-interpolation.md#escapes) and holds no interpolation: an
  unescaped hole opener (`%{`, `%!{`, `%${NAME}`, `%!${NAME}`) is an error,
  and `${` is ordinary text. Rendering writes every `%` as `\%`, so a rendered
  text reads back unchanged.
- **Constructors.** Arguments bind against the declared fields by the same
  [zone rules](functions.md#parameters) as an ordinary call, except that a
  named-only field always takes an explicit `field = value` (value syntax has
  no variables, so there is no bare-name shorthand). A name matches a record's
  or enum member's declared name or its
  [`@name`](attributes.md#name-and-json-name). An optional qualifier is the
  single name immediately enclosing the declaration: its innermost scope
  (`Geo::Point(...)`) or, for an inline enum member, its enum
  (`Shape::Square(...)`); a top-level record takes none. Where an enum type is
  expected, the enum's own name also qualifies any of its members.
- **Defaults.** An argument for a field with a declared
  [default](types.md#record-types) may be omitted; a constructor whose every
  field has a default may be spelled bare (`Name`) or as `Name()`. Strict JSON
  likewise may omit a defaulted field's key, and the derived JSON Schema
  ([Agent calls](agent-calls.md#derived-json-schema)) excludes it from
  `"required"`.
- **Dicts.** A bare name is always a constructor, so a text key is quoted
  (`{"cpu": 2}`). A key equal to an earlier one, after reading against the key
  type, is an error.
- **`json` slots.** `null` and a heterogeneous array or dict are legal only in
  a `json`-typed slot, read as plain data with text dict keys and no
  constructor calls, since a `json` value has no declared type to resolve one
  against.

```agl
record Retry
  count: int = 3

record Config
  retry: Retry = Retry
  verbose: bool = false
```

`Retry`, `Retry()`, `Config(verbose = true)`, and the bare `Config` all
construct a value with `retry` filled from its default.

A rendered record or enum value
([Uniform rendering rules](strings-and-interpolation.md#uniform-rendering-rules))
parses back through `parse`/`as` to an equal value when every field,
transitively, holds value-syntax data (no function values) and each nested
record's or enum member's declaration sits at top level or directly inside one
scope or enum — so its rendered qualifier is the single name value syntax
accepts.

### Presentation attributes

`@opt-name`, `@opt-short`, `@opt-env`, `@opt-metavar`, `@opt-hidden`, and
`@doc` shape how a value parameter, function parameter or module parameter is
addressed and described; see [Attributes](attributes.md#host-parameter-attributes).

### Help

A host is expected to describe the selected program on request. What it shows
is drawn entirely from declarations: the `program def`'s `@doc` describes the
program, each positional slot is named after its parameter, and each
name-addressable parameter is listed with its long flag, its `@opt-short`
spelling if any, its value placeholder, and its own `@doc`. Module and function
parameters follow, one section per module declaring a visible reached parameter,
titled by its module path, each parameter under its shortest resolving spelling; a
parameter with no resolving spelling is omitted.

Help rewraps each prose paragraph to the terminal width, after removing the
prose's common indentation. A paragraph holding an indented line — an example,
a list, a table — keeps its line breaks and indentation.

## Host-configurable settings

### Host parameters

A **host parameter** is a value the host may supply in place of a default.
There are four kinds:

- a value parameter of the selected `program def` — a *program argument*
  (see [Program arguments](#program-arguments));
- a [`@param`](attributes.md#function-parameters) parameter of a `def` — a
  *function parameter*: the host value replaces the default at every call that
  omits the argument, so it reaches nested calls, method calls, and partial
  applications alike, and an explicit argument always wins. A seeded mutable
  default (an array, say) is copied afresh for each call;
- a static `let` or `var` marked [`@param`](attributes.md#module-parameters) —
  a *module parameter*: the host value is bound during static initialization
  instead of evaluating the initializer;
- an [engine setting](#engine-settings).

A module or function parameter belongs to every selected program that
**reaches** it, not to every program that merely imports its module. A program
reaches a declaration when it is read, assigned or called:

- by the program's body or the default of one of its parameters;
- by the body or a parameter default of any function the program reaches, by
  name, as a function value, or as a method;
- by the initializer of any static binding the program reaches.

A function parameter is reached when its function is. Host parameters are
listed with the selected program's own module first. A parameter the program
does not reach has no host surface: it has no flag, appears in no help, and
takes no configured value.

A `@param` parameter of the selected `program def` is that program's argument,
the one parameter of that command: it is bound at the entry call, and its value
is also the default of every other call to the program that omits it. For any
other command that reaches the program, the same parameter is a function
parameter. A program argument without `@param` is bound at the entry call
only.

The parameter's **external name** is its `@opt-name`, or its declared name;
it is used in flags, config leaves, help, and completion. A root module
parameter `verbose` in `A/logging` has a bare `--verbose` spelling when it
resolves, plus dotted qualified spellings `--logging.verbose` and
`--A.logging.verbose`; one in `scope debug` adds the scope path:
`--logging.debug.trace`, `--A.logging.debug.trace`. A function parameter adds
its function's qualified path: `max-steps` of `refine` in `devel/review` is
`--review.refine.max-steps` and `--devel.review.refine.max-steps`, and of a
method `review` on `Agent`, `--review.Agent.review.max-steps` — the receiver
qualifier is part of the path. `--refine.max-steps` is no spelling. An
anonymous entry (a loose file or `-c` source) has no module prefix: its
parameters are spelled bare plus their scope path alone, so `n` of function `f`
is `--n` and `--f.n`, `by` of method `bump` on `Counter` is `--by` and
`--Counter.bump.by`, and a root module parameter `n` only `--n`. Module,
scope and function paths are dotted in flags and TOML, never `::`. `bool`,
`Option[T]`, and `Optional[T]` parameters also take `--no-...`; other types
take a value read as for [program arguments](#program-arguments).

A long spelling is claimed by the first of these **claim levels** that has a
claimant (a short spelling by the same levels, without level 3):

1. host flags;
2. the selected program's own value parameters;
3. every parameter's most qualified spelling, which no abbreviation can take, so
   each parameter stays addressable: those of named modules first, then those of
   an anonymous entry, so an entry never takes an imported module's spelling;
4. parameters of the program's own module;
5. parameters of modules with the program's **owner**;
6. parameters of modules with any other owner;
7. the [engine settings](#engine-settings).

A parameter's module is the one declaring it (the function's, for a function
parameter). A module's owner is the package mounting it, or else the loose
module root it was found through; an inline `-c` program belongs to the working
directory's root, as a file there does. A parameter in a helper module of the
program's own package thus shadows a same-named parameter of a dependency, and
parameters of modules in the loose root holding the program's file shadow those
of other roots. A shadowed parameter keeps its qualified spellings and is listed
in help under those alone. Several parameters at one level make a spelling
ambiguous only when it is used; the diagnostic names their declaration paths
and the spellings each keeps. In an anonymous entry, a root `@param n` thus
keeps `--n` while the `n` parameters of functions `f` and `g` are `--f.n` and
`--g.n`; with only `f` and `g`, `--n` is ambiguous. An imported module `tools`
with a root `@param n` keeps `--tools.n` even when the entry declares a scope or
function `tools` with its own `n`. Parameters of named modules that a program
reaches may not share a most qualified spelling (colliding routes, such as the
module `a/refine` and the function `refine` of module `a`): the program is
rejected, as is a file by `agm check` and a module loaded by the REPL.

Two config routes reach a module or function parameter:

- The **declaration route** is the parameter's own table. A module parameter's
  is its declaring module's table, by any unambiguous module suffix:
  `[A.logging]` for root bindings, `[A.logging.debug]` for `scope debug`, or
  the exact quoted `["A/logging".debug]`. A function parameter's is its
  function's qualified table, by the same module suffixes: `[review.refine]` or
  `[devel.review.refine]` for `refine`, `[review.Agent.review]` for a method.
  The leaf is the parameter's external name.
- The **program route** is the selected program's own table (see
  [Config-file schema](#config-file-schema)). It addresses a parameter through
  any resolving spelling, the dotted ones as quoted keys, so a parameter whose
  bare name a nearer declaration claims is still configurable per program.

The declaration route of a `program def` is its qualified table
(`[A.logging.inner]`). It sets a `@param` parameter (`limit` of `inner`) both
when `inner` is the selected program and when another command reaches it; in the
second case only this table is read, not the command-path table of `inner`. It
also accepts every name-addressable argument of the program without a warning.

An entry module no package owns (a loose file or `-c` source) has no
declaration route; a loose file's own parameters are configured through its
program route.

A configured key naming a parameter that a module in the program's import
closure declares, but the program does not reach, is not an undeclared key: it
draws no warning and has no effect.

Setting two spellings of one parameter in one layer is an error, as is a
spelling several parameters claim.

One leaf addresses one parameter. A table may be a parameter's declaration
route and a table of the selected program at once: `[dv.review]` is the program
route of `agm dv review` and the module route of `dv/review`. A leaf set in
such a table that would reach a different parameter through each route (a
program argument `agent` and a module parameter `agent` of `dv/review`) is an
error naming both; so is the same leaf in an inherited group table (`[dv]`).
The check applies to leaves present in tables the program reads. The argument
stays configurable through its module-suffix program table (`[main.review-main]`
for `review-main` in `dv/main`) or `@opt-name`, which also gives either
parameter a distinct key.

A table at a leaf position is a nested route, never a value, when its path is a
table of a command registered by any package (or a group of one) or a
declaration or program route table: a scope region, a function, a command. `[devel.other]` is not the
value of an `other` leaf of `[devel]`, and `[devel.review.refine]` is not the
value of a `refine` argument of `[devel.review]`. Any other table is a native
value of a table-typed parameter. A table-typed parameter named like a route
table must therefore be set as a string or in a table where its name is no route.

For a program `main` in `tool.agl` that imports `A/logging` and `devel/review`:

```toml
[A.logging]
verbose = true

[A.logging.debug]
trace = true

[devel.review.refine]
max-steps = 6                     # default of every call of refine

[tool.main]
verbose = false                   # wins over [A.logging]
"A.logging.debug.trace" = false   # wins over [A.logging.debug]
"review.refine.max-steps" = 8     # wins over [devel.review.refine]
```

`agm check` validates parameter declarations but supplies no values. The REPL
has no selected program, program route, or parameter flags: every module and
function parameter reads only its declaration route (its module's or its
function's table) when the REPL first loads its module.

### Engine settings

The standard-library module `std/config` declares the engine settings as
`builtin var` bindings, read and written through a qualified target
(`std/config::strict-json`) after `import std/config`. Each declaration's
constant initializer is its portable default, used when the host supplies no
value.

| Setting | AgL type | Portable default | Governs |
| --- | -------- | ---------------- | ------- |
| `trace` | `bool` | `false` | [tracing](#tracing) |
| `trace-file` | `Option[path]` | `None` | the trace destination |
| `strict-json` | `bool` | `false` (lenient recovery) | JSON-codec calls without a `strict-json` option |
| `agent` | `Agent` | `AgentPi()` | `ask` without an `agent` option |
| `sandbox` | `AgentSandbox` | `Sandbox` (every field defaulted) | agent calls without a `sandbox` option |
| `timeout` | `Option[text]` | `None` | `exec` without a `timeout` option; its initial value also sets the agent idle timeout |
| `parse-error-retries` | `int` | `4` | `ask` without a `parse-error-retries` option (`exec` defaults to `0`) |
| `debug` | `bool` | `false` | whether `std/fs` temporary paths are kept |

Every engine setting is also a host parameter of the selected program, claimed
at the last level: its bare flag (`--timeout 30s`) and its qualified flag
(`--std.config.timeout 30s`) set the initial value. `bool` and `Option[T]`
settings also take `--no-...` (`--no-trace`, `--no-timeout`, `--no-debug`,
`--no-strict-json`), except `trace-file`, which has no negation. `--trace`,
`--trace-file`, and `--no-trace` are mutually exclusive, as are a flag and its
negation. A program or module parameter of
the same name claims the bare spelling first, so the setting is then set
through the qualified spelling or `[exec]`.

A call option always overrides its setting, in both directions:
`strict-json = false` forces lenient parsing under a strict setting. With no
`timeout`, the agent idle timeout is host-defined.

Host text for `agent` uses the host `Agent` syntax of [Reading
values](#program-arguments); a native agent's empty provider, model, or
`thinking` field takes its default from host configuration when the agent is
used ([Agent defaults](../../commands/agl.md#agent-defaults)). Host text for
`sandbox` is strict JSON or one value-syntax literal — a member name
(`Native`, `Disabled`, `Sandbox`) or constructor call
(`Sandbox(memory = Some("8G"))`), with omitted fields filled from their
declared defaults ([host AgentSandbox
syntax](../../commands/agl.md#host-agentsandbox-syntax)).

**Positional effect.** The host applies each initial value before execution;
a host `trace-file` value turns `trace` on unless the CLI sets `trace` (a
configured `trace-file` does so even beside a configured `trace = false`),
and a `debug` initial value of `true` turns `trace` on when no host value
sets `trace` or `trace-file`. Thereafter a write to `std/config::X` governs
the statements that follow it in program order, and stays effective if a later
expression fails. Writing `trace` or `trace-file` updates the trace
destination for subsequent calls: `trace-file := Some(path)` enables tracing,
and a later `trace := false` disables it while retaining the path. Trace
output is best-effort: a filesystem failure disables tracing for the rest of
the run without rolling back the assigned values. A `timeout` write changes
only the `exec` timeout; the agent idle timeout cannot change mid-program.
Reading `timeout` returns the exact text assigned or supplied. `debug` is read
once, when the host session (the run, or the REPL session) ends.

A write the engine cannot accept raises a catchable exception and leaves the
setting unchanged: [`RangeError`](exceptions.md#rangeerror) for a negative
`parse-error-retries`, [`TypeError`](exceptions.md#typeerror) for a `timeout`
whose text is not a duration or a blank `trace-file`. The same invalid values
supplied by the host are pre-execution errors.

### Precedence

Every host parameter — program argument, function parameter, module parameter
or engine setting — takes the first value supplied, left to right:

```
CLI  >  @opt-env  >  program route  >  @inherit  >  @config  >  manifest [config]  >  declaration route  >  default
```

A program argument has no declaration route beyond its program route, and its
default is its declared default (else a missing required argument is an error).
A module parameter's declaration route is its module route and its default its
initializer; a function parameter's is its function's table and its default the
declared default. An engine setting has no `@opt-env` or `@inherit`; its
declaration route is `[exec]`.

- **Program route** — the selected program's own tables (its qualified
  table and, for a registered command, its command-path table), then inherited
  group tables; see [Config-file schema](#config-file-schema).
- **`@inherit`** — the value [`@inherit`](attributes.md#inherit) reads from
  another command's configuration: its program tables, then its package's
  manifest `[config]`. Another command's command line, environment, `@config`,
  and `@inherit` never contribute. An address naming no registered command
  draws a warning and supplies nothing.
- **`@config`** — the selected program's own [`@config`](attributes.md#config)
  entries. A program argument takes its value only when that program is the
  selected one; a function `@param` takes it as the default of every reaching
  call, like a host value.
- **Manifest `[config]`** — for a package-owned program only, its package
  manifest's [`[config]`](../../commands/pkg.md#config) table: command table,
  then inherited group table, then manifest root. It supplies program
  arguments, module and function parameters, and engine settings alike.
- **Declaration route** — the parameter's own table (a module's or a
  function's); **`[exec]`** — global engine defaults.

A program-route leaf with the bare name of an engine setting sets a same-named
parameter instead when one claims that spelling; the setting is then addressed
by the quoted key `"std.config.NAME"`.

These tiers choose an engine setting's **initial** value; a source write to
`std/config::X` overrides it from its program point onward
([positional effect](#engine-settings)). For example, `--sandbox
Native` overrides both `[prog.main] sandbox = "Sandbox"` and
`[exec] sandbox = "Sandbox"`; a later
`std/config::sandbox := AgentSandbox::Disabled` overrides the flag
from that point on.

Hosts without some tiers skip them. Inline `-c` source has no program or
declaration route and no manifest: all its parameters (program arguments,
module and function parameters) resolve as CLI > `@opt-env` > `@inherit` >
`@config` > default, while its file-backed imports keep their module routes.
The REPL has no selected
program: engine settings resolve as CLI > `[exec]` > declared default,
`@inherit` and `@config` apply nothing, and a
`program def` declared there is an ordinary function, so its named-only
parameters are supplied as `main(x = 5)`.

### Config-file schema

`[exec]` holds global engine defaults under the settings' names
(`strict-json`, `trace-file`) beside the execution options `echo` and
`max-call-depth`; any other `[exec]` key draws an undeclared-key warning and is
ignored. A program's qualified table is its module
suffix (the entry file's stem, or a package-qualified route) followed by its
declaration path: `[prog.main]` for `main` in `prog.agl`. One table supplies
both the program's engine-setting overrides and its value parameters. A longer
suffix, or an exact quoted module route such as
`["review-tools/review".review.main]`, disambiguates same-leaf modules.

```toml
[exec]
sandbox = "Native"

[prog.main]
sandbox = 'Sandbox(memory = Some("8G"))'
```

A program a package registers as a command is also addressed by that command
path and by every [alias](../../commands/pkg.md#aliases) path naming the
command: `agm dv review`, registered for `review-tools/review::main`, reads
`[dv.review]` however the program is run — as the command, by installed
reference, or by file path. Setting one leaf through two spellings of the
same program in one config layer is an error. Every proper prefix of a command
path is an inherited group table (`[dv]` for `dv review`): a deeper prefix
wins over a shallower one, and the program's exact tables win over any of
them regardless of config layer. A leaf an inherited table sets joins
ambiguity checks but is exempt from undeclared-key warnings, since a
descendant command may consume it.

AgM's own schema sections — `[agent]`, `[deps]`, `[modules]`, `[packages]`,
`[params]`, and `[warnings]` — keep their own meaning at every depth, so no route passes
through one: a module whose stem is `agent` has no `[agent.<program>]` route,
and a package command whose first word is a section name is rejected.

## Tracing

While tracing is active, a conforming host writes one JSON object per line.
Every record starts with `kind`, `ts`, `file`, `line`, `col`, and `pid`, in that
order. `ts` is an ISO-8601 local timestamp with an offset; `file`, `line`, and
`col` identify the source location when known and are `null` otherwise. Source
columns are 1-based. `pid` is the id of the process that wrote the record.
Each record is written with a single append, so concurrent processes' records
do not interleave on local filesystems. `run_start` records the invoked `command` and selected
`function` when known. It also includes `arguments` by program parameter name,
`parameters` for host-seeded `@param` bindings and `def` parameters of other functions
(keyed `module::function::param`, a scope path preceding the function name), and `config`
for other host-seeded values. Parameter and config keys use qualified names;
enum values retain their `$case` tag, and decimal values are exact text strings.
A non-empty dict whose keys are not text is written as an array of
`{"key": …, "value": …}` entries; text-keyed and empty dicts are objects. Trace
records encode values by their runtime shape, uniformly across events.
Cycles and non-data values degrade to markers. Run boundaries delimit each
execution, including entries appended to a shared file. Positional source
writes may enable or disable tracing, so records outside the active interval
(including run start or end) can be absent.

Tracing records only observable boundaries:

- run start (with the invoked command, selected `program def` function, and
  host-supplied values when known) and end (with success/failure);
- stdout emitted by `print`;
- each agent request and response. A request records the fully composed prompt,
  the selected `Agent` value as given (`agent`, variant and payload), the agent
  actually dispatched after [`[agent]` defaults](../../commands/agl.md#agent-defaults)
  (`effective_agent`, a `$case`-tagged object), the `AgentSandbox` it runs under
  (`sandbox`, the session's fixed one for a session ask), the `SessionTransport`
  member name it is driven through (`transport`), attempt information, and output
  contract; a response records its full content or transport/cancellation
  outcome;
- every `exec` invocation (command, exit code, duration, stdout, stderr, and
  timeout flag);
- an exception only when it escapes the program uncaught;
- records a companion emits through `runtime.trace` ([FFI](ffi.md#trace-hook)).

Ordinary expression evaluation is not traced.

## Results and termination

A run ends in one of three ways:

1. **Success** — all statements executed; the host can observe the final
   bindings, each scoped one under its full path spelling.
2. **Pre-execution failure** — a static error, program-argument validation error, or
   host configuration error; nothing was executed.
3. **Uncaught exception** — the program started and an exception reached the
   top. The host reports the exception's type name, its fields keyed by their
   effective JSON name, and the source location of the raise site. A field
   holding a value with a reference cycle ([Types](types.md#cycles)), or a
   value of a kind with no JSON representation, is reported as a placeholder
   marker, so reporting a failure never fails. A field whose declared type
   has no JSON form is reported from its value alone: an enum member carries
   its effective `$case` tag ([`@json-name`](attributes.md#name-and-json-name),
   else `@name`, else the declared name), nested record and exception fields
   are keyed by their effective JSON names as in the typed encoding, and a
   non-empty dict with non-`text` keys is an array of `{"key": …, "value": …}`
   objects.

### Child processes

A process the program forked through `std/ps` belongs to the run until it is detached.

- **Success.** The run waits for every such process still running. Their results are discarded; a
  failure nobody consumed is reported as a warning.
- **Any other end** — an uncaught exception, `std/os::exit`, an interrupt (Ctrl-C), or
  termination by SIGTERM or SIGHUP. The run terminates those processes (SIGTERM, then SIGKILL
  after a grace period), each unwinding and releasing what it owns, before the run's other
  cleanup. A second interrupt during termination kills them at once.

Surviving members of an attached child's process group are terminated during cleanup, even if
the child has already finished. A detached process is neither waited for nor terminated.

In the REPL, a process belongs to the session and outlives the entry that forked it. Ctrl-C
cancels the entry and terminates only the processes that entry forked; an entry that ends with
an uncaught exception does the same. Leaving the REPL (`:quit`, `:exit`, Ctrl-D,
`std/os::exit`, or a termination signal) or `:reset` terminates every attached process of every
entry.
