# Expressions

[← Index](index.md)

This chapter covers literals, constructors, member access, record updates,
indexing, operators, casts, and how expected types propagate. Everything in
AgL is an expression; a block's value and the `unit` rule for discarded values
are in [The block's value](program-structure.md#the-blocks-value) and
[Expression statements](program-structure.md#expression-statements). `if`,
`case`, and loops are in [Control flow](control-flow.md), calls in
[Functions](functions.md), and operator precedence in [Lexical
structure](lexical-structure.md#operator-precedence).

## Literals

<!-- agl-check: fragment -->
```agl
42            # int
1.5           # decimal
true          # bool (and false)
null          # json
()            # unit
"text %{x}"   # template (type text; see Strings and interpolation)
```

`()` is the unit value and also the empty argument list of a zero-argument
call — the two are syntactically unified.

### Array literals

```agl
let issues = ["missing tests", "unclear API"]
```

Elements must share a type, up to `int → decimal` widening. Under an expected
type, each element is checked against the expected element type. An empty array
may obtain its element type from an expected container type or another
constraint in the same enclosing expression:

```agl
def second[T](left: T, right: T) -> T = right

program def main() -> unit =
  let items = second([], [1])
```

Otherwise it needs an annotation (`let items: array[Issue] = []`).

Under an expected type of `json`, each element is checked against `json`
directly, so elements need **not** share a type — `[1, "two", true, null]` is a
valid `json` value — and the literal itself is typed `json`, not
`array[json]`: a fresh literal needs no `as json` cast. The same applies to
dictionary literals.

### Dictionary literals

```agl
let metadata: dict[text, json] = {
  "source": "reviewer",
  "attempt": 2,
}
```

A key is an ordinary expression of the dictionary's key type — a literal,
interpolated text, a constructor application, an arithmetic expression, or a
variable; a bare name is a reference, not a shorthand key. Under an expected
type of `json`, keys are always `text`. A non-empty literal's key type must
satisfy `Hashable`. An empty literal takes its key and value types from an
expected type or a constraint in the enclosing expression, or needs an
annotation (`let metadata: dict[text, json] = {}`).

Two entries are a static error when both keys are [constant
expressions](bindings-and-scope.md#constant-expressions) with a comparable
constant value: `null`, `bool`, int and decimal literals compare numerically
(`1` and `1.0` collide); text compares after folding templates and constant
references to the text each renders (`"%{prefix}-a"` collides with the text it
folds to); a constructor application — direct or through a constant alias —
compares by constructor identity with its arguments normalized to field names
(`Point(1, 2)` collides with `Point(x = 1, y = 2)`). Folding never crosses a
`var`, whose value can change, or a module parameter, whose value the host
settles at run time; a template hole folds only a reference of scalar type
(`text`, `int`, `decimal`, `bool`), so a `json` constant in a hole is not
compared. Keys
with no comparable constant value are left to run time: two entries whose keys
compute equal values raise catchable `DuplicateKeyError`.

## Constructors

Constructor arguments bind **positional-greedy**, as in calls
([Functions](functions.md#calling-functions)): positional arguments fill
positional-capable fields left to right, then named arguments (`field =
value`) follow, and a bare name landing on a named-only field means
`name = name`. Fields are standard (positional or named) unless a zone
attribute says otherwise ([Field zones](types.md#field-zones)); an
exception's inherited `message` is named-only. Every required field must be
supplied; unknown and duplicate fields are static errors. A constructor with
a required field is also a function value ([Constructors in the value
namespace](bindings-and-scope.md#constructors-in-the-value-namespace)). The
grammar of qualified and type-applied constructor references is in
[Grammar](grammar.md#expressions).

### Record construction

```agl
record Issue
  title: text
  severity: int
  description: text

let bug = Issue(title = "Bug", severity = 2, description = "Crash on save.")
let same = Issue("Bug", 2, description = "Crash on save.")
```

### Enum member construction

An enum establishes a same-named scope, and each inline member is a record in
that scope; every member also contributes its terminal name as an injected bare
constructor candidate. A module qualifier reaches an inline member of one of
the module's root enums by its terminal name (`mylib::Pass`, `::Pass`); see
[Module-qualified enum members](modules.md#module-qualified-enum-members).

```agl
enum Review
  | Pass
  | Fail(issues: array[text])

let pass = Review::Pass                         # type Review::Pass
let fail = Fail(issues = ["missing tests"])     # type Review::Fail
let review: Review = Pass                       # widened in an enum-typed slot
let reviews: array[Review] = [Pass, Fail(issues = [])]
```

A member constructor produces its own record type; it widens to the enum only
in an enum-typed slot. This is a directed check, not common-type inference:
`[Pass, Fail(issues = [])]` without the annotation has no inferred enum type.
A bare member name must resolve to exactly one visible constructor; an
expected enum type cannot choose between same-named candidates
([Overload sets, shadowing, and
ambiguity](bindings-and-scope.md#overload-sets-shadowing-and-ambiguity)).

### Generic constructors

Constructors of generic records and enums are generic. Their type arguments
are inferred from the arguments, the expected type, or sibling arguments of an
enclosing call, or pinned with `::[…]` (`Box::[int](value = 1)`) or an applied
owner (`Option[int]::Some(value = 2)`). See [Constructor type
arguments](generics.md#constructor-type-arguments).

### Fieldless and all-defaulted constructor references

A fieldless constructor reference — a standalone record or an enum member,
bare or qualified — constructs its value immediately in value position. So
does one whose every field has a `=` default, filling every field with its
default:

```agl
record Marker
record Settings
  retries: int = 3
  verbose: bool = false

enum Status
  | Ready
  | Failed(reason: text)

program def main() -> unit =
  let marker = Marker
  let ready = Status::Ready
  let settings = Settings
  print(settings.retries)          # 3
  print(settings == Settings())    # true — same value either spelling
```

The call forms `Marker()`, `Status::Ready()`, and `Settings()` construct the
same values. Such a reference is not a `() -> T` function value; supply an
explicit function where one is required:

```agl
record Marker
def invoke(factory: () -> Marker) -> Marker = factory()

program def main() -> unit =
  let result = invoke(fn() => Marker)
```

A generic fieldless constructor takes its type arguments from context or from
`::[…]` ([Constructor type
arguments](generics.md#constructor-type-arguments)).

### Exception construction

Exception types are constructed like records:

```agl
program def main() -> unit =
  raise Abort(message = "Cannot continue.")
```

## Member access

`expr.member` projects a field or a method. Fields belong to records (member
records and `ExecResult` included) and exceptions; methods belong to records,
enums, exceptions, and built-in receiver types. A field projection yields the
field's value; a method projection yields a bound function value whose receiver
is the value left of the dot:

```agl
record Meter
  var value: int

def Meter::add(self, amount: int) -> int = self.value + amount

program def main() -> unit =
  let meter = Meter(value = 4)
  let value = meter.value
  let add = meter.add          # bound method
  let plus = meter.add(?)      # partial application
  meter.value := 7
  print(value)                 # 4
  print(add(3))                # 10
  print(plus(5))               # 12
```

A leading-dot expression (`.value`, `.add(3)`) is a unary function over a
contextual receiver, as in `meters |> .map(.value)`. Arrays and dictionaries
have no fields; use indexing. An enum-typed value has no fields, even when
every member defines the same field, and exposes only the enum's own methods;
narrow it with a pattern or a member-record cast first. A member-record value
also exposes the methods of every enum that declares or references it. A
same-named field and visible method are ambiguous. See
[Methods](functions.md#methods) for selection, visibility, and leading-dot
typing, and [Destructive
assignment](bindings-and-scope.md#destructive-assignment) for
`receiver.field := value`.

## Record update

`target with field = value, ...` builds a **shallow copy** of a **record** or
**exception** value with the listed fields replaced; an enum member record is
a record for this rule. Unlisted fields keep their values — an unlisted array
or dict field is shared with the target, not copied, so mutating it through
the result is observed through the target too. The target itself is unchanged,
even when it has `var` fields. Update values are ordinary expressions in the
enclosing scope; there is no implicit field scope:

```agl
record Issue
  title: text
  severity: int

program def main() -> unit =
  let issue = Issue(title = "Bug", severity = 2)
  let urgent = issue with severity = 5, title = "Bug!"
  let louder = issue with severity = issue.severity + 1
  print(urgent)
```

The result has the target's static type. Every listed field must exist on that
type, each value must be assignable to the field type (with expected-type
propagation and `int → decimal` coercion), and listing a field twice is a
static error. `with` does not apply to enums (match and reconstruct instead),
dictionaries, arrays, or `json`. The target is evaluated once, then the update
values left to right. Updating an exception through a base-exception-typed
binding preserves its concrete type ([Exceptions](exceptions.md)). For
general copies see [Copying values](types.md#copying-values).

**Binding and chaining.** `with` binds looser than every operator, on both
sides: the target is the whole operator expression before `with`, and the
update list extends to the end of the enclosing expression, so comparing an
updated value needs parentheses — `(r with x = 1) == r2`. Chaining is
left-associative: `r with x = 1 with y = 2` updates the result of the first
update. An update used as an update *value* must be parenthesized:
`r with inner = (r.inner with x = 1)`.

**Restricted positions.** In comma-separated element positions — call
arguments, array and dict elements — and in inline `=>` branch bodies, the
whole update must be parenthesized: `f((r with x = 1), y = 2)`. Suite bodies
have no such restriction.

## Indexing

`expr[index]` reads from an array, dictionary, or text. It is a postfix
operator and chains with calls and field access:

```agl
record Row
  name: text

def make-items() -> array[int] = [7, 8]

program def main() -> unit =
  let xs = [10, 20, 30]
  let metadata = {"source": "reviewer"}
  let matrix = [[1, 2], [3, 4]]
  let rows = [Row(name = "Ada")]
  print(xs[2])                # 30
  print(xs[-1])               # 30
  print(metadata["source"])   # reviewer
  print("Ada"[0])             # A
  print(matrix[0][1])         # 2
  print(rows[0].name)         # Ada
  print(make-items()[0])      # 7
```

Whitespace matters: `xs[0]` is indexing because `[` is adjacent to `xs`, while
`f [0]` is the single-argument call `f([0])`.

Array and text indexes must be `int`; negative indexes count from the end. A
text index returns one Unicode code point. An out-of-range array or text index
raises catchable `IndexError`. Text is immutable, so it cannot be an
indexed-assignment target.

A dictionary index must have (or directedly coerce to, e.g. `int` into a
`decimal` key) the dictionary's key type, which must satisfy `Hashable`. A
missing key raises catchable `KeyError`.

## Calls

`f(args)` calls a declared function, a built-in, a constructor, or any function
value; a placeholder argument (`?`) makes it a partial application instead.
With exactly one positional argument and no named arguments the parentheses
may be dropped (`print review`), and a callee's explicit type arguments carry
over to that sugar: `ask::[int] "How many?"` is `ask::[int]("How many?")`.
Application binds tighter than every operator — `print x + 1` is
`(print x) + 1` — and the sugar does not chain (`print f x` does not parse). See [Calling
functions](functions.md#calling-functions), [Partial
application](functions.md#partial-application), and [Agent
calls](agent-calls.md) for `ask`.

## `print`

`print` takes one argument of any type, writes its rendered value and a newline
to standard output, and returns `()`. It renders with `pretty = false` and
`quote-strings = false` (see [`render`](#render)), so a top-level `text`
prints verbatim:

```agl
record Review
  score: int

program def main() -> unit =
  let review = Review(score = 4)
  print "Review round failed; retrying."
  print review                    # Review(score = 4)
  print(review.score + 1)         # a compound argument needs parentheses
```

`print` raises `CyclicValueError` when its argument contains a reference
cycle. As a function value it needs an expected type or explicit type argument
(`let f: text -> unit = print`, `print::[text]`). An explicit type argument
requires the argument to be assignable to it and prints the coerced value, so
`print::[json]("hi")` prints the json form `"hi"`, not `hi`.

## `render`

`render(value, pretty = true, quote-strings = true)` converts any value to
`text` by the [uniform rendering
rules](strings-and-interpolation.md#uniform-rendering-rules) that
interpolation, `print`, `as text`, and REPL echo share, adjusted by two
parameters. `pretty = true` renders structured values and JSON on indented
lines (a dict key stays on one line); `false` keeps the single-line form.
`quote-strings` affects only a top-level `text` or `json` argument: `false`
renders a `text` verbatim and a `json` as pure JSON; `true` renders value
syntax, quoting the text, or each string in the `json`, as an AgL string
literal.

```agl
program def main() -> unit =
  print(render("hi"))                          # "hi"
  print(render("hi", quote-strings = false))   # hi
  print(render([1, 2], pretty = false))        # [1, 2]
```

`render` raises `CyclicValueError` on a reference cycle. As a function value it
needs an expected type or explicit type argument (`let f: json -> text =
render`). An explicit type argument coerces the argument, so
`render::[json]("hi", quote-strings = false)` renders `"hi"` (a `json` string,
not text), and `render::[json]("50%")` renders `"50\%"`.

Other value built-ins: `copy` and `shallow-copy` ([Copying
values](types.md#copying-values)); `parse` and `try-parse` ([Parsing
values](types.md#parsing-values)).

## `resource` and `resource-dir`

`resource(path)` returns the absolute `path` of an existing packaged or
adjacent resource. Its only argument must be a text literal holding a relative,
forward-slash path with no `..` segment; it cannot be computed or passed
through a binding, so `resource` is not a function value. `resource-dir()`
returns the resource anchor itself.

A loose module's resources are anchored at its directory; a
[package-owned](packages.md) module's at the package root, so they stay stable
when the module is imported from another project. A module with no backing
file has no anchor, so either call there is a static error. Both calls are
[constant expressions](bindings-and-scope.md#constant-expressions) and resolve
before execution; a missing target is a static error, and package checks and
package creation verify each literal target. `resource` only produces a path;
use `std/fs` for filesystem effects.

<!-- agl-check: fragment -->
```agl
let prompt-path = resource("prompts/review.md")
let package-root = resource-dir()
```

## Operators

### Equality: `==` and `!=`

`==` is equality; a single `=` is never a comparison. Both operands must have
the same type, or one `int` and one `decimal`, or one type must widen
nominally to the other (a member record or narrower enum against an enum, a
derived exception against an ancestor), so `opt == None` checks
([Assignability and coercion](types.md#assignability-and-coercion)).
Equality is full value equality, and `int`/`decimal` compare exactly without
widening, so it never raises ([Values and equality](types.md#values-and-equality)).
A type that is, or contains, a function or `unit` has no equality; a type
parameter is comparable only under an `Eq` or `Hashable` bound ([Constraint
blocks](generics.md#constraint-blocks)). Comparisons are non-associative:
`x == y == z` is a parse error.

### Ordering: `<` `<=` `>` `>=`

Both operands must be numeric or both `text`. Numbers order exactly, an `int`
against a `decimal` included, without widening. Text orders lexicographically
by code point.

### Membership: `in`

<!-- agl-check: fragment -->
```agl
issue in issues          # element membership:  issues: array[T]
key in counts            # key membership:      counts: dict[K, V]
"missing" in body        # substring:           both text
```

Element membership requires the element type to satisfy `Eq` (a type
parameter needs an `Eq`/`Hashable` bound). Key membership requires the
`Hashable` key type, and the tested value must have, or directedly coerce to,
that type. An `int` tested against `decimal` elements or keys compares exactly
and never raises ([Numbers](types.md#numbers-int-and-decimal)).

### Arithmetic: `+` `-` `*` `/` and unary `-`

1. Both operands must be numeric. `+ - *` on two `int`s yield `int`; if either
   is `decimal`, the result is `decimal`.
2. `/` **always yields `decimal`**, even for two `int` operands.
3. Division by zero raises `ArithmeticError`, as does a `decimal` result
   outside the decimal range ([Numbers](types.md#numbers-int-and-decimal)).
4. Unary `-` negates an `int` or `decimal`.

Text is concatenated with the prelude operator `++`, not `+`
([Operator precedence](lexical-structure.md#operator-precedence)).

### Boolean: `and`, `or`, `not`

Operands must be `bool`. `and` and `or` short-circuit.

### Casts: `as` and `as?`

<!-- agl-check: fragment -->
```agl
EXPR as T     # convert EXPR to T; raises CastError on failure
EXPR as? T    # Option[T]: Some(converted value) or None; never raises
```

`as?` performs the same conversion as `as` but yields `None` wherever `as`
would raise — a `CastError`, a `CyclicValueError` while converting a cyclic
value to `text` or `json`, or an `ArithmeticError` from `as decimal` on an
out-of-range `int`. Enum, member-record, and exception casts are identity casts
that check nominal identity rather than convert. A recursive record or enum is
an ordinary cast target, including inside a container such as `array[Tree]`;
a type with no finite schema is rejected as a fallible cast target ([The
finite-schema boundary](generics.md#the-finite-schema-boundary)). The full
conversion matrix is in [Casts and
convertibility](types.md#casts-and-convertibility).

Casts are left-associative and bind tighter than every binary operator but
looser than unary `-` and application: `-1 as text` is `(-1) as text`,
`2 * 3 as text` is `2 * (3 as text)`, `f x as int` is `(f x) as int`, and
`x as json as text` is `(x as json) as text`.

```agl
program def main() -> unit =
  let raw: json = 3
  let n: int = raw as int                # raises CastError unless an integral number
  let maybe: Option[int] = raw as? int
  case raw as? int of                    # the optional cast carries its value
    | Some(value) => print value
    | None => print "not an int"
  let s: text = n as json as text        # total
```

### Enum-member and exception tests: `is`, `is not`

```agl
enum Status
  | Ready
  | Blocked(reason: text)

exception Problem
exception Timeout extends Problem

def label(status: Status) -> text = if status is Ready => "ready" else => "blocked"

def is-timeout(e: Problem) -> bool = e is Timeout    # Timeout or a descendant

def wait(status: Status) -> unit =
  if status is not Status::Ready => print "waiting"
```

The left operand must have enum or exception type. For an **enum**, the
right-hand name must be one of that enum's members — its bare injected name,
its record declaration name, or a qualified member spelling — and the test
compares nominal member identity. When one bare spelling exposes members of
several enums, the left operand's enum type selects the member; several
distinct matching members remain ambiguous. For an **exception**, the
right-hand name must be the left operand's static type, an ancestor, or a
descendant (an unrelated type is a static error), and `x is T` holds exactly
when `x as? T` is `Some`. Either name may be qualified, including through a
named scope, as in a [constructor
pattern](pattern-matching.md#module-qualified-constructor-patterns).

`is`/`is not` bind tighter than every infix operator and looser than casts:
`a == e is X` is `a == (e is X)`. The test never narrows the left operand's
static type; cast it to reach the target's fields or methods.

### Operators as function values

A built-in symbolic binary operator alone in parentheses is a function value:
`(+)`, `(-)`, `(*)`, `(/)`, `(==)`, `(!=)`, `(<)`, `(<=)`, `(>)`, and `(>=)`.
It behaves like the lambda `fn(a: L, b: R) => a op b`, with `L` and `R` taken
from context: an expected function type, a higher-order function's parameter,
or the arguments of a direct or partial call.

```agl
program def main() -> unit =
  print([1, 2, 3].fold(0, (+)))         # 6
  print(["a", "b"].map((==)(?, "a")))   # [true, false]
  print([3, 1, 2].sort((-)))            # [1, 2, 3]
  print([1, 5, 9].select((<)(?, 4)))    # [1]
  let ratio: (int, int) -> decimal = (/)
  print(ratio(1, 4))                    # 0.25
```

The operator's typing rules then apply to those operand types, including
`int → decimal` widening; as for a lambda, a concrete expected result type is
adopted when the result is assignable to it
(`let f: (int, int) -> decimal = (*)` is valid). Operand types the context does
not determine are a static error (`let f = (+)`). Both operands arrive already
evaluated. `(-)` is subtraction, not negation. The word operators have no
operator-value form (`(and)` is a parenthesized name); use a lambda. A
user-defined operator is an ordinary function, so `(|>)` names its
declaration.

## Control-flow expressions

`if`, `case`, loops, and `try` are expressions; their types and
parenthesization rules are in [Control flow](control-flow.md) and
[`try`/`catch`](exceptions.md#try-catch).

## Divergent expressions

`raise`, `return`, `break`, and `continue` never yield a value at their
expression site. Their type is the **bottom type**, assignable to any expected
type, so they may appear in any value position:

```agl
program def main(finished: bool, reason: text) -> unit =
  for attempt in 1 to 3 do
    let next: int = if finished => break else => attempt + 1
    if next > 3 => raise Abort(message = reason)
  done
```

See [Exceptions](exceptions.md#raise), [Early return](functions.md#early-return),
and [`break` and `continue`](control-flow.md#break-and-continue).

## Expected-type propagation

An expected type propagates top-down where it helps:

| Context | Propagated expectation |
| ------- | ---------------------- |
| `let x: T = e` / `var x: T = e` | `T` into `e` |
| `x := e` | declared type of `x` into `e` |
| Constructor argument | declared field type |
| `array[T]` / `dict[K, V]` expectation | element type into each element; key/value type into each key/value |
| `case` / `if` expression with outer expectation | into every branch |
| `ask` / typed `exec` | becomes the call's target type |
| Function call | each parameter type into the corresponding argument |
| Lambda with omitted parameter annotations | matching function parameter types into the omissions |
| Function body | `-> RetType` propagated in |

Once scope has resolved an unqualified constructor (`let r: Review = Pass`),
the expectation checks it against the enum type, types empty containers, and
gives agent calls their output contracts; it never selects among same-named
constructors. A target that depends on sibling constraints is settled with the
whole enclosing expression before an agent call's output contract is fixed.
Where no expectation exists, inference is bottom-up and an untyped `ask`
defaults to `text`; so does a generic type parameter that nothing else in the
enclosing expression pins (as in `print <| exec "…"`), once sibling
constraints have settled.
