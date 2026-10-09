# Types

[← Index](index.md)

AgL is statically typed with nominal user types, a small set of built-ins,
and directed implicit coercions. The full program is scope-resolved and
type-checked before any expression executes; checking stops at the first
error, and a program with a static error never runs.

## Built-in types

```text
unit
text
json
bool
int
decimal
array[T]
dict[K, V]
() -> B
A -> B
(A, B, …) -> C
```

A type expression is a built-in type, a function type, or a `name` — a user
type, an alias, or, inside a generic declaration, one of its type parameters —
optionally preceded by a [qualifier chain](lexical-structure.md#qualifier-chains)
(`mylib::Point`, `Geometry::Point`) and optionally applied to type arguments.
An **applied type** such as `Box[int]`, `Outcome[int, text]`, `Box[Box[int]]`,
or `mylib::Box[int]` instantiates a generic declaration; `array[T]` and
`dict[K, V]` are the same form. Type arguments are invariant. See
[Generics](generics.md) for type parameters, inference, and invariance, and
[Grammar](grammar.md#type-expressions) for the complete syntax.

There are no union types, no string-literal types, and no optional/nullable
types; model alternatives and optionality with enums.

### `unit`

`unit` is the type of expressions that exist only for their side effect. Its
only value is `()`, which is also the empty argument list of a zero-argument
call — the two are syntactically unified. `print(…)`, `:=`, an `if` without
`else`, and loops have type `unit`.

```agl
program def main() -> unit =
  let _: unit = print "hello"
```

`unit` has no JSON encoding and cannot be stored in a `json` slot; it renders
as `()`. In the REPL, an entry of type `unit` echoes nothing by default
([`agm repl` evaluation notes](../../commands/agl.md#evaluation-notes)).

### `text`

An immutable sequence of Unicode scalar values: a surrogate code point
(U+D800–U+DFFF) never appears in a `text` value. Every source of `text` from
outside the program — literals, parsed values, decoded bytes, host input —
rejects input that is not valid Unicode. Indexing (`s[i]`) returns the scalar
value at an integer index as `text`; negative indexes count from the end, and
an out-of-range index raises `IndexError`. Text cannot be changed through
indexed assignment. Untyped `ask` results default to `text` ([Agent
calls](agent-calls.md)). Text methods come from the standard library and may
also be declared by user modules ([Builtin-type
methods](#builtin-type-methods)).

### Numbers: `int` and `decimal`

There is **no binary floating-point type**.

- `int` — an arbitrary-precision integer. An integer of any length, written or
  decoded, is an exact `int`.
- `decimal` — an exact decimal number. Literals with a fractional part, such
  as `1.5`, are `decimal`.

`decimal` arithmetic uses a fixed context that is part of the language and
does not vary by host: 28 significant digits, round-half-even, and an adjusted
exponent bounded by ±999999, with subnormal values reaching below it. A
`decimal` value is always finite and within this range. Range is checked, not
precision: a value with more than 28 significant digits is created and kept
exactly. An out-of-range decimal literal is a compile-time error.

Every decoding boundary — JSON or TOML text, an agent or HTTP response, a
host-supplied program argument, an extern return value — rejects a non-finite
number with that boundary's own error, and rejects an out-of-range one for a
`decimal` target, so a `decimal` can never arise out of range. A `json` value
may hold a number of any finite magnitude, but not one no `decimal` can hold at
all (such as `1e99999999999999999999`).

A `decimal` operator (`+ - *` or `/`) rounds its result to 28 significant
digits; unary `-` is exact, including in a constant expression. A result that
underflows may lose precision or become zero; one that overflows, or is
otherwise invalid (such as division by zero), raises the catchable
`ArithmeticError` ([Exceptions](exceptions.md#arithmeticerror)), labelled with
the operator. Converting an out-of-range `int` to `decimal` — `as decimal`, or
the implicit widening of a mixed-operand operator, a `dict` key, a call
argument, or another `decimal`-typed context — raises the same way, labelled
with the operator or `as decimal`; `as? decimal` yields `None` instead.

Comparing an `int` with a `decimal` never widens: `== != < <= > >=` and `in`
compare the two exact values whatever the `int`'s magnitude, so they never
raise (`10.pow(30) + 1 == 1000000000000000000000000000001.0` is true). An
`int` outside the range is never `in` a `dict` with `decimal` keys.

On the JSON wire both kinds are plain JSON numbers, parsed and emitted
exactly. A number written without a fraction or exponent reads as an `int`;
any other reads as a `decimal`. Where the target expects the other kind, the
number converts as the cast would: an `int` widens, and a `decimal` narrows to
`int` only when integral ([`decimal as int`
integrality](#decimal-as-int-integrality)), so `2.0` fills an `int` target and
`2.5` does not. A `decimal` or `json` target keeps the exact value, trailing
zeros included, and its JSON encoding shows them — except for a number whose
coefficient digits plus exponent magnitude exceed 10,000, which is encoded in
scientific notation without trailing zeros.

### `bool`

`true` or `false`. Booleans never coerce to or from numbers.

### `json`

`json` holds any *JSON-shaped* value: `null`, booleans, numbers, text, and
arrays/dictionaries of JSON-shaped values. The literal `null` has type `json`.
Records, enums, exceptions, and functions are **not** JSON-shaped.

`null` is not assignable to `text`, `int`, `decimal`, `bool`, records, or
enums. Use an enum for optionality — the standard library's `Option[T]` is the
general form of this shape:

```agl
enum MaybeText
  | Missing
  | Present(value: text)
```

### `array[T]` and `dict[K, V]`

Homogeneous containers, and **mutable reference values**: binding, assignment,
passing as an argument, and storing in a field never copy an array or dict —
every alias shares the same underlying object. Elements are read with indexing
(`xs[0]`, `metadata["key"]`) and updated in place with indexed `:=` ([Bindings
and scope](bindings-and-scope.md#destructive-assignment)). There is no `len`
operator.

`dict[K, V]` admits any key type `K`. Only a *hashing operation* — a non-empty
dict literal `{k: v, …}`, indexing (`d[k]`), indexed assignment (`d[k] := v`),
and `k in d` — requires `K` to be `Hashable`
([Constraint blocks](generics.md#constraint-blocks)). The type itself, empty
`{}`, `for`, rendering, and `copy`/`shallow-copy` do not, so `K` may be
non-hashable, such as `array[int]`; `==` needs `Eq` on the whole dict type. A
dict with a `Hashable` key encodes to JSON in its key's form, and decodes back
when the key is also decodable ([Dict wire forms](#dict-wire-forms)).
Standard-library dict methods follow the same split.

Arrays, dicts, and records with a `var` field cross the [FFI](ffi.md) as live
views; other values cross as independent values. Use `copy`, `shallow-copy`,
or `as json` for a boundary snapshot.

### Builtin-type methods

`array[T]`, `dict[K, V]`, `text`, `json`, `int`, `decimal`, and `bool` can
have methods declared by any module ([Methods](functions.md#methods)); the
prelude makes the standard library's visible ([Prelude](modules.md#prelude)).

### Cycles

Because arrays, dicts, and `var` record fields are mutable references, a value
can come to contain itself — a genuine reference cycle:

```agl
record Node
  children: array[Node]

program def main() -> unit =
  let xs: array[Node] = [Node(children = [])]
  let n = Node(children = xs)
  xs[0] := n
```

A `var` field can close a cycle directly:

```agl
enum Link
  | End
  | Cell(var next: Link)
```

`End` makes this recursive declaration inhabited; a `Cell` may first point at
`End` and later receive itself with `cell.next := cell`.

Rendering (`print`, `render`, interpolation, REPL echo), `as text`, and
`as json` raise the catchable `CyclicValueError`
([Exceptions](exceptions.md#cyclicvalueerror)) if the walk re-enters a value
already on its own path; so does rendering a cyclic live view in an extern
companion. `as?` yields `None` instead. A **shared** (diamond) structure — the
same value reachable twice, but never from itself — is not a cycle. Three
operations traverse cycles without raising: `copy` (see [Copying
values](#copying-values)), `==`, which treats a pair already being compared as
equal and so terminates co-inductively, and trace logging, which records a
placeholder ([Tracing](host-environment.md#tracing)).

### Copying values

Because binding is by reference, an independent value must be requested
explicitly with the prelude functions `copy` and `shallow-copy`. Both are
generic and identity-typed (`T -> T`), accept an explicit type argument
(`copy::[decimal](5)`), and are first-class values once an expected type or
type argument fixes `T`:

```agl
program def main() -> unit =
  let clone: array[int] -> array[int] = copy
  let clone-level = shallow-copy::[array[int]]
```

`shallow-copy` rebuilds exactly **one** level: a new array, dict, record,
enum, or exception holding the *same* element or field references. Replacing
the copy's top-level contents — including a `var` field — does not affect the
original, but a container reached through the copy is shared with the
original. Every other value (`int`, `decimal`, `bool`, `text`, `json`, `unit`,
a function) is returned as-is. `shallow-copy` never recurses and never raises.

```agl
program def main() -> unit =
  let inner = [1, 2]
  let outer = [inner]
  let copied = shallow-copy(outer)
  copied[0][0] := 9
  print(outer)   # [[9, 2]]
```

`copy` is deep: every array, dict, record, enum, and exception reachable from
the value is rebuilt, and a `json` leaf is copied. Function values are
returned as-is, so a container reachable only through a closure stays shared.
**Sharing is preserved**: two slots pointing at one array before the copy
point at one new array after it. `copy` is the one deep walk that completes on
a cyclic value: it produces an isomorphic, independent cycle (which still
raises `CyclicValueError` when rendered).

```agl
program def main() -> unit =
  let shared = [1]
  let pair = [shared, shared]
  let copied = copy(pair)
  copied[0][0] := 9
  print(pair)     # [[1], [1]]
  print(copied)   # [[9], [9]]
```

### Function types

A function value has a positional function type. The parameters appear as a
type or a parenthesized comma-separated list of types; `->` separates them
from the result type. A single parameter may omit parentheses, and chained
arrows associate to the right:

<!-- agl-check: fragment -->
```agl
let f: int -> text = classify        # one param, text result
let g: (int, int) -> int = add       # two params, int result
let h: () -> bool = fn() => true     # zero params
let k: int -> text -> bool = chain   # int -> (text -> bool)
```

Function type assignability is by **exact structural match** — same parameter
count, parameter types in order, and result type; no variance applies. Named
and defaulted arguments belong to declared names (`def`s and built-ins), not to
function value types.

Function values are capability handles, not data: they render as an opaque
handle such as `<function: int -> int>`, but have no JSON encoding and no
equality. See [Functions](functions.md).

## Library types named by the language

The standard library declares these types and the language's own constructs
use them. The prelude ([Modules](modules.md#prelude)) makes them available
without an import.

### `Option` and `Optional`

`Option[T]` is `Some(value)` or `None`. `as?` yields one, and a possibly absent
value has this type, since ordinary types are not nullable. `Optional[T]` is
`Option[T]`'s `Some` and `None` members plus `Default`, for host parameters
that distinguish a value, explicit absence, and the host's default ([Program
arguments](host-environment.md#program-arguments)). A receiver typed by the
shared member (`Option::Some[T]`, `Option::None`) sees both enums' methods, so
`Some(1).map(f)` is ambiguous between `Option::map` and `Optional::map`;
annotate the value `Option[int]` first.

### `ExecResult`

The record an unannotated `exec` returns, with the command's output, exit
code, and timeout status ([Shell execution](shell-execution.md)).

### `Agent`

A builtin enum of agent backends whose members are records: `AgentPi` (the
default), `AgentClaude`, `AgentCodex`, and `AgentCommand`. Its `ask` and
`ask-request` methods select the receiver agent ([Agent
calls](agent-calls.md)).

### `AgentRequest`

The first-attempt request `ask-request` builds without dispatching: the agent,
the prompt, the output contract the call selects, the attempt number and
previous error, metadata, and the sandbox
([`ask-request`](agent-calls.md#ask-request-the-request-builder)).

### `Session`

An opaque, host-created handle for a continuing agent conversation
([Sessions](agent-calls.md#sessions)). Source cannot invoke its constructor,
and a `Session`, or any value whose type contains one, has no equality and no
`json` conversion.

### `Sandbox`

`Sandbox` is a builtin record naming a sandboxed run's resource limits
([Shell execution](shell-execution.md)). Every field has a default, so the
bare `Sandbox`, `Sandbox()`, and a partial call such as
`Sandbox(memory = Some("8G"))` are all valid constructions ([Fieldless and
all-defaulted constructor
references](expressions.md#fieldless-and-all-defaulted-constructor-references)).

### `AgentSandbox`

`AgentSandbox` is a builtin enum selecting a sandboxing mode: `Disabled`,
`Native`, or a referenced `Sandbox` record ([Enum types](#enum-types)). Because
the third member references `Sandbox` rather than declaring its own record, a
`Sandbox` value widens into an `AgentSandbox` slot with no rewrapping, and
`is`/`case` reach it under its own name (`Sandbox`, not
`AgentSandbox::Sandbox`). See [Engine
settings](host-environment.md#engine-settings) for
`std/config::default-sandbox`.

## Record types

A `record` declares a nominal product type, its fields in an indented block,
one per line:

```agl
record Issue
  title: text
  var severity: int
  description: text

program def main() -> unit =
  let positional = Issue("Missing tests", 3, "No failure-path tests exist.")
  let named = Issue(
    title = "Missing tests",
    severity = 3,
    description = "No failure-path tests exist."
  )
  named.severity := 1
  print(positional == named)   # false
```

- **Defaults.** A field without `= <constant expr>` is required; a field with
  one is optional, and a construction that omits it uses the default.
- **`var` fields.** A field marked `var` is a mutable reference slot:
  `receiver.field := value` updates the shared record in place, so every alias
  observes it. `var` is valid on records and enum-member records, not
  exceptions, and affects assignment only. `with`
  ([Record update](expressions.md#record-update)) always builds a new shallow
  value instead.
- **Fieldless records.** A fieldless record omits the body (`record Token`) or
  writes `record Token()`. Its bare reference constructs a value, as does that
  of a record whose every field has a default ([Fieldless and all-defaulted
  constructor
  references](expressions.md#fieldless-and-all-defaulted-constructor-references)).
- **Inline form.** A parenthesized field list is equivalent to the block:
  `record Point(x: int, y: int)`.

Two record types with identical fields are still distinct types, as are
same-named records from different modules or scopes ([Module- and
scope-qualified type identity](#module-and-scope-qualified-type-identity)). A
record may be generic ([Generics](generics.md)). Records, enums, and exceptions
may have [methods](functions.md#methods).

### Field zones

Record fields, inline enum-member fields, and an exception's own fields are
**standard** by default, whatever their number: each may be supplied
positionally or as `field = value`. An exception's inherited `message` field
is named-only ([Exceptions](exceptions.md)). The `@arg-pos`, `@arg-std`, and
`@arg-named` [zone attributes](attributes.md#zone-attributes) constrain
fields: in front of a field (on its line or the line above) they zone that
field; in front of the declaration, every field that carries none of its own.

```agl
record Pair[T1, T2]      # both fields standard
  fst: T1
  snd: T2

record Span              # first positional-only, last standard
  @arg-pos first: int
  last: int

@arg-named
record Labelled          # both fields named-only
  id: int
  label: text

record Mixed
  id: int
  @arg-named
  value: int
  @arg-named label: text

program def main() -> unit =
  let p = Pair(1, snd = "b")     # first positional, second named
  let s = Span(0, last = 4)      # Span(first = 0, last = 4) is an error
  let l = Labelled(id = 1, label = "x")
  let m = Mixed(7, value = 2, label = "y")
  print(s.last - s.first + p.fst + l.id + m.value)
```

A parenthesized field list takes the same attributes:
`record Span(@arg-pos first: int, last: int)`. Fields are listed in zone order —
positional-only, then standard, then named-only; a field after one from a
later zone is a static error. Rendering follows the same zones ([Uniform
rendering rules](strings-and-interpolation.md#uniform-rendering-rules)).

## Enum types

An `enum` declares a closed nominal union of record types. A value of an enum
is one of its member-record values; constructing a member does not wrap or
retag the record. A bare member name declares a new record in the enum's
scope, fieldless or with fields:

```agl
enum Outcome
  | Ok(value: int)
  | Err(reason: text, fatal: bool)

let ok = Outcome::Ok(42)
let ok2 = Outcome::Ok(value = 42)
let err = Outcome::Err("bad", false)
let named-err = Outcome::Err(reason = "bad", fatal = false)
```

Member fields follow the record rules — standard by default, positionally or
by name — and accept defaults, `var`, and zone attributes
(`| @arg-named Values(var a: int, b: int)` makes every field named-only). They
may also be written in an indented block under the member, one per line, with
attributes on the line above a field. One enum may mix both forms, though by
convention it uses one:

```agl
enum VerifyResult
  | Verified
  | Completed
      @doc("absolute file path of the next unblocked task")
      next-task-file: path
  | Rejected
      reason: text
  | Blocked
      reason: text
      recoverable: bool = false
```

A qualified member spelling instead **references** an existing record. The
reference may apply the enum's type parameters, and aliases are transparent:

```agl
record Saved
  id: int

record Box[T]
  value: T

enum Stored[T] = ::Saved | ::Box[T] | Fresh(value: T)
```

A reference must name a record, possibly through a type alias. An enum may not
name one member declaration twice, even at different type arguments, and its
members' terminal names must be distinct. The enum's scope contains only its
inline members: `Stored::Fresh` exists, `Stored::Saved` does not, and
referencing a record does not re-export it. Every member's terminal name is
also injected as a bare constructor and pattern candidate wherever the enum is
reached ([Injected enum members](scopes.md#injected-enum-members)).

Each member is a record type usable in any type position, such as
`array[Stored::Fresh]`; a referenced member keeps its own type and path.
Member records support construction, field access, methods, `with`, casts,
and standalone JSON decoding like other records. A member's methods include
those of every enum that declares or references it, widening the receiver to
that enum when one is selected ([Methods](functions.md#methods)). A member
value widens to an enum only in a known enum-typed slot, so its inferred type
is the member record type; `with` applies while it has that type.

An inline member captures exactly the enum type parameters its fields use, in
enum declaration order: `Tree[T]::Leaf` is fieldless and non-generic, while
`Tree[T]::Node(value: T)` captures `T`. A member type may be selected from an
applied owner, `Reply[text]::Answer`, which is already concrete and takes no
further type arguments; `Reply::Answer[text]` applies the member directly.

Enums are the intended model for agent outcomes. Their JSON wire shape (a
member tag string, or a `"$case"`-tagged object) is described under [Nominal
types `as json`](#nominal-types-as-json-structural-encoding); construction in
[Expressions](expressions.md#enum-member-construction), and destructuring in
[Pattern matching](pattern-matching.md).

## Builtin type declarations

`builtin record`, `builtin enum`, `builtin exception`, and `builtin type`
declare host-recognized types; they are a standard-library authoring form.
The `builtin` modifier may sit on the declaration keyword's line or the line
above it. The declared name and complete shape — fields, or members and their
fields — must match a recognized built-in exactly, and each complete scoped
name may be declared only once across the program, so a program loading the
default standard library cannot redeclare one at the same path ([Builtin
declarations](scopes.md#builtin-declarations)).

A builtin type declaration also names the type of the values the host
produces under that name: an unannotated `exec` returns the program's
`ExecResult`, `ask-request` builds its `AgentRequest`, and a built-in
exception the host raises is its `builtin exception` of that name. Such a
value carries that declaration's path, so it renders under it and a `catch`
naming the declaration matches it. Consequently:

- If a program declares its own `builtin` for a name, a `catch` naming the
  standard declaration of that name is rejected.
- The host fills nominal fields with standard values, so a field whose type is
  nominal — `AgentRequest`'s `agent` and its `Option` fields, and the `agent`
  field of `AgentCallError` and `AgentParseError` — must resolve to the
  standard type. Redeclaring both such a field's type (a program's own
  `builtin enum Agent`, or `Option` under `--no-stdlib`) and its containing
  builtin, so that the field resolves to the redeclaration, is an error at the
  `ask`/`ask-request` call or the `catch` clause that uses them.
- The agent an `ask` or `ask-request` dispatches to, as argument or receiver,
  must be the standard `Agent`.

## REPL type redeclaration

In the REPL, redeclaring a record, enum, or exception creates a new type that
shares the old one's name and display but is unrelated to it:

- Every later spelling of the name — constructor, annotation, `catch`,
  pattern, `is`, bare or qualified — means the new declaration. The new
  declaration starts with no methods.
- A value built earlier keeps its declaration's fields, members, methods, and
  equality; using it where the new type is expected, or comparing it with a
  new value, is a type error. It still matches `_`, renders, and matches or
  casts through an alias declared before the redeclaration: with
  `type OldA = A` declared first, an earlier `A::Tint` value `v` still
  satisfies `v is OldA::Tint`.
- An alias names its type permanently; no redeclaration or import retargets
  it. A path beneath an alias reads the current target path: after
  `type OldB = Base` and a redeclaration of `Base`, `OldB::h` reads whatever
  `Base::h` currently is.
- A failed entry changes nothing. Redeclaring a record an existing enum
  references, or the enum declaring a referenced member, keeps that enum's
  member set but stops injecting the superseded member. Redeclaring an enum
  creates new inline member types, and a scope nested under a superseded
  member becomes unspellable.

## Recursive types

A record, enum, or exception may reference its own type, directly or through
other declarations, in its fields or enum-member fields — within one module or
across any number of imported modules:

```agl
enum Tree
  | Leaf
  | Node(value: int, left: Tree, right: Tree)

record Category
  name: text
  subcategories: array[Category]

exception Retryable extends Exception
  causes: array[Retryable]
```

### Inhabitation

Recursion is legal only when a finite value can be built — the type must be
**inhabited**. The chain is broken by:

- an enum member that needs no value of the same (or a mutually recursive)
  type — a **base case**, such as `Leaf`;
- an `array[T]`/`dict[K, V]` field, since the empty container is always a
  value, as with `Category.subcategories`.

A record or exception with one required field that needs a value of the same
or a mutually recursive declaration with no such escape, or an enum each of
whose members does, has no finite value and is rejected at the declaration:

<!-- agl-check: error -->
```agl
record Node
  next: Node
# Rejected: every value of 'Node' would be infinite.
```

This covers an enum whose only member carries itself and a mutually recursive
pair with no base case or container field on either side. `Exception`, which
is not constructible itself, is inhabited through its constructible
descendants. An inline enum
member is a record of its own, so the rule applies to each member: a
base-case sibling does not rescue a member that requires itself:

<!-- agl-check: error -->
```agl
enum Chain
  | Link(next: Chain::Link)
  | End
# Rejected: every value of 'Chain::Link' would be infinite, although 'Chain'
# itself has the base case 'End'.
```

A generic declaration applied to an instance of itself, such as
`Box[Box[int]]`, is not recursion: it is inhabited according to its arguments.
For recursion at a different type argument, see [Recursive generic
types](generics.md#recursive-generic-types).

### Recursive aliases are not allowed

A `type` alias may not be recursive, directly or through another alias: an
alias has no nominal identity to anchor a cycle, so recursion must pass
through a `record`, `enum`, or `exception`.

## Module- and scope-qualified type identity

Every `record`, `enum`, and `exception` declaration introduces its own type,
identified by its **defining module, scope path, and name**. Two same-named
types are distinct when they come from different modules or named scopes,
including a module root and a scope in that module, whatever their structure:

<!-- agl-check: fragment -->
```agl
# In foo.agl
record Point
  x: int
  y: int

# In bar.agl
record Point
  x: int
  y: int

# In entry.agl
import foo
import bar

let p: foo::Point = foo::Point(x = 0, y = 0)
# let q: bar::Point = p   # static error: bar::Point is not foo::Point
```

Because a type is its declaration, not its name, the same path declared twice
in one module — which only the REPL allows — yields two distinct types ([REPL
type redeclaration](#repl-type-redeclaration)).

### Qualified type references

A type in another module or a named scope is spelled with its qualifier in
annotations, cast targets, and constructor expressions. Type arguments follow
the complete name, or a qualifier segment under the [segment type-argument
rule](lexical-structure.md#qualifier-chains):

<!-- agl-check: fragment -->
```agl
import mylib
import mylib as M

let p1: mylib::Point = mylib::origin()
let p2: M::Point = M::origin()
let b: mylib::Box[int] = mylib::Box(value = 2)

scope A
  record Token
end A

let token: A::Token = A::Token()
```

A type position takes only type declarations — records, enums, exceptions,
aliases, and builtin types — looked up by whole path at each [lookup
step](scopes.md#names-and-visibility). A scope region is no type, so an own
`scope Geo` never hides an imported `record Geo`.

### Self-reference: `::TypeName`

`::TypeName` names the **current module's own** type, read from the root,
bypassing every enclosing scope, import, and `use`:

```agl
record Node
  value: int

def make() -> ::Node = Node(value = 0)
```

## Type aliases

`type` declares a transparent alias:

```agl
enum Review
  | Pass
  | Fail(issues: array[text])

type Status = Review
type Issues = array[text]
type Metadata = dict[text, json]
```

An alias is another name for the type its target denotes, never a new type. The
target is resolved where the alias is declared, through that site's own
declarations, imports, and `use` declarations, and means that wherever the
alias is used. An alias whose target names no type, or that is part of an
alias cycle, is an error at its declaration whether or not it is used (a cycle
at the alias declared first). Alias chains resolve transitively.

A spelling through an alias means what the same spelling through its target,
as written, means at the alias's declaration — in every position: type, value,
constructor, pattern, `is`, `use` target, import and export items, and
`hiding`. With `type Geo = Base`:

- **Paths beneath.** `Geo::Inner` means what `Base::Inner` means at the alias's
  declaration, whether or not `Base` is nameable where `Geo` is read. A reading
  module's own declarations beneath `Base` are not reached through another
  module's `Geo`, and a target spelled with a module route
  (`type Geo = base::Base`) reaches only what that module exports.
- **Anchors.** `::Geo::f` reads only the own module's `Base::f`; the route
  `al::Geo::f` only what module `al` exports beneath `Base`.
- **No declarations beneath.** An alias declares no scope. Declaring beneath a
  name the module declares as an alias — `def Geo::f()`, `let Geo::v = 1`,
  `record Geo::Part`, or `scope Geo` — is an error, whichever comes first;
  declare beneath the target. Beneath an *imported* alias's name, a module's
  own declaration stands at its written path and wins over what the alias
  reaches.
- **Receivers.** `def Geo::m(self)` is an error: a method receiver names its
  type directly ([Methods](functions.md#methods)).
- **One declaration.** Reaching a type directly and through aliases, even
  aliases from different modules, is never an ambiguity; aliases of two
  distinct types still clash.
- **Own aliases are own paths.** A path reached through an alias the module
  declares counts as the module's own: with `type Color = lib2::Color` beside
  `import lib::*` whose `Color` also declares `Red`, `Color::Red` is
  `lib2::Color::Red`; where only the imported type declares `Green`,
  `Color::Green` reaches that one.

```agl
record Base
  x: int

record Base::Inner
  y: int

type Geo = Base

def Base::m(self) -> int = self.x

scope Base
  def make() -> Geo = Geo(x = 2)
end Base

def Base::twice(self) -> int = self.m() * 2

program def main() -> unit =
  let b = Geo::make()
  let i: Geo::Inner = Base::Inner(y = 3)
  print(b.twice() + i.y + Geo::twice(b))
```

**Constructors.** An alias of a record or exception is also its constructor.
The owner-qualified form `Point::Point(…)` accepts, in either position, the
type's name or any alias on the chain to it: with `type P = Point` and
`type Q = P`, each of `Q::Q(…)`, `Q::P(…)`, `Q::Point(…)`, `P::P(…)`, and
`P(…)` constructs a `Point`, in expressions and patterns, while `P::Other(…)`
is an error. A `use` rename names the type too: after
`use lib::{Point as R}`, `R::R(…)` is `lib::Point::Point(…)`.

**Applied aliases.** An alias of an applied generic names that applied type
and fixes its type arguments: with `type B = Box[int]`, `B::B(value = 1)` is
`Box[int]::Box(value = 1)`, and `B(value = "s")` or a `B::B(…)` pattern on a
`Box[text]` is an error. Any other path beneath a bare alias segment reads
beneath the target's head: `B::helper()` for a static `def Box::helper()` is
`Box::helper()`, while a written application used as a segment
(`Box[int]::helper()`) stays under the [segment type-argument
rule](lexical-structure.md#qualifier-chains). Aliases of the same applied type
are one declaration; `B` and the generic `Box` are distinct, so `hiding Box`
leaves `B`. Whether an
alias renames or applies is decided after expanding every alias in it: with
`type Id[T] = T`, `type IB = Id[Base]` renames `Base`, and an alias that
passes a parameter through (`Id`) reaches nothing beneath its bare name. A
parameterized alias constructor infers only the parameters its target
mentions; an explicit application still supplies every declared one: with
`type Tagged[X] = Point`, `Tagged(x = 1)` and `Tagged::[int](x = 1)` both
construct a `Point`.

**Enum aliases.** An alias of an enum is a transparent owner of its members.
With `enum Reply[T] = Silent | Answer(value: T)`, `type Texts = Reply[text]`,
and `type Rows[A] = Reply[array[A]]`:

- `Texts::Answer` is `Reply[text]::Answer` in types, constructors, patterns,
  and `is` tests, and carries the alias's type arguments:
  `Texts::Answer(value = 1)` is an error.
- A parameterized alias names a member type only once applied
  (`Rows[int]::Answer`). An unapplied `Rows::Answer` in a constructor,
  pattern, or `is` test infers `A`: `Rows::Answer(value = ["a"])` constructs a
  `Reply[array[text]]::Answer`, while `Rows::Answer(value = 1)` and a
  `Rows::Answer` test on a `Reply[int]` are errors.
- A bare member name the alias injects — through an import item naming the
  alias, the module's own alias, or `use Texts::*` — is the *generic* member,
  whose type arguments inference fixes as for any constructor: with only
  `Texts` imported, bare `Answer(value = 1)` constructs a `Reply[int]` member.
  Every route to one member is one candidate, so importing both `Reply` and
  `Texts` leaves bare `Silent` unambiguous.

**Other aliases.** An alias of a structural type (`type F = int -> bool`)
reaches no paths; two aliases of the same structural type are one declaration.
An alias of a builtin type reads as that builtin (`type T2 = text` makes
`T2::size` mean `text::size`), but methods are declared on the builtin
directly (`def text::shout(self)`). An `is` test through an alias tests the
declaration it denotes; for an exception alias, that includes descendants.

`builtin type path = text` is the one builtin alias: `path` is `text`, naming a
filesystem location, so `Box[path]` and `Box[text]` are one type. The
standard library declares it and the prelude forwards it; where no declaration
of `path` is visible, as under `--no-stdlib`, `path` still denotes `text`,
while a visible declaration of that name takes precedence. A host presents a
program parameter annotated `path`, `Option[path]`, `Optional[path]`, or an
alias of one as a filesystem location ([Host
environment](host-environment.md#program-arguments)); any other annotation,
including a non-builtin alias named `path`, is ordinary `text`.

## Declaration validity

The following are static errors:

1. A user type whose name duplicates another user type, a built-in type name,
   or a built-in exception name ([Exceptions](exceptions.md)).
2. Duplicate record fields, duplicate enum member declarations or terminal
   names, or duplicate fields within one inline member.
3. References to unknown types in records, enums, aliases, or function
   parameter declarations.
4. Cyclic aliases, and declarations beneath a name the same module declares
   as an alias ([Type aliases](#type-aliases)).
5. An **uninhabitable** record, enum, or exception ([Inhabitation](#inhabitation)).

Type declarations are valid at the module root and in [named scope
regions](scopes.md), not in expression blocks.

## Assignability and coercion

Typing is exact nominal matching with these implicit coercions:

1. **`int` widens to `decimal`.** An `int` is accepted wherever a `decimal` is
   expected, and mixed arithmetic yields `decimal`.
2. **A `json` target accepts any *scalar* JSON-shaped value** — `null`,
   `bool`, `int`, `decimal`, or `text`.
3. **An enum member record widens to an enum that declares or references it.**
4. **An enum widens to another enum that contains all of its constructors**,
   at matching type arguments; the value stays the same record.
5. **A derived exception widens to any ancestor in its `extends` chain**,
   keeping its concrete identity; `as` to a named ancestor does the same
   explicitly ([`try`/`catch`](exceptions.md#try-catch)).

Rules 3–5 apply only against a known target slot — they never find a common
type while inferring a mixed expression (`[Pass, Fail(issues = [])]` needs an
annotation) — and never propagate through containers or type arguments. There
are no other implicit conversions: in particular an `array` or `dict`, even a
JSON-shaped one, is never implicitly absorbed into `json`, because an
implicit conversion never builds a data structure; write `as json`. Without a
known target slot, all branches of a `case` or `if` must have the same type
after rule 1; against a slot, each branch is checked against it. Equality
operands follow [Values and equality](#values-and-equality).

## Casts and convertibility

AgL provides two cast operators ([Expressions](expressions.md#casts-as-and-as)
for syntax and precedence):

- **`EXPR as T`** — converts the value to type `T`, raising `CastError`
  ([Exceptions](exceptions.md)) when a fallible conversion fails.
- **`EXPR as? T`** — the same conversion without raising: `Some(value)` on
  success, `None` on failure, including where `as` would raise
  `CyclicValueError` or `ArithmeticError`.

Among enum members, enums, and exceptions, casts are **identity casts**,
never parsing conversions: the result is the same value, and a fallible one
checks the runtime nominal identity ([Conversion matrix](#conversion-matrix)).
A cast between unrelated exception types, siblings included, is a static
error. See
[Exceptions](exceptions.md#casts-and-is) for exception `is` tests.

### Conversion matrix

A **total** conversion has no conformance failure, so it never raises
`CastError` — though rendering or JSON conversion still raises
`CyclicValueError` on a reference cycle. A **fallible** conversion may raise
`CastError`. A **static cast error** rejects the combination before the
program runs. A cast to the source's own type is an accepted no-op; for an
array or dict it yields the *same* value, not a copy, unlike `as json`, which
builds an independent snapshot.

| Target type | Permitted source types | Outcome |
| ----------- | ---------------------- | ------- |
| `text` | any data type (`text`, `json`, `bool`, `int`, `decimal`, `array[E]`, `dict[K,V]` for any `K`, record, enum, exception) | total — renders the value as AgL-form text |
| `json` | any type with a JSON representation — see [Convertibility to `json`](#convertibility-to-json) | total — canonicalizes the value to `json` |
| `bool` | `bool` | total (no-op) |
| `bool` | `text`, `json` | fallible — value must be a JSON boolean |
| `int` | `int` | total (no-op) |
| `int` | `decimal` | fallible — decimal must have no fractional part |
| `int` | `text`, `json` | fallible — value must be an integral number |
| `decimal` | `decimal` | total (no-op) |
| `decimal` | `int` | total (widening, same as the implicit coercion) |
| `decimal` | `text`, `json` | fallible — value must be a number |
| `array[E]` | identical `array[E]` | total (no-op) |
| `array[E]` | `text` | fallible — [parse](#parsing-values), then element validation |
| `array[E]` | `json` | fallible — element validation |
| `dict[K,V]` | identical `dict[K,V]` | total (no-op), for any key type `K` |
| `dict[K,V]` for a `Hashable`, decodable `K` | `text` | fallible — [parse](#parsing-values), key decoding per its wire form, then value validation |
| `dict[K,V]` for a `Hashable`, decodable `K` | `json` | fallible — key decoding per its wire form, then value validation |
| record `R` | same record `R` | total (no-op) |
| record `R` | `text` | fallible — [parse](#parsing-values), then field validation |
| record `R` | `json` | fallible — field validation |
| enum `E` | same enum `E` | total (no-op) |
| enum `E` | member record `R` of `E` | total identity upcast (no-op) |
| member record `R` of `E` | `E` | fallible identity downcast — checks that the runtime member is `R` |
| enum `B` | enum `A` whose constructors `B` all contains | total identity widening (no-op) |
| enum `B` | enum `A` sharing some but not all constructors with `B` | fallible identity cast — checks that the runtime constructor belongs to both |
| enum `E` | `text` | fallible — [parse](#parsing-values), then member validation |
| enum `E` | `json` | fallible — member validation |
| exception type `T` | `T`, or any descendant of `T` | total identity upcast (no-op), including to the root `Exception` |
| exception type `T` | any ancestor of `T` | fallible identity downcast — checks that the runtime type is `T` or a descendant |
| exception type | `text`, `json` | **static cast error** — an exception cannot be decoded |
| `Agent` | `text` | fallible — a JSON object, an `Agent` member constructor call, or shorthand; no verbatim command fallback |
| `Agent` | `json` | fallible — validates a tagged `Agent` member object |
| any type | `unit`, function type | **static cast error** |
| `unit`, function type | any type | **static cast error** |

Any combination not listed is a static cast error; in particular, booleans
never convert to or from numbers.

### Parsing values

When the target requires structure (`bool`, `int`, `decimal`, array, dict,
record, or enum), a `json` source is only **validated** against the target's
shape. A `text` source is first **parsed** — strictly, with no surrounding
prose, no Markdown fences, and no recovery — as exactly one JSON value when
that JSON decodes into the target, and otherwise as exactly one AgL
[value-syntax](host-environment.md#value-syntax) literal; the result is then
validated. A JSON object with a duplicate member name is rejected, by every
JSON parse in the language. (Agent output, by contrast, is parsed with lenient
recovery by default — [Agent calls](agent-calls.md).)

The prelude functions `parse::[T](text)` and `try-parse::[T](text)` accept
exactly the targets `as T` does and convert text the same way, but raise
`ValueParseError` ([Exceptions](exceptions.md#valueparseerror)) instead of
`CastError`; `try-parse` never raises and returns `Result::Err` with the
error instead. A `parse::[json]` target parses strict JSON or value-syntax
plain data. A `text` target returns the text unchanged. `T` comes from an
explicit `::[T]` or the expected type, and a call supplying neither is a static
error; both are call-only, never function values.

#### `text as json` — embedding, not parsing

Because `text` is already JSON-shaped, `"42" as json` is the JSON **string**
`"42"`: a total cast that wraps the text without interpreting it. To read a
text's contents as JSON, use `parse::[json]("42")`, which yields the JSON
number `42`.

### `decimal as int` integrality

`decimal as int` succeeds only when the value has no fractional part:
`3.0 as int` yields `3`, while `3.5 as int` raises `CastError`. The same rule
narrows a `json` or wire number to `int`, which must also lie within the
`decimal` range: a `json` number of magnitude `1e1000000` or more does not
narrow.

### Convertibility to `json`

A type converts to `json` iff no **non-data** type — `unit`, a function type,
or `Session` — and no dict with a non-`Hashable` key is reachable from it:

- `text`, `json`, `bool`, `int`, and `decimal` always convert;
- `array[E]` converts iff `E` does; `dict[K, V]` iff `K` is `Hashable` and `V`
  converts;
- a record, enum, or exception converts iff neither is reachable from its
  declaration at its type arguments, transitively through its fields — for an
  exception also through its ancestors and its descendants, since a
  base-typed value may hold a descendant.

So `array[R] as json`, `dict[text, array[R]] as json`, and a recursive
`record Node(tag: int, children: array[Node])` all convert exactly as `R`
does, and a record with a `unit` or function field is a static error. A
**free type variable never converts**: `T as json`, `array[T] as json`, and
`Box[T] as json` are static errors inside a generic `def`, since type
arguments are erased.

### Nominal types `as json` — structural encoding

Records, enums, exceptions, and containers of them convert to `json` only by an
explicit `as json`, never implicitly. The encoding is structural:

- **record** → a JSON object with one key per field, in declaration order,
  keyed by each field's effective JSON name (its declared name unless
  [`@name`/`@json-name`](attributes.md#name-and-json-name) overrides it).
- **plain enum** (every member fieldless) → the JSON string of the member's
  effective tag: `"Pass"`.
- **any other enum** → a JSON object with a `"$case"` key holding the member's
  effective tag, plus one key per member field; a fieldless member is the
  object with only `"$case"`. The shape follows the slot's static type: a
  record that is a member of both a plain and a non-plain enum is a string in
  the first slot, a tagged object in the second, and its own record object in
  a record-typed slot.
- **exception** → a JSON object with every field of the value's runtime
  exception type, in declaration order.
- **`array[E]`/`dict[K, V]`** → the JSON array, or the dict's
  [key-directed form](#dict-wire-forms), converting each element, key, and
  value the same way.

Except for exceptions and any type that reaches one (which cannot decode), this
is exactly what the decoding rows of the [conversion matrix](#conversion-matrix)
accept, so a round trip recovers
the value: `(rs as json) as array[R] == rs`.

```agl
record R
  x: int

program def main() -> unit =
  let r: R = R(x = 1)
  print r                                   # R(x = 1)
  print(r as json)                          # {"x": 1}
  print render(r as json, pretty = true)
```

A finite value of a JSON-convertible recursive type always converts, even a
growing polymorphic-recursive one such as `Perfect[int]`; a schema boundary
still requires a finite schema ([The finite-schema
boundary](generics.md#the-finite-schema-boundary)).

### Dict wire forms

A dict's JSON form is chosen by its static key type `K`, so a generic field
`dict[K, V]` takes the form of each instantiation's key type:

| Key type | JSON form |
| -------- | --------- |
| `text`, or a text alias such as `path` | object keyed by the text |
| `int`, `decimal` | object keyed by the number's exact JSON text (`"-2"`, `"1.50"`) |
| `bool` | object keyed by `"true"` or `"false"` |
| an enum whose members all have no fields | object keyed by the member's effective `$case` tag (see [`@json-name`](attributes.md#name-and-json-name)) |
| any other `Hashable` key: a record, an exception, `json`, `Option[...]`, an enum with a member that has fields | array of `{"key": <key>, "value": <value>}` objects, each key in its own JSON form |

The [derived JSON Schema](agent-calls.md#derived-json-schema) follows the
same forms. Every form keeps insertion order, and an empty dict still takes
its key type's form.

```agl
enum Color
  | Red
  | @json-name("bleu") Blue

record Point
  x: int
  y: int

program def main() -> unit =
  let by-int: dict[int, text] = {1: "a", -2: "b"}
  let by-color: dict[Color, int] = {Red: 1, Blue: 2}
  let by-point: dict[Point, text] = {Point(x = 1, y = 2): "p"}
  print(by-int as json)    # {"1": "a", "-2": "b"}
  print(by-color as json)  # {"Red": 1, "bleu": 2}
  print(by-point as json)  # [{"key": {"x": 1, "y": 2}, "value": "p"}]
```

Decoding — a cast from `text` or `json`, `parse`, agent and `exec` output, and
program parameters — reads the same forms for any `Hashable`, decodable key
type; host text and text casts may also spell a dict in [value
syntax](host-environment.md#value-syntax). A stringified key is read as its
type: a number key follows the number rules (`"2.0"` fills an `int` key,
`"2.5"` does not), a `bool` key is exactly `"true"` or `"false"`, and an enum
key is a member's effective tag. An entries element must be an object with
exactly the members `key` and `value`.

Two wire keys that decode to equal keys (`"1"` and `"1.0"` for an `int` key,
or a repeated entry key) fail the conversion with the conversion's own
failure — `CastError` for `as`, `None` for `as?`, `ValueParseError` for
`parse`, `ExecError` for `exec` output, a retry or `AgentParseError` for agent
output, and a host invocation error for a program parameter.

A type-variable key cannot decode, bounded or not, and neither can an
exception key. A type that reaches a dict with a non-`Hashable` key — directly
or through type arguments, as `Outer[array[int]]` does when `Outer[T]` holds a
`Box[Wrap[T]]` and `Box[K]` a `dict[K, int]` — cannot be decoded, cast to
`json`, or used in an `extern def` signature (where a type-variable key counts
as `Hashable`). `as text` renders it.

## Values and equality

Every **data** type has full value equality (`==` / `!=`):

- Scalars compare by value; `int` and `decimal` compare numerically and
  exactly, without widening ([Numbers](#numbers-int-and-decimal)).
- Arrays compare element-wise; dicts by key set and per-key values.
- Records and exceptions compare by nominal type and field values. An enum
  value is its member record, so an enum-typed value compares with a
  member-typed one by value.
- `json` values compare structurally.

Both operands must have the same type after `int` → `decimal` widening, or
one operand's type must widen to the other's by [assignability
rules](#assignability-and-coercion) 3–5: `opt == None`, `Some(1) == opt`, and
a derived exception against an ancestor-typed value all check, while two
distinct member types (`Some(1) == None`) do not. Ordering comparisons accept
only numbers or `text` ([Expressions](expressions.md)).

Function types, `unit`, and `Session` have **no equality**, transitively: an
`array`, `dict`, record, enum, or exception that contains one at any depth
cannot be compared either, so `==` on two `array[int -> int]` values is a
static error. A type parameter is comparable only under an `Eq` or `Hashable`
bound ([Constraint blocks](generics.md#constraint-blocks)).

Equality is cycle-safe: cyclic values, including cycles through `var` fields,
compare structurally and never raise `CyclicValueError` ([Cycles](#cycles)).
