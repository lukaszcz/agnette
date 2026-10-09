# Functions

[← Index](index.md)

AgL has named `def` declarations and anonymous `fn` expressions (lambdas). Functions are first-class
values: they may be stored in bindings, passed as arguments, and returned from
other functions. The declaration, lambda, and call syntax is specified in
[Grammar](grammar.md#function-declarations).

## `def` — named function declarations

A declaration head may include its [scope](scopes.md) path, as in
`def Text::render(value: text) -> text = value`. An inline body requires `=`;
before an indented suite body the `=` is optional. The body is a single
expression, which may be a block (a sequence of items ending in an
expression):

```agl
def classify(n: int) -> text =
  if
    | n > 0 => "pos"
    | n < 0 => "neg"
    | else  => "zero"

def summarize(doc: text, sentences: int = 3) -> text =
  let prompt = "Summarize in at most %{sentences} sentences:\n%{doc}"
  ask(prompt)

def double(n: int) = n * 2

# Compact one-line block body.
def incremented() -> int = (let x = 0; x + 1)
```

An inline `def` body is a single item. The next block separator — a newline
or `;` — ends it and starts the next block item. Write a multi-item body as a
parenthesized block or as a suite — see
[Inline bodies](grammar.md#inline-bodies).

`def` is valid only at the program root and in named scope regions; a `def`
nested in a block (a loop body, an `if` branch, …) is a static error.

### Return type

The `-> RetType` annotation is optional on ordinary `def` declarations. When
present, the body is checked against it. When omitted, AgL infers the return
type from the body, as it does for lambdas. A monomorphic function infers
through direct recursion, same-module forward references, and mutual
recursion when the group provides concrete evidence:

```agl
def is-even(n: int) = if n == 0 => true else => is-odd(n - 1)
def is-odd(n: int) = if n == 0 => false else => is-even(n - 1)
```

This also applies when a later function is used as a function value or
through partial application. Inference uses only the definitions in that
dependency group, never callers or their expected types. A recursive generic
function infers only when every recursive call passes its type parameters
unchanged and in order ([Recursive generic
functions](generics.md#recursive-generic-functions)). A group with no concrete
result evidence — for example one that always raises or only calls within the
group — needs return type annotations.

### Early return

`return expr` exits the nearest enclosing `def` or `fn` body immediately and
makes `expr` the call's result. A bare `return` is `return ()` and is valid
only when the result type is `unit`.

```agl
def first-positive(xs: array[int]) -> int =
  for x in xs do
    if x > 0 =>
      return x
  done
  -1

def log-and-stop() -> unit =
  print "stopping"
  return
```

A body that does not execute a `return` yields its last expression. With a
return type annotation, each `return` operand is checked against it. Without
one, the inferred result type is the common type of every `return` operand and
the tail value, unified as `if` and `case` branches are (`int` may widen to
`decimal`; divergent branches are ignored); if there is none, annotate the
result.

A `return` inside a lambda returns from that lambda. `return` is a static
error outside a function body, including at the program top level and in
parameter defaults.

Like `raise`, `return` is admitted directly in an inline branch or `catch`
body (`if ready => return x else => 0`) and in a loop body. It is an
`expr`-level form, so in an `or_expr` position — an `until` condition or an
operand — it must be parenthesized. A `return`
followed by a newline is a bare `return`; the operand never continues onto the
next line.

### Built-in functions

`builtin def` declares a function implemented by the host, so it has no body
and its return type annotation is required. `builtin` is a declaration
modifier: it may precede `def` on the same line or on the line directly above
it. The declared name and signature must match a recognized built-in exactly.
The standard library makes these declarations; programs call them through the
default standard-library import.

A call to a built-in name resolves like any other name: the callee must reach
a `builtin def` declaration — bare through the default import or a program's
own declaration, or qualified through its declaring path. A name with no such
declaration in scope is an ordinary undefined-name error.

A built-in's complete scoped name — of a `builtin def` or of a `builtin
record`/`enum`/`exception` — is its host identity and may be declared only
once across the program. A root `ask` therefore coexists with `Agent::ask`,
and a program that loads the default standard library (unless started with
`--no-stdlib`; see [Modules](modules.md#prelude)) cannot redeclare a
standard-library builtin at the same scoped name.

An ordinary function in a named scope, or in a module with a path identity
([Modules](modules.md)), may reuse a built-in function's name because it is
reached through its own qualified namespace. The bare built-in spelling stays
reserved, even after a `use` makes that scope's or module's members bare.
Constructors follow the same rule
([Keywords](lexical-structure.md#keywords)).

```agl
use Codec::*

scope Codec
  def render[T](value: T) -> array[T] = [value]
end Codec

program def main() -> unit =
  let wrapped = Codec::render(1)  # the scoped generic function
  let shown = render(1)           # the built-in render
  print(wrapped)
  print(shown)
```

A `builtin def` method must also match a recognized host method. On a
[builtin receiver](#methods) it must be `print`, `render`, `copy`, or
`shallow-copy` taking only `self`; `copy` and `shallow-copy` return the
receiver's exact type, `print` returns `unit`, and `render` returns `text`.
Calling one performs the corresponding bare built-in with `self` as its
value. On a nominal receiver, the recognized host methods are the standard
library's `Agent::ask`, `Agent::ask-request`, and `Session` methods
([Agent calls](agent-calls.md)), whose receiver supplies the target agent or
session. Host methods follow the ordinary selection, bound-method, and
specialization rules.

The special built-ins `print`, `render`, `copy`, `shallow-copy`, `parse`,
`try-parse`, `resource`, `ask`, `ask-request`, and `exec`, and the `Agent::ask`,
`Agent::ask-request`, and `Session::ask` methods, take their subject
positionally only and every option by name only: `print(value = 1)`,
`exec(command = "ls")`, and `ask("Hi", AgentClaude())` are static errors.
`Session::open` and the other `Session` operations bind arguments like an
ordinary `def`.

Runtime built-ins are first-class values. A value occurrence is contextually
specialized and behaves like an eta-expanded lambda over the declaration's
required parameters; omitted optional parameters keep their defaults:

```agl
program def main() -> unit =
  let emit: text -> unit = print
  let query: text -> int = ask
  let run: text -> ExecResult = exec
  let open-session: Agent -> Session = Session::open
  let current-session: () -> Session = Session::default
  ()
```

A generic built-in value needs an expected function type or explicit type
arguments, except that an unconstrained `ask` value defaults its result to
`text`, like a direct `ask` call, and `ask-request` its output contract to
`text`; `exec` defaults to `ExecResult`. For calls and values alike, an
expectation that is a type parameter nothing else in the enclosing expression
pins counts as unconstrained: `print <| exec "date"` prints an `ExecResult`.
To capture non-default options, wrap a direct call in a lambda. Built-in
methods produce receiver-capturing values, and qualified method or `Session`
static references produce ordinary positional function values. `resource` is call-only because its path must be
a source literal; the nullary `resource-dir` is a function value. `parse` and
`try-parse` are call-only because their target type comes from the call's
explicit type argument or expected type ([Parsing
values](types.md#parsing-values)).

### Externally implemented functions

`extern def` declares a function implemented by a co-located Python companion
file. Like `builtin def`, it has no body and a mandatory return type. A type
parameter that no parameter mentions makes it **type-directed**: each call
site's resolved type reaches the companion. See [Python FFI](ffi.md) for the
declaration rules, the type mapping, and the error model.

### Parameters

Parameters are listed with explicit types. Each parameter belongs to one of
three **zones** that determine how call-site arguments match it:

| Zone | Binding | Written as |
|------|---------|------------|
| **Positional-only** | Positional argument only | `@arg-pos`; a method receiver `self` |
| **Standard** | Positional or named | `@arg-std` |
| **Named-only** | Named argument only (or bare-name shorthand) | `@arg-named` |

A [zone attribute](attributes.md#zone-attributes) in front of a parameter puts
that parameter in its zone; in front of the declaration it sets the zone of
every parameter that does not name one itself. Without one, parameters of a
`def`, `extern def`, or `builtin def` are standard (the special built-ins
above excepted), a method receiver `self` is positional-only, and parameters
of a `program def` are named-only (its CLI projection is described in [Host
environment](host-environment.md#program-arguments)). A lambda parameter
takes no zone attribute and no default (both are static errors): a lambda is
called only as a function value, positionally with every argument.

```agl
def f(@arg-pos x: int, y: int) -> int = x + y                    # x pos-only, y standard
def g(@arg-pos x: int, y: int, @arg-named z: int) -> int = x + y + z  # all three zones
def simple(x: int, y: int) -> int = x + y                        # both standard

@arg-pos
def h(x: int, y: int) -> int = x + y                             # both positional-only
```

Parameters are listed in zone order: positional-only, then standard, then
named-only. A parameter that follows one from a later zone is a static error.

**Defaults.** A parameter default is an `or_expr`, so `if`, `case`, `try`,
loops, `raise`, and `fn` must be parenthesized in a default. No *required*
positional-only or standard parameter may follow a *defaulted* one of those
zones; named-only defaults may appear in any order:

```agl
def greet(name: text, greeting: text = "Hello") -> text =
  "%{greeting}, %{name}!"

def with-named-default(x: int, @arg-named tag: text = "ok") -> text =
  "%{tag}: %{x}"
```

Defaults are evaluated at each call that omits them. A [record field
default](types.md#record-types) follows the same ordering rule and is likewise
omittable at a constructor call.

## Methods

A `def` whose first parameter is `self` and whose enclosing scope resolves to
a receiver type — a record, enum, enum member, exception, or builtin receiver
— is a **method**: a scoped function selected by its receiver's **static type**.
`p.f(x)` supplies `p` as the first argument of the selected function, while
`Type::f(p, x)` calls one function by its ordinary scope path. A method is not
dynamically dispatched.

The receiver part of `def Type::f(self)` (or `scope Type` containing that
`def`) is the type declared at that scope path — the method's whole path
without its own name — read at the path's parent step in the module
containing the declaration ([Names and
visibility](scopes.md#names-and-visibility)). A record, enum, enum member, or
exception the module declares there wins. Otherwise an import tail or `use` in
that scope or an enclosing one must provide exactly one type at that path (a
renamed contribution counts under its new name); two distinct provided types
are ambiguous. There is no outward fallback — inside `scope r`,
`def Geo::m(self)` extends `r::Geo`, never a root `Geo` — and a qualified
import alone provides no bare receiver name. A receiver names its
type directly: a receiver path that selects a type through a type alias, the
module's own or an imported one, is an error. The declaration extends the
resolved type's plain scope in the module that contains the `def`; it does
not add a declaration to the type's home module.

<!-- agl-check: fragment -->
```agl
# geometry.agl
record Point
  x: int
  y: int

def Point::shift(self, amount: int) -> Point = Point(x = self.x + amount, y = self.y)

# metrics.agl
import geometry::*

def Point::norm(self) -> int = self.x * self.x + self.y * self.y

# main.agl
import geometry::*
import metrics

program def main() -> unit =
  let p = Point(x = 2, y = 3)
  print(p.shift(1).norm())
  print(metrics::Point::norm(p))
```

`metrics::Point` in this example is a plain scope of `metrics`. A bare
`Point::norm(p)` follows ordinary scope-path resolution; dot selection is the
operation that gathers methods for a receiver type across modules.

A method is selectable in a module when that module declares it or can reach
its declaration by a qualified import route. Any import form — a plain import,
alias, bare tail, wildcard, or facade re-export — can provide that route;
`hiding` removes it. `use` adds bare names only and does not affect method
visibility. The implicit prelude import is an ordinary route, so the
standard-library methods it re-exports, builtin-receiver ones included, are
selectable wherever it is enabled ([Prelude](modules.md#prelude)).

Method candidates for a name form **one level**, fixed by the receiver's
static type `T`:

- record `T` — `T`'s own methods plus those of every current enum that
  declares or references `T` as a member;
- exception `T` — `T`'s own methods plus every ancestor's in its `extends`
  chain;
- enum `T` — only `T`'s own methods; a method declared on one of `T`'s own
  members is not part of this level;
- builtin receiver — only that builtin type's methods.

Route visibility then decides within the level: exactly one visible method
wins; two or more are a static ambiguity, listing every declaration; none
visible but candidates exist reports that the method exists but is not
visible here; no candidates is no member. A home-module method and an orphan
carry equal weight — neither position in the level nor declaration order
breaks a tie.

Fields and methods are distinct member kinds. A field of the receiver type
(its own or an inherited one) and any visible method in its level are
ambiguous, for a read and for `receiver.name := value` alike; hide the
method's route to reach the field, or call the method by a qualified path. An
enum method named like a member field stays legal to declare: it clashes with
the field only on a member-typed receiver, since an enum-typed receiver has
no fields and simply selects the method. A method declaration itself is
rejected when its resolved owner or an ancestor already has a field of that
name, or when it pairs with a same-module method of the same level (below).

When the selected method belongs to an owning enum `E` rather than to the
member itself, the receiver **widens** to `E` — an identity upcast, so
mutation through a `var` field stays visible through the member binding.
`E`'s type parameters that the member's fields capture come from the
member's own type arguments; any parameter the member doesn't capture must be
solved by the call's arguments, its expected type, or its result, or the call
is rejected with a suggestion to annotate or cast the receiver.

Along the member→owning-enum axis and the exception descendant→ancestor axis,
changing the receiver's static type by an annotation or an identity upcast
may turn a call into an error or back, but never changes *which* function the
call selects. Widening one enum to an overlapping enum is outside this: they
remain distinct nominal types, each with its own methods, even when one's
members are a subset of the other's.

An enum in another module that references a record adds that enum's methods
to the record's level wherever they are reachable by route in the using
module — the same exposure orphan methods already accept. Repair a resulting
ambiguity or field clash by hiding the contributing route, calling the
intended function by a qualified path, or widening the receiver explicitly
with `as`.

A module may not declare two same-named methods whose owners share a level —
a record and an enum that declares or references it, or an exception and one
of its ancestors; the later declaration is rejected. The same pair is legal
across modules and becomes an ambiguity only where both routes are visible at
a call site; repair it with a qualified call, a rename, `hiding` one route, or
an annotation or `as` that narrows or widens the receiver's static type.

A builtin receiver may be named in a method declaration in any module, by an
ordinary, `builtin`, or `extern` definition: `array[E]::name` binds its
receiver element parameter and `dict[K, V]::name` binds its receiver key and
value parameters; `dict[text, V]::name` binds only the value parameter and
applies only to `text`-keyed dicts; `text`, `json`, `int`, `decimal`, and
`bool` are bare receivers. Otherwise a builtin receiver must use its bare
generic form, so `array[int]::name`, `dict[int, V]::name`, and
`dict[text, array[int]]::name` are invalid, and so is a receiver spelled
through an alias of a builtin type. These builtin heads are the only
declaration-path segments that take type arguments, and only for a method:
`def array[E]::f()` and `def Box[int]::f()` are invalid. As with a nominal
generic receiver, `_` may occupy an unused receiver slot; it binds a private
rigid parameter the body cannot name. A `{…}` constraint block may bound the
receiver's key, element, or value parameter; see [Generic
methods](generics.md#generic-methods).

Builtin-receiver methods follow the same visibility, call, bound-method, and
specialization rules as nominal methods.

```agl
record Person
  name: text
  address: text

def Person::with-address(self, address: text) -> Person =
  Person(name = self.name, address = address)

program def main() -> unit =
  let person = Person(name = "Ada", address = "Main Street")
  let by-member = person.with-address("East Road")
  let by-path = Person::with-address(person, "East Road")
  print(by-member.address)
  print(by-path.address)
```

A method may update a `var` field through `self`; `self` need not be a mutable
binding because the assignment updates the record, not the receiver name:

```agl
record Counter
  var value: int

def Counter::add(self, amount: int) -> unit =
  self.value := self.value + amount

program def main() -> unit =
  let counter = Counter(value = 1)
  let alias = counter
  counter.add(2)
  print(alias.value)    # 3
```

`self` must be the first parameter. It has no default and cannot be supplied
by name. Its annotation is optional; when written, it must be exactly the
resolved receiver type with its receiver type parameters. Thus an orphan on
`geometry::Point` may write either `self: Point` or `self: geometry::Point`;
for a generic `Box` receiver, `self: Box[E]` is valid while `self: Box[int]`
is not. Elsewhere `self` is an ordinary identifier and, as an ordinary
parameter, requires an annotation.

On a generic receiver type, the first type parameters of a method bind the
receiver type parameters in positional order. Their names are the method's
choice. `_` may fill any unused receiver slot and may repeat; see
[Generic methods](generics.md#generic-methods) for the complete rule.

```agl
record Box[T]
  value: T

def Box::get[E](self: Box[E]) -> E = self.value
def Box::size[_](self) -> int = 1

program def main() -> unit =
  let box = Box(value = 7)
  print(box.get())
  print(box.size())
```

An inline enum member likewise establishes a record type scope, including the
captured generic parameters of its fields:

```agl
enum Tree[T]
  | Leaf
  | Node(value: T)

def Tree::Node::extract[E](self) -> E = self.value

def Tree::or-default[T](self, fallback: T) -> T =
  case self of
    | Leaf => fallback
    | Node(value) => value

program def main() -> unit =
  print(Node(value = 1).extract())    # 1: Tree::Node's own method
  print(Leaf.or-default(4))           # 4: Tree's method, receiver widened to Tree[int]
```

`Tree::Node::extract` is a method of `Tree::Node[E]` alone. `Tree::or-default`
belongs to `Tree`, `Tree::Node`'s owning enum, so it is also in
`Tree::Node[E]`'s level: selecting it widens the receiver to `Tree[E]`. `Leaf`
captures no type parameter, so calling `or-default` on it leaves `T`
phantom; here the argument's type solves it.

### Bound methods and leading-dot expressions

A method member used without a call is a **bound method**: a function value
that has captured its receiver and has parameters only for the remaining
method parameters. It can be stored, passed, or partially applied like any
other function value:

```agl
record Meter
  value: int

def Meter::add(self, amount: int) -> int = self.value + amount
def apply(value: int, f: int -> int) -> int = f(value)

program def main() -> unit =
  let meter = Meter(value = 4)
  let add = meter.add
  let plus = meter.add(?)
  print(apply(3, add))    # 7
  print(plus(5))          # 9
```

A **leading-dot expression** omits the receiver of a member expression and
produces a unary function that accepts it: `.field` behaves like
`fn(receiver: T) => receiver.field`, and `.method(args)` like
`fn(receiver: T) => receiver.method(args)`. The surrounding expression must
supply a unary function type with a resolved receiver type `T` — a pipeline,
an annotated binding, or a callback parameter type; the result type may be
inferred. An unconstrained `let read = .field` is rejected.

Selection and invocation follow ordinary member rules. A field yields its
value; a method selected without parentheses yields a bound function. Thus,
for `Meter::add(self, amount: int) -> int`, `.add` has type
`Meter -> int -> int`, while `.add(3)` has type `Meter -> int`. A field holding
a function can likewise be selected or called. Explicit method type arguments,
named arguments, and partial-application placeholders are supported.

The complete postfix chain belongs to the function body: `.owner.name`,
`.trim().size()`, and `.items[0]` select or call through the omitted receiver.
Parentheses end that chain; `(meter |> .add)(3)` first selects a bound method,
then calls it. Each nested leading-dot expression introduces its own receiver.
Names in arguments refer to their enclosing bindings, including a method's
`self`. An enum receiver has no fields; narrow it to a member record before
projecting a field:

```agl
enum VerifyResult
  | Complete(reason: text)
  | Incomplete(reason: text)

record Meter
  value: int

def Meter::add(self, amount: int) -> int = self.value + amount

program def main() -> unit =
  let result: VerifyResult = Incomplete("pending")
  let reason = result as VerifyResult::Incomplete |> .reason
  let select-add: Meter -> int -> int = .add
  let add = select-add(Meter(4))
  let numbers = [1, 2, 3]
  let incremented = numbers |> .map(fn(value: int) => value + 1)
  let stringify: (array[int]) -> array[text] = .map::[text](fn(value: int) => "%{value}")
  print(reason)               # pending
  print(add(3))               # 7
  print(incremented)          # [2, 3, 4]
  print(stringify(numbers))   # ["1", "2", "3"]
```

### Scope and forward references

A root `def` may call itself or any other root `def`, including one declared
later; `def`s in the same named scope have the same visibility, so root and
same-scope `def`s may be mutually recursive (`is-even`/`is-odd` under
[Return type](#return-type)). A root or same-scope `def` may likewise read or
write a `let`/`var` binding declared later, in a module with a static root
([Names and visibility](scopes.md#names-and-visibility)).

## `fn` — anonymous functions (lambdas)

`fn` produces a function value. A unary lambda may omit its parameter
parentheses. The return type annotation is optional: when omitted it is
inferred from the body, unless a concrete expected function type checks the
body against its result type.

A parameter annotation may be omitted when the lambda has a matching function
context — a function-typed binding or a higher-order function or method
parameter. Each omitted type comes from the corresponding parameter of that
function type; annotations may be omitted independently:

```agl
program def main() -> unit =
  let double = fn(x: int) => x * 2
  let increment: int -> int = fn x => x + 1
  let add: (int, int) -> int = fn (x, y) => x + y
  let add-left: (int, int) -> int = fn (x: int, y) => x + y
  let greet = fn(name: text) -> text => "Hello, %{name}!"
  let ops: array[int -> int] = [double, increment, fn(x: int) => x * x]
  let six = (fn(x: int) => x + 1)(5)
```

An omitted parameter type without a matching context, or with a context of a
different arity, is a static error. When generic argument inference supplies
the context, concrete sibling arguments are considered before the contextual
lambda. A lambda in juxtaposition position — the right operand of an operator
or the lone argument of a single-argument call — must be parenthesized.

### Lambdas are not self-recursive

A lambda's name (the binding introduced by `let`) is not in scope inside the
lambda body, so lambdas are not self-recursive; express local recursion with a
`def`. Lambda return-type inference stays local because the body never depends
on the lambda's own type.

## Generic functions

A `def` may declare type parameters in square brackets after its name
(`def id[T](x: T) -> T = x`) and bound them with a `{…}` constraint block.
Type parameters, inference, explicit `::[…]` type arguments, generic functions
as values, and strict parametricity are specified in [Generics](generics.md).

## Calling functions

All calls use the same parenthesized syntax, whether the callee is a user
`def`, a constructor, a [built-in function](#built-in-functions) such as `ask`,
`exec`, or `print`, or a function value. The callee may carry explicit type arguments, so
`id::[int](5)` is a typed call. An argument is an expression without a bare
record update, which must be parenthesized ([Record
update](expressions.md#record-update)).

Arguments are matched **positional-greedy**: positional arguments fill
positional-capable (positional-only and standard) slots left to right, in
declaration order. Named arguments use `name = value`, follow every positional
argument, and may appear in any order. Defaulted parameters may be omitted:

```agl
def add(x: int, y: int) -> int = x + y
def f(@arg-pos x: int, y: int) -> int = x + y
def format-msg(message: text, prefix: text = "[INFO]") -> text =
  "%{prefix} %{message}"

program def main() -> unit =
  print(add(3, 4))
  print(add(3, y = 4))
  print(f(1, y = 2))
  print(format-msg("Done."))
  print(format-msg("Done.", prefix = "[WARN]"))
```

Unknown names, duplicates, missing required arguments, extra positional
arguments, and supplying a positional-only parameter by name are static
errors.

**Named-only shorthand.** A bare name `x` in positional position, after every
positional-capable parameter is filled, is the named argument `x = x` when it
lands on a named-only parameter. A non-bare expression there is an error:

```agl
def tag(count: int, @arg-named label: text) -> text = "%{label}: %{count}"

program def main() -> unit =
  let label = "files"
  print(tag(3, label))            # tag(3, label = label)
```

**Named arguments at declared-name sites only.** Named arguments are
available when calling a declared name (`def`, constructor, or built-in). A
function *value* has a purely positional type and is called with positional
arguments only.

**Single-argument sugar.** With exactly one positional argument and no named
arguments, the parentheses may be dropped:

<!-- agl-check: fragment -->
```agl
print review               # print(review)
ask "Hello?"               # ask("Hello?")
print res.stdout           # field-access path as the argument
print classify(x)          # print(classify(x))
f Option::Some(value = 1)  # f(Option::Some(value = 1))
ask::[int] "How many?"     # ask::[int]("How many?")
```

A `$` literal may supply that single argument
([Strings and interpolation](strings-and-interpolation.md#the-literal)).
Application binds tighter than every operator, so `print x + 1` is
`(print x) + 1`, and juxtaposition never chains: `print f x` does not parse
([Operator precedence](lexical-structure.md#operator-precedence)). A symbolic
operator name after a callee is an infix operator, so an operator value passed
as an argument needs the call's parentheses: `f(>>)`.

### Calling function values

A function value is called like any other callee:

```agl
def classify(n: int) -> text = if n < 0 => "negative" else => "non-negative"
def maker[T]() -> T -> T = fn(value: T) => value

program def main() -> unit =
  let g: int -> text = classify
  print(g(7))
  let number = maker()(7)
  print(number)
```

A generic call that produces a function value may take its type arguments from
the arguments of that value call, as `maker()(7)` does: the constraints of one
enclosing expression are solved together. An unconstrained function-producing
call still requires explicit type arguments or an expected function type at
its binding boundary.

## Partial application

A parenthesized call that contains one or more placeholder arguments evaluates
to a new function value instead of calling the callee. A placeholder is the
whole value of a positional argument (`?`, `?1`, `?2`, …) or of a named
argument (`name = ?`). Placeholders work with declared functions,
constructors, function values, and bound methods.

```agl
def add(a: int, b: int) -> int = a + b
def digits(a: int, b: int, c: int) -> int = a * 100 + b * 10 + c

program def main() -> unit =
  let inc: (int) -> int = add(?, 1)
  print(inc(4))                   # 5
  let plus: (int, int) -> int = add
  let plus-two: (int) -> int = plus(?, 2)
  print(plus-two(5))              # 7
  let fill-edges: (int, int) -> int = digits(?, 9, ?)
  print(fill-edges(1, 2))         # 192
  let reordered: (int, int) -> int = digits(?2, 9, ?1)
  print(reordered(1, 2))          # 291
```

The resulting function has one parameter per placeholder, typed as the callee
parameter or constructor field it binds to; its result type is the underlying
call's. With bare `?`, the parameter order is the placeholders' order of
appearance in the argument list, named arguments included. A numbered
placeholder gives its parameter position explicitly. Within one call,
placeholders are all bare or all numbered, and numbered ones use each index
from `?1` through `?n` exactly once: `f(?0)`, `f(?2)`, `f(?1, ?1)`, and
`f(?, ?1)` are rejected.

Named-argument holes bind to the named parameter, including named-only
parameters, and constructors follow the same argument binding rules:

```agl
def shaped(x: int, @arg-named y: int, @arg-named z: int = 0) -> int = x * 100 + y * 10 + z

record Box[T]
  value: T

program def main() -> unit =
  let fill-y: (int) -> int = shaped(3, y = ?, z = 9)
  let fill-x: (int) -> int = shaped(x = ?, y = 4)
  let make-box: (int) -> Box[int] = Box(value = ?)
  print(fill-y(5))                # 359
  print(fill-x(2))                # 240
  print(make-box(8).value)        # 8
```

Non-placeholder arguments, and the callee expression of a function-value call,
are evaluated once, left to right, when the partial application itself is
evaluated; the closure captures those values. Defaults for omitted parameters
are not captured: they are evaluated at each invocation. An exception from a
captured callee or argument is raised when the closure is created; one from
the underlying call, when it is invoked:

```agl
var ticks = 0
var saved = 4

def next-tick() -> int =
  ticks := ticks + 1
  ticks

def add(a: int, b: int) -> int = a + b
def stamped(x: int, suffix: int = next-tick()) -> int = x * 10 + suffix
def fail-created() -> int = raise Abort(message = "created")

program def main() -> unit =
  let use-saved = add(?, saved)
  saved := 100
  print(use-saved(6))             # 10; captured saved = 4

  let stamp: (int) -> int = stamped(?)
  print(stamp(2))                 # 21
  print(stamp(2))                 # 22; the default ran again

  try
    let f = add(?, fail-created())
    print(f(1))
  catch Abort as e =>
    print(e.message)              # created
```

Generic callees infer type arguments jointly from non-placeholder arguments,
placeholder parameter types, and an expected function type, also when the
callee is a function-valued expression; fixed arguments are considered before
an expected shape completes any remaining type arguments. A type argument that
is still unknown, as in `let make = singleton(?)`, needs the `::[…]` form or an
expected function type:

```agl
def id[T](x: T) -> T = x
def singleton[T](x: T) -> array[T] = [x]
def map-one[A, B](f: (A) -> B, xs: array[A]) -> array[B] = [f(xs[0])]

program def main() -> unit =
  let keep-ints: (array[int]) -> array[int] = map-one(id, ?)
  print(keep-ints([5])[0])
  let make-single: (int) -> array[int] = singleton(?)
  print(make-single(7)[0])
  let make-text = singleton::[text](?)
  print(make-text("hi")[0])
```

A placeholder is a partial-application marker only in a parenthesized call; a
standalone `?`, `f(? + 1)`, and the sugar `f ?` do not parse. Free special
built-in calls such as `print(?)` and `ask(?)` reject placeholders —
reference the built-in directly for its defaulted function value instead —
while a receiver method first produces a bound value, so
`reviewer.ask::[text](?)` works. The ordinary argument-binding errors apply
too: too many holes, unknown or duplicate names (`f(missing = ?)`), missing
required arguments, positional arguments for named-only parameters, and named
holes on a function value (`g(x = ?)`).

## Function types

A function value has a positional type — `A -> B`, `(A, B) -> C`, or
`() -> C` — matched exactly; parameter names, zones, and defaults belong only
to the declared name. Function values have opaque rendering, no JSON
encoding, and no equality. See [Function
types](types.md#function-types).

## Recursion and the call-depth limit

Functions may call themselves and each other without restriction at the
language level. The host enforces a **call-depth limit** on active function
calls, mutual recursion included, with a portable default of 256; the host
may select a different limit before execution, and the program cannot change
it. Exceeding it raises the catchable `RecursionError`
([Exceptions](exceptions.md#recursionerror)):

```agl
def fact(n: int) -> int =
  if n <= 1 => 1 else => n * fact(n - 1)

program def main() -> unit =
  print(fact(10))                 # 3628800
  try
    print(fact(10000))
  catch RecursionError as e =>
    print "call depth exceeded %{e.limit}"   # call depth exceeded 256
```

## Complete example

```agl
enum Review
  | Pass
  | Fail(issues: array[text])

let reviewer = AgentClaude("sonnet", "medium")

def summarize-issues(issues: array[text]) -> text =
  "Issues found:\n%{issues}"

def review-artifact(artifact: text) -> Review =
  reviewer.ask("Review this artifact:\n%{artifact}", parse-error-retries = 2)

program def main(spec: text) -> unit =
  let artifact: text = ask "Implement %{spec}"
  case review-artifact(artifact) of
    | Pass => print "Accepted."
    | Fail(issues) => print(summarize-issues(issues))
```
