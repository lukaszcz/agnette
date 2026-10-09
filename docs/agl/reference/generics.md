# Generics

[← Index](index.md)

AgL supports **prenex (rank-1) parametric polymorphism**: declarations may
abstract over types using type parameters, and those parameters are
instantiated at concrete types where the declaration is used. Type parameters
appear only at the top of a declaration — there are no nested or
higher-ranked type quantifiers. Identifier case classifies nothing; examples
follow the standard library's convention of capitalized type and constructor
names.

## Declaring type parameters

`def`, `record`, `enum`, and `type` aliases may declare type parameters in a
bracketed list immediately after the declared name. Each parameter is an
ordinary `name` that is in scope **as a type** throughout the declaration's
body.

```agl
def id[T](x: T) -> T = x

record Box[T]
  value: T

enum Option[T]
  | None
  | Some(value: T)

type Transform[A, B] = A -> B
```

A declaration may have several parameters (`def apply[A, B](…)`,
`enum Outcome[T, E]`), and each must be a distinct name. `_` is accepted only
in a method's receiver slots (see [Generic methods](#generic-methods)). Inside
the body, a type parameter may be used anywhere a type is expected — a field
type, a parameter or result type, a nested `let` annotation, or a lambda's
signature:

```agl
def singleton[T](x: T) -> array[T] =
  let single: array[T] = [x]
  single

def via-lambda[A](x: A) -> A =
  let g: A -> A = fn(y: A) -> A => y
  g(x)
```

A `var` field is mutable at every instantiation; type arguments substitute its
field type but not its mutability.

## Generic methods

