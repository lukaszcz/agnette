# Attributes

[← Index](index.md)

An attribute is a `@name` or `@name(args)` prefix on a defining declaration.
It annotates the declaration for the compiler or the host; nothing in the
program can read it.

## Placement

An attribute may prefix a `record`, `enum`, enum member, `exception`, `type`
alias, any `def` form (`program`, `builtin`, and `extern` included), a
`builtin var`, a field, a parameter, and a `let`/`var` binding. `import`,
`use`, `export`, `infix`, and scope regions take none. Several attributes may
be written in a row; their layout is given in
[Lexical structure](lexical-structure.md#attributes), their syntax in
[Grammar](grammar.md#attributes).

Every built-in attribute but `@config`, `@allow`, and `@exempts` takes one
constant text expression positionally, or nothing at all; see [Constant
arguments](#constant-arguments) below. `@allow` and `@exempts` take one or more
such expressions, `@allow("a", "b")`. `@config` instead takes one or more
`key = value` entries; see [`@config`](#config). An unknown name, a misplaced,
repeated, or conflicting attribute, and an argument list the attribute does not
admit are static errors.

## Constant arguments

An attribute's text argument is a literal, a template whose holes name
constants, or a reference to a constant — anything the compiler can reduce to
text before the program runs:

```agl
let aspects = "correctness, efficiency"
let rounds = 3

@doc("Review %{aspects} over %{rounds} rounds.")
@command(default-command)
program def review(scope: text) -> unit = print "%{scope}: %{aspects}"

let default-command = "devel review"
```

A name in such an argument, in a hole or on its own, is a constant of the
**declaring module**: one reached from the attribute's own scope region
outward to the module root, or under an explicit scope path. A name reached
only through another module is a static error, as is one that no constant of
this module declares. Order does not matter — a constant denotes its value, so
an argument may name one declared further down the file — while a constant
defined in terms of itself is a static error.

A hole renders a `text`, `int`, `decimal`, or `bool` constant exactly as
interpolation renders it at runtime. An array, dict, constructor, `unit`, or
`null` constant has no compile-time text and cannot fill a hole, though it
is an ordinary constant elsewhere. The whole argument must be text: a
number folds inside a hole, not as the argument itself.

Folding happens first, so an attribute that narrows its text to a particular
spelling — `@name`, `@json-name`, `@command`, `@extern-name`, the `@opt-*`
attributes, and the names of `@allow` and `@exempts` — checks the folded result.

A binding a host can override — a [module parameter](#module-parameters) or an
[engine setting](host-environment.md#engine-settings) — may be named too, and
folds to the value its declaration writes. Attribute text is fixed when the
program is compiled, so it states the declared default, while a reference to
that same binding in an expression reads whatever value the host bound.

## Catalog

| Attribute | Argument | Allowed on | Effect |
| --------- | -------- | ---------- | ------ |
| `@doc("…")` | prose | every defining declaration | Describes the declaration; hosts show it in help, and enum-member prose annotates derived JSON Schema. |
| `@arg-pos` | none | `def` parameter, field, or their declaring `def`, `record`, `exception`, or enum member | Positional-only zone. |
| `@arg-std` | none | same | Standard zone (positional or named). |
| `@arg-named` | none | same | Named-only zone. |
| `@extern-name("…")` | Python identifier | `extern def` | Names the companion function. |
| `@param` | none | module-root or scope-region `let`/`var` binding | Exposes the binding as a host parameter. |
| `@opt-name("…")` | flag word | `program def` parameter or `@param` binding | External spelling: flag, `--no-` negation, config key, completion. |
| `@opt-short("c")` | one ASCII letter | `program def` parameter or `@param` binding | One-letter flag `-c`. |
| `@opt-env("VAR")` | variable name | `program def` parameter or `@param` binding | Environment fallback when no CLI token supplies the value. |
| `@opt-metavar("…")` | placeholder | `program def` parameter or `@param` binding | Value placeholder in usage and help. |
| `@opt-hidden` | none | `program def` parameter or `@param` binding | Omits the flag from help and completion; the parameter still binds. |
| `@name("…")` | AgL identifier | field, enum member, or record declaration | Alternative external name; default JSON name. |
| `@json-name("…")` | non-empty text, not `"$case"` | same | Overrides the JSON name only. |
| `@command("…")` | command path | `program def` | Registers the program as that package command. |
| `@allow("name", ...)` | one or more warning names | every defining declaration | Hides the named static warnings within the declaration. |
| `@exempts("name", ...)` | one or more exemptable warning names | any `def` form | Exempts the function's call results from the named static warnings. |
| `@config(key = value, ...)` | one or more keyed entries | `program def` | States the values module parameters and engine settings take when this program is selected. |

## Zone attributes

`@arg-pos` (positional-only), `@arg-std` (positional or named), and
`@arg-named` (named-only) are mutually exclusive. In front of a `def`
parameter or field they zone that entry; in front of the declaration they set
the zone of every entry that names none. A lambda parameter takes no zone
attribute. Zones, their default per declaration form, and their ordering rule:
[parameters](functions.md#parameters) and
[record fields](types.md#field-zones); zones in patterns:
[Pattern matching](pattern-matching.md).

## `@doc`

One [constant text expression](#constant-arguments) of prose. It never changes
a declaration's meaning. A host shows a `program def`'s `@doc` as the
program's description — wherever the program runs, including as the prose of
the command it registers — and each host-facing parameter's `@doc` as that
parameter's help ([Host environment](host-environment.md#help)). Where a host
has room for one line only, such as a listing of commands, it shows the
opening paragraph.
An inline enum member's `@doc`, or a referenced member record's own `@doc`, is
the `description` of that member's `oneOf` alternative in a
[derived JSON Schema](agent-calls.md#derived-json-schema). A record's, enum's,
member's, or field's `@doc` also reaches a type-directed extern's companion
through its [target contract](ffi.md#target-type-parameters).

## `@extern-name`

Names the Python companion function of an `extern def` when it differs from
the declared AgL name ([Python FFI](ffi.md#declarations-and-companions)).

## `@name` and `@json-name`

Placement: a field of a record, exception, or enum member; an inline enum
member; or a record declaration (a record's own `@name`/`@json-name` doubles
as its JSON tag wherever it is an enum member). Not legal on an enum or
exception declaration, a parameter, a binding, a function, or a `type` alias.

`@name("…")` takes an AgL identifier that is not a hard keyword; a soft
keyword is fine. It is the declaration's alternative external name, and the
default JSON name when no `@json-name` is given.

`@json-name("…")` takes any non-empty text except `"$case"` (reserved for
the enum-member discriminator). It overrides the JSON name only, leaving the
declared name and any `@name` untouched everywhere else.

Effective JSON name: `@json-name` if present, else `@name`, else the
declared name. This is the object key a record or exception field encodes
and decodes under, and the JSON tag naming a record wherever it inhabits an
enum, wherever a value crosses JSON: agent structured output, `as`/`as?`
casts, program parameters, and [uncaught-exception
reports](host-environment.md#results-and-termination). A dict key of an enum
whose members have no fields encodes as its member's tag ([Dict wire
forms](types.md#dict-wire-forms)). Rendering (`print`, string
interpolation, …) always uses the declared name.

Two sibling fields or enum members whose effective JSON names collide, or
whose declared/`@name` spellings collide, are static errors, checked across
a record's own fields, an exception's inherited field chain, and an enum's
members (inline and referenced alike).

A record's, enum member's, or field's `@name` is also accepted in place of
the declared name in [host value syntax](host-environment.md#value-syntax):
`Square` and `sq` both construct the `Square` member below.

```agl
enum Shape
  | @name("sq") Square
      side: int
  | Rect
      @json-name("w") width: int
      @json-name("h") height: int
  | Circle

record Job
  @name("file") @json-name("file_path") file-path: path
  shape: Shape
```

## `@command`

`@command` registers a `program def` as a command of the package that owns its
module ([Packages](packages.md#programs-and-commands)). Its argument is a
command path: space-separated words naming the command a reader invokes, so
`@command("devel review")` is invoked as `devel review`. A path whose first
word is one of the host's own commands, or any word of which looks like an
option, is a static error; validating the package also rejects a first word
naming one of AgM's config sections
([`agm pkg`](../../commands/pkg.md#commands)). It appears at most once, and on
a `program def` alone.

A command is a way to refer to a program, so the registration carries no prose
of its own: the program's `@doc` describes it wherever it runs, the listing of
its command included.

```agl
@command("devel review")
@doc("Review the working tree and report findings by severity.")
program def main(@arg-pos subject: text) -> unit =
  print "reviewing %{subject}"
```

A program outside a package registers nothing: nothing reads its `@command`.

## `@allow` and `@exempts`

Both take one or more [constant text expressions](#constant-arguments), each
naming a [static warning](warnings.md). An unknown name, or a name repeated within
one attribute, is a static error; the error points at the offending argument.
Neither attribute changes what a program computes. Their semantics are described
under [Suppression with `@allow`](warnings.md#suppression-with-allow) and
[Exemption with `@exempts`](warnings.md#exemption-with-exempts).

`@allow` may prefix every declaration that takes attributes; its region is the
whole declaration, attributes included. `@exempts` may prefix any `def` form:
`def`, `program def`, `extern def`, `builtin def`, and a method; on any other
declaration it is a static error.

```agl
@allow("tab-indentation")
def double(n: int) -> int =
  @allow("noop-parse-retries")
  let result = n * 2
  result

@exempts("unquoted-exec-hole")
def passthrough(value: text) -> text = value
```

## Module parameters

`@param` exposes an ordinary static `let` or `var` binding as a module
parameter. The binding must be at module root or directly in a scope region
(never in a function, block, or lambda), must bind a name rather than `_`, and
must have an initializer; it cannot be a `builtin var`. Its annotated or
inferred type must be closed and decodable at the host parameter boundary.

How a host value takes the initializer's place: [Module
parameters](host-environment.md#module-parameters). A `var` stays mutable
after a host value is bound; when exported (see
[Modules](modules.md#re-exports-and-visibility)), an importer may also write
it.

The [host parameter attributes](#host-parameter-attributes) and `@doc` apply to
the binding; its external name is its `@opt-name`, else its declared name.
Flags, configuration routes, precedence, help, and REPL behavior:
[Module parameters](host-environment.md#module-parameters).

```agl
@param @doc("Log level") @opt-short("l") var level: int = 1

scope debug
  @param @opt-name("trace") let tracing = false
end debug
```

## `@config`

`@config` states, in source, the values a `program def` selects for module
parameters and engine settings when it is the program a host runs — the
source-level counterpart of that program's own configuration table. It takes
one or more `key = value` entries, comma-separated and optionally spanning
several lines with a trailing comma, and is legal only on a `program def`, at
most once.

Each `key` is an ordinary AgL reference — bare, qualified relative to the
enclosing scopes, anchored with a leading `::`, or a module route — resolved by
ordinary scope lookup in the scope that declares the `program def` (not its
own parameter scope). The same visibility and import rules apply as for any
reference: a target reached only through another module still needs that
module imported to be named.

Each key must resolve to a [module parameter](#module-parameters) — a
`@param` binding anywhere in the program's transitive import closure,
including its own module — or one of the root `std/config` [engine
settings](host-environment.md#engine-settings). Any other target (a plain
`let`/`var`, a non-engine `builtin var`, a function, a type) is a static
error, as is a key that resolves to no declaration — which includes a name
that only a signature parameter binds, since a `program def`'s own parameter
scope is not where a `@config` key resolves. Two entries addressing the same
target, by any two spellings, are a static error.

Each `value` is a [constant expression](bindings-and-scope.md#constant-expressions),
checked against the target's declared type — the module parameter's own
type, or the engine setting's type.

```agl
import std/config
import std/log

@param let retries: int = 1

scope Debug
  @param let verbose: bool = false
end Debug

@config(
  retries = 5,
  Debug::verbose = true,
  config::timeout = Some("30m"),
  log::level = log::Level::Debug,
)
program def main() -> unit = print retries
```

`@config` applies only when its `program def` is the **selected** program: a
program reached through an ordinary call, or a sibling `program def` in the
same module that is not selected, keeps its own parameters and settings
unaffected by it. `agm check` and the REPL validate a `@config` attribute
statically and apply nothing. Where `@config` ranks against the command line
and configuration files: [Precedence](host-environment.md#precedence).

## Host parameter attributes

`@opt-name`, `@opt-short`, `@opt-env`, `@opt-metavar`, and `@opt-hidden`
shape how a `program def` value parameter ([Program
definitions](program-structure.md#program-definitions)) or a
[`@param`](#module-parameters) binding is addressed by the host; their effects
are in the [catalog](#catalog). Beyond it:

- `@opt-name`'s flag word is ASCII letters and digits with single interior
  hyphens, so both `--word` and `--no-word` can be formed.
- `@opt-short` takes exactly one ASCII letter.
- An unset and an empty `@opt-env` variable both supply nothing.
- A positional-capable parameter under `@opt-hidden` keeps its usage slot.

A positional-only program parameter is never addressed by name, so `@opt-name`,
`@opt-short`, `@opt-env`, and `@opt-hidden` on one are static errors.
`@opt-metavar` and `@doc` still apply to it.

```agl
@doc("Deploy a release tag.")
program def main(
  @doc("Release tag.") @opt-metavar("TAG") @arg-pos tag: text,
  @doc("Cluster to deploy to.") @opt-name("to") @opt-env("DEPLOY_CLUSTER") cluster: text = "staging",
  @opt-hidden @opt-env("DEPLOY_SKIP_CHECKS") skip-checks: bool = false,
) -> unit =
  let mode = if skip-checks => "unchecked" else => "checked"
  print "deploying %{tag} to %{cluster} (%{mode})"
```

In `deploy.agl`, `agm exec deploy.agl v1.4 --to prod` deploys to `prod`, and
with no `--to` the cluster comes from `DEPLOY_CLUSTER`, else `staging`. Help
lists `TAG` and `--to`; `--skip-checks` and `DEPLOY_SKIP_CHECKS` work but stay
unlisted.

Value resolution and help: [Program arguments](host-environment.md#program-arguments)
and [Precedence](host-environment.md#precedence).