For a method on a generic type with `N` type parameters, the method's first
`N` type parameters bind those receiver parameters in positional order. The
names need not match the type declaration's names. A method must provide every
receiver slot; `_` may occupy an unused slot and may repeat. Each `_` slot is a
private rigid binding: it participates in receiver matching but is not readable
in the method body. Type parameters after those slots belong to the method
itself. A builtin receiver instead binds its parameters in the receiver
segment (`array[E]::name`, `dict[K, V]::name`); the method's own list holds
only its own parameters ([Methods](functions.md#methods)).

```agl
record Box[T]
  value: T

enum Outcome[A, B]
  | Ok(value: A)
  | Err(error: B)

def Box::get[E](self) -> E = self.value
def Box::map[E, U](self, f: E -> U) -> Box[U] = Box(value = f(self.value))
def Box::size[_](self) -> int = 1
def Outcome::is-ok[_, _](self) -> bool = self is Ok

program def main() -> unit =
  let box = Box(value = 7)
  let mapped = box.map::[text](fn(value: int) -> text => "v=%{value}")
  let outcome: Outcome[int, text] = Ok(value = 1)
  print(box.get())
  print(box.size())
  print(mapped.value)
  print(outcome.is-ok())
```

The receiver fixes the leading method type parameters: `box.map` has `E`
fixed as `int`, so inference and the `::[…]` suffix apply only to `U`. A bound
method value follows the same rule.

A method's own `{…}` [constraint block](#constraint-blocks) follows all of its
type parameters and may bound receiver-bound and own parameters alike:

```agl
record Box[T]
  value: T

def array[E]::has{Eq E}(self, value: E) -> bool = value in self
def Box::same[E]{Eq E}(self, other: Box[E]) -> bool = self.value == other.value
def Box::holds[E, U]{Eq U}(self, xs: array[U], x: U) -> bool = x in xs

program def main() -> unit =
  let box = Box(value = 3)
  print([1, 2, 3].has(3))
  print(box.same(Box(value = 3)))
  print(box.holds([1, 2, 3], 2))
```

A `def` in a type scope without a `self` receiver is not a method: it declares
its own type parameters and does not capture the type's:

```agl
record Box[T]
  value: T

def Box::build[U](value: U) -> Box[U] = Box(value = value)

program def main() -> unit =
  let box = Box::build("ready")
  print(box.value)
```

## Type application

A generic type is **used** by applying it to type arguments, `Name[arg, …]`,
producing a distinct concrete type for each instantiation. An imported
declaration may be applied through a bare name an import tail or `use`
provides, or through a qualified name such as `lib::Box[int]`. The built-in
`array[T]` and `dict[K, V]` use the same form.

```agl
record Box[T]
  value: T

enum Outcome[T, E]
  | Ok(value: T)
  | Err(error: E)

program def main() -> unit =
  let bi: Box[int] = Box(value = 1)
  let outcome: Outcome[int, text] = Err(error = "missing")
  let nested: Box[Box[int]] = Box(value = Box(value = 7))
  print nested.value.value
```

<!-- agl-check: fragment -->
```agl
import containers::*

def unwrap(box: Box[int]) -> int = box.value
let qualified-box: containers::Box[int] = containers::Box(value = 2)
```

## Inference and explicit type arguments

Type arguments are normally **inferred** from the argument types and the
expected (contextual) type. Each occurrence of a generic function or
constructor is instantiated independently; only the surrounding expression's
argument and result equalities connect occurrences.

- **Arguments first.** Argument evidence fixes an instantiation before an
  expected result type is used, so `let d: decimal = id(1)` instantiates `id`
  at `int` and then widens the `int` result to `decimal`.
- **Divergence is no evidence.** A divergent argument such as `raise …`
  constrains nothing, so `id(raise Abort(message = "x"))` needs result context
  or explicit type arguments.
- **Explicit arguments.** The `::[…]` suffix supplies type arguments, one per
  declared type parameter, in declaration order. On a callee it forms a typed
  call (`id::[int](9)`); without a call it instantiates a function value
  (`id::[int]`).

```agl
def id[T](x: T) -> T = x
def apply[A, B](x: A, f: A -> B) -> B = f(x)

program def main() -> unit =
  print(id(5))
  print(id("hi"))
  let d: decimal = id(1)
  print(id::[int](9))
  let next = apply::[int, int](10, fn(n: int) -> int => n + 1)
  let int-id = id::[int]
  print(int-id(next) + d)
```

### Generic functions as values

A generic `def` or field-bearing constructor used as a first-class value needs
expression-local evidence that fixes its instantiation: an expected function
type, explicit type arguments, or the other arguments and result of an
enclosing higher-order call — including a later sibling argument:

```agl
def id[T](x: T) -> T = x
def apply[T](f: T -> T, value: T) -> T = f(value)

record Box[T]
  value: T

def build[T](factory: (T) -> Box[T], value: T) -> Box[T] = factory(value)

program def main() -> unit =
  let f: text -> text = id         # expected function type
  let n = apply(id, 42)            # fixed by the sibling argument
  let made = build(Box, 7)         # generic constructor as a value
  print(f("via value"))
  print(n + made.value)
```

A binding is an inference boundary: `let f = id` is a static error even if a
later expression calls `f`, because that use cannot specialize an
already-bound value. [Partial application](functions.md#partial-application)
infers from its fixed arguments, placeholder positions, and an expected
function type in the same way.

## Constructor type arguments

The constructors of a generic record or enum are generic too, and are inferred
like functions: from constructor arguments, the expected type, or the
surrounding expression. A fieldless member infers its enum's type arguments
from the expected type:

```agl
enum Option[T]
  | None
  | Some(value: T)

enum Outcome[T, E]
  | Ok(value: T)
  | Err(error: E)

program def main() -> unit =
  let empty: Option[int] = None              # T from the annotation
  let some = Some::[int](value = 8)          # T explicit
  let owned = Option[int]::Some(value = 13)  # owner-applied
  let none = Option[int]::None
  let ok: Outcome[int, text] = Ok::[int, text](value = 1)
  let make = Some::[text]                    # a function value
  print(make("ready"))
```

Explicit constructor type arguments take one of three forms:

- `Member::[…]` on an inline enum member accepts either the type parameters
  that member captures or the owning enum's full parameter list, substituted
  through the member's captured parameters: in `Outcome[T, E]`, `Ok` captures
  only `T`, so both `Ok::[int]` and `Ok::[int, text]` construct `Ok[int]`.
- `Enum[…]::Member` applies the owning enum first, under the [segment
  type-argument rule](lexical-structure.md#qualifier-chains)
  (`Option[int]::Some`).
- `Record::[…]` on a standalone record accepts only the record's own type
  parameters; a scoped one takes them after its full path (`Geo::Pair::[int]`).
  A referenced enum member is applied through its own record, never through an
  enum owner.

A field-bearing member with a required field yields a function value; a
fieldless or all-defaulted one yields the constructed value, which is not
callable ([Fieldless and all-defaulted constructor
references](expressions.md#fieldless-and-all-defaulted-constructor-references)).

## Strict parametricity

A value whose static type is a **bare type parameter** `T` is **opaque**: the
generic body may only **pass it, return it, and store it**. Every operation
that would inspect it is a static error:

- `==`, `!=`, and `in` — unless a [constraint block](#constraint-blocks)
  bounds `T` `Eq` or `Hashable`; ordering (`<`, `<=`, `>`, `>=`) never lifts,
- arithmetic,
- field access (`x.foo`) or indexing (`x[0]`),
- `is` / `is not`.

<!-- agl-check: error -->
```agl
def bad[T](x: T) -> bool = x == x     # static error: '==' needs 'Eq' for 'T'
```

A generic definition therefore behaves uniformly at every instantiation. A
structure built around `T` — a `Box[T]` or `array[T]` — supports every
operation except those needing `Eq`/`Hashable` on the nested `T`, such as `==`
or `in` over `array[T]`.

This guarantee does not extend to a Python companion behind `extern def`,
which receives the ordinary runtime representation and is trusted to preserve
parametricity ([Python FFI](ffi.md#generics-and-trust)). An extern type
parameter that no parameter mentions reaches its companion as a contract, so
its instantiation may not be a type variable ([Target type
parameters](ffi.md#target-type-parameters)).

### Constraint blocks

A generic function declaration — `def`, `extern def`, `builtin def`, or a
method, including a builtin-receiver method — may follow its type parameters
with a `{…}` constraint block naming one or more of them `Eq` or `Hashable`.
Type declarations (`record`, `enum`, `exception`, `type`) carry no bounds, and
`program def` cannot declare type parameters. A bound belongs to the operation
that needs it, never to the type that carries the parameter.

```agl
def same[T]{Eq T}(a: T, b: T) -> bool = a == b
def member[T]{Hashable T}(x: T, xs: array[T]) -> bool = x in xs
```

`Hashable` implies `Eq`. A bound lifts the strict-parametricity restriction
on `==`/`!=` and `in` for that parameter — and for any type that mentions it,
such as `array[T]` or `Option[T]` — throughout the declaration's body,
including a method's receiver parameters. A `Hashable` bound additionally
lifts the dict [hashing operations](types.md#arrayt-and-dictk-v) on a
`dict[T, V]`. A constraint names only a type parameter in scope from the
declaration or its receiver; naming one twice, or both `Eq` and `Hashable`,
is a static error, as is a block on a declaration with no type parameters.

`Eq` is exactly the equality [Values and
equality](types.md#values-and-equality) defines for concrete data; an opaque
`Session` satisfies neither kind. An exception ancestor is disqualified when a
descendant adds a non-qualifying field, since an ancestor-typed value may hold
that descendant. `Hashable` is narrower — deeply immutable data: as `Eq`, but
no field may be declared `var` at any depth, and an `array` or `dict` never
qualifies. A generic nominal type satisfies either kind only where its type
arguments do, at the parameter positions its fields use.

A bound is checked wherever its parameter is **instantiated** — an inferred
call, an explicit `::[…]`, a method's receiver type argument, a function value
fixed by its expected type, a partial application, or a bound method value —
against the concrete type supplied there or, when that is the caller's own
type parameter, against the caller's bound:

```agl
def same[T]{Eq T}(a: T, b: T) -> bool = a == b
def relay[T]{Eq T}(a: T, b: T) -> bool = same(a, b)   # relay's 'Eq T' covers the call
```

An unbounded caller parameter never satisfies a bound. A caller's `Hashable`
satisfies a callee's `Eq`; the reverse does not.

## Invariance

Type arguments are **invariant**: an applied type matches another only when
its type arguments match **exactly**. No variance or subtyping applies through
a type argument, and neither does the `int` → `decimal` widening. Arrays and
dicts are shared mutable references, so accepting an `array[int]` as an
`array[decimal]` would let the callee store a fraction into the caller's
integer array:

<!-- agl-check: error -->
```agl
def halve-first(xs: array[decimal]) -> unit =
  xs[0] := xs[0] / 2

program def main() -> unit =
  let counts: array[int] = [3, 4]
  halve-first(counts)   # static error: expected 'array[decimal]', got 'array[int]'
```

Annotating the value at the type it is used as works, because each `int`
element widens as the literal is built: `let amounts: array[decimal] = [3, 4]`.
Likewise `Box[int]` and `Box[decimal]` are unrelated types, and `array[int]` is
not assignable to `array[json]` (use `as json`).

## Recursive generic functions

A generic function with an omitted result annotation can infer through direct
or mutual recursion when each recursive call uses the caller's type parameters
unchanged and in declaration order:

```agl
def last-of[T](values: array[T], index: int) =
  if index + 1 < values.size() => last-of(values, index + 1) else => values[index]
```

A recursive call at a different instantiation — growing `T` to `array[T]`,
replacing it with a fixed type, or permuting several parameters — is
polymorphic recursion and requires an explicit result annotation.

## Recursive generic types

A generic record or enum may reference itself, directly or through other
declarations, under the [recursive types](types.md#recursive-types) rules. The
self-reference's type argument may be a different type built from the
parameter (polymorphic recursion), combining any parameters, containers, and
other generic declarations; each reference in a mutually recursive group may
recurse at a different argument:

```agl
record Pair[A, B]
  first: A
  second: B

enum Perfect[T]
  | Single(value: T)
  | Succ(next: Perfect[Pair[T, T]])

def depth[T](p: Perfect[T]) -> int =
  case p of
    | Single() => 0
    | Succ(next) => 1 + depth(next)   # recurses at Perfect[Pair[T, T]]

program def main() -> unit =
  let level0: Perfect[int] = Single(value = 1)
  let level1: Perfect[int] = Succ(next = Single(value = Pair(first = 1, second = 2)))
  print(depth(level0) + depth(level1))   # 1
```

Each `Succ` layer doubles the payload type. [Inhabitation](types.md#inhabitation)
applies as for any recursive type: `Single` is the base case that makes
`Perfect[T]` constructible for every `T`. Values are constructed, matched,
compared, and folded like those of any other recursive type; `depth`, with its
annotated result, recurses at each new instantiation.

## The finite-schema boundary

Four positions require the instantiations a type reaches to close finitely: a
JSON-decoded `ask`/`exec` response type ([Agent calls](agent-calls.md)), the
target of a fallible `as` or `as?` cast
([Expressions](expressions.md#casts-as-and-as)), a non-`text` `program def`
value parameter ([Host environment](host-environment.md#program-arguments)),
and an `extern def` parameter or result ([Python FFI](ffi.md)). The first three
derive a **finite JSON Schema**; the FFI requires the same closure without
deriving one, except for a [target type
parameter](ffi.md#target-type-parameters), which derives one like an `ask`
target.

Uniform recursion (`Tree[T]` referencing `Tree[T]`), permutation (`Swap[B, A]`
referencing `Swap[A, B]`), and argument-constant recursion (`R[int]`
referencing `R[T]`) reach finitely many concrete shapes. A growing reference
like `Perfect[T]`'s reaches infinitely many (`Perfect[int]`,
`Perfect[Pair[int, int]]`, …), so a type reaching it has no finite schema and
is rejected at that use site:

<!-- agl-check: error -->
```agl
record Pair[A, B]
  first: A
  second: B

enum Perfect[T]
  | Single(value: T)
  | Succ(next: Perfect[Pair[T, T]])

program def main() -> unit =
  let bad: Perfect[int] = AgentPi().ask("Give me a value.")
  # static error: 'Perfect[int]' cannot be used as an agent output type:
  # its recursive instantiations never close, so it has no finite JSON schema.
```

A cast `source as Perfect[int]` reports the same error for a cast target.
Nothing else about `Perfect[int]` is restricted: it can be constructed,
matched, compared, passed, returned, rendered, and converted **to** `json` (a
finite runtime value always converts).

## No generic agent targets

An `ask` response type becomes a wire-serialized output contract, and a bare
type parameter has no wire representation, so a type parameter (or a type
containing one) may not be an `ask` response type. Instantiate the generic at
a concrete type before the value crosses the agent boundary.

## Erasure

Generics add **no run-time type information**: one generic body, and one
generic constructor, serves every instantiation, and there is no reflection
over a value's type arguments at run time.

## See also

- [Types](types.md) — applied types, type aliases, recursive types.
- [Functions](functions.md) — generic function declarations, partial
  application.
- [Bindings and scope](bindings-and-scope.md#overload-sets-shadowing-and-ambiguity)
  — unqualified member ambiguity, shadowing of constructors.
- [Pattern matching](pattern-matching.md#patterns-on-generic-nominal-types) —
  bare names in patterns, patterns on generic types.
