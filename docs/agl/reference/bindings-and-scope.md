# Bindings and Scope

[← Index](index.md)

AgL has two value binders (`let` and `var`), destructive assignment (`:=`), and
function declarations (`def`). There is no bare assignment: `x = e` as an item
is a syntax error — use `let`/`var` to bind, `:=` to reassign, or `==` to
compare.

## `let` — immutable binding

```ebnf
let_decl   ::= attributes? "let" decl_head (":" type_expr)? "=" init_value
init_value ::= expr | suite
```

`let` binds exactly **one** name; there are no destructuring binders — use
`case` to match or destructure a value. The initializer is evaluated and
checked against the annotation, if any, before the name becomes visible. The
annotation also declares the binding's type; without one, the type is inferred
from the initializer, so a bottom initializer needs an annotation.

The binding is **immutable** and scopes over the **continuation** — the
remaining items of the block and any enclosing continuation that consumes the
block — except at the root of a module with a static root, where every binding
is visible throughout the module regardless of declaration order ([Names and
visibility](scopes.md#names-and-visibility)). A final binder makes its block
`unit`-valued ([The block's value](program-structure.md#the-blocks-value)).

```agl
enum Review
  | Pass
  | Fail(issues: array[text])

program def main(artifact: text) -> unit =
  let review: Review = ask "Review %{artifact}"   # annotation sets the agent's output type
  let count = 3                                   # inferred: int
```

`let` freezes the *name*, never the *data*: a `let`-bound array, dict, or record
with `var` fields can still be updated in place through the name
([Destructive assignment](#destructive-assignment)). Use `copy` for an
independent value ([Copying values](types.md#copying-values)).

`_` is a wildcard binder. `let _ = e` and `var _ = e` evaluate `e` and discard
its value without introducing a readable name; they may be repeated in a scope,
and `_` is never readable, even when an outer scope binds that spelling. An
annotation on `_` is ignored, so the initializer is checked without an expected
type. Use `_` to discard a non-`unit` value intentionally.

### REPL persistence and echo

At the REPL top level, a completed `let` persists its binding across later
entries and echoes the bound name, value, and type. If a later initializer
fails, completed bindings and function closures stay available.

### Binder scope paths

At the module root, `let` and `var` accept an optional scope-path prefix on the
binder (`let A::x = 1`, `var A::count = 0`), declaring the binding at that path
rather than in the module root namespace. The prefix is an ordinary
declaration path, not a module route or a type-argument-applied segment; a
prefix naming a type alias the same module declares is an error ([Type
aliases](types.md#type-aliases)). See [Named scopes](scopes.md#binder-paths)
for binders inside a `scope` region.

A module-root or scope-region binding may be marked `@param` to admit a
host-supplied initial value. It is an ordinary `let` or `var` in all language
semantics; see [Module parameters](host-environment.md#module-parameters).

## `var` — mutable binding

```ebnf
var_decl ::= attributes? "var" decl_head (":" type_expr)? "=" init_value
```

Identical to `let` except that the binding is **mutable**: it may later be
updated with `:=`.

## `:=` — destructive assignment

```ebnf
assign_stmt   ::= assign_target ":=" init_value
assign_target ::= qualifier_chain? name
                | postfix "[" expr "]"
                | postfix "." field_name
```

A bare `name := value` (no index) rebinds the nearest visible **mutable**
binding; it never creates a binding. The right-hand side is checked against the
binding's declared type. The assignment has type `unit` and returns `()`. The
qualifier is the same one a read uses, so a scoped `var`'s path
(`A::count := 1`) is a valid target; see [Named
scopes](scopes.md#names-and-visibility) for the scoped forms. A cross-module
target — qualified, or bare through an import tail or `use` — must be an
exported `var` ([Modules](modules.md#re-exports-and-visibility)) or a
[`builtin var`](#builtin-var-host-backed-bindings); an exported `let` is not
writable.

```agl
program def main() -> unit =
  var attempts: int = 0
  attempts := attempts + 1   # target type: int
  print attempts
```

`:=` also updates an array element or an existing dictionary key:

```agl
program def main() -> unit =
  let xs = [1, 2]
  xs[0] := 10
  var metadata = {"status": "draft"}
  metadata["status"] := "ready"
```

The opening `[` must be adjacent to the target name or the preceding index
(`xs[0]`); `xs [0]` is not an indexed assignment target.

Arrays and dicts are mutable reference values ([Types](types.md)), so an
indexed assignment mutates the container **in place**: every binding, field,
closure capture, or loop cursor referencing it observes the change. The
container may be any array- or dict-typed expression — a `let` binding, a
parameter, a record or exception field, a call result, or a qualified read; mutating
contents is not rebinding, so `let` does not restrict it. Array assignment uses
the negative-index and `IndexError` rules of array access. A dictionary index
has, or coerces to, the key type ([Indexing](expressions.md#indexing)).
Dictionary assignment updates existing keys only — a missing key raises
`KeyError` — and replaces only the value: the stored key keeps its originally
inserted form even when the index is an equal but differently written value:

```agl
program def main() -> unit =
  let price: json = 1.50
  let labels: dict[json, text] = {price: "x"}
  labels[1.5] := "y"
  print(labels)   # {1.50: "y"}
  let counts: dict[decimal, int] = {1.5: 0, 2.0: 0}
  counts[2] := 1  # the int index coerces to the decimal key
```

`receiver.field := value` updates a field declared `var` on a record or
enum-member record. The receiver may be any record-typed postfix expression —
a binding (`let` included), a field, an indexed element, a parameter, or a call
result — and the shared value is mutated in place. Exception fields and fields
without `var` are not assignable. An enum-typed receiver has no fields: narrow
it with a `case` pattern or cast it to its member record first.

```agl
record Counter
  var hits: int

program def main() -> unit =
  let counter = Counter(hits = 0)
  let alias = counter
  counter.hits := counter.hits + 1
  print(alias.hits)   # 1
```

Indexed or field assignment can close a reference cycle; see
[Cycles](types.md#cycles). Binding and assignment never copy; see [`copy` and
`shallow-copy`](types.md#copying-values).

Evaluation order for `target[index] := value` is left to right: the container,
the index, `value`, then the checked store. For a nested target such as
`m["a"]["b"] := v`, the outer container is evaluated and its `"a"` entry read
before `"b"` and `v` are evaluated, so a `KeyError` or `IndexError` while
reaching the target aborts before `value` runs, while `value`'s side effects
always precede the store's own check. For `receiver.field := value`: the
receiver, `value`, then the store.

Static rules, all checked before execution:

1. Redeclaring a name in the same scope is an error.
2. Assigning to an undeclared name is an error.
3. A bare `name := value` to an immutable binding — `let`, a function
   parameter, a catch binder, or a pattern binder — is an error.
4. A field-assignment target must be a `var` field of a record or enum-member
   record.
5. Reading a name that is not visible in the current scope chain is an error.
6. The built-in call names cannot be binder or parameter names
   ([Keywords](lexical-structure.md#keywords)).

## Suite initializers

Every binder and `:=` initializer takes the same two shapes a function body
does: one expression, or an indented **suite** whose value is its last item.
Names bound inside the suite are local to it.

```agl
program def main(threshold: int) -> unit =
  let verdict =
    let doubled = threshold * 2
    let limit = doubled + 1
    if threshold > limit => "over" else => "under"

  var budget: int =
    let base = threshold
    base * 3

  budget :=
    let spent = budget - 1
    spent * 2

  print verdict
  print budget
```

A `:=` that ends its line is otherwise a [line
continuation](lexical-structure.md#layout-indentation-newlines-continuation);
the suite form wins when the next line is indented further.

## `def` — function declarations

`def` is a static declaration valid at the module root and in [named scope
regions](scopes.md); a `def` inside a nested block is a static error. Its head
may include a scope path (`def Text::render(value: text) -> text = value`). It
introduces an immutable function binding; root and same-scope `def`s may refer
to one another regardless of order, so they may be mutually recursive. See
[Functions](functions.md) for declarations, parameters, and recursion, and
[Generics](generics.md) for type parameters.

## Typing of bindings

- With an annotation, the initializer is checked against the annotated type
  (`int → decimal` widening applies; see [Types](types.md)).
- Without an annotation, the type is inferred from the initializer. **An
  untyped `ask` defaults to `text`; an untyped `exec` defaults to
  `ExecResult`**:

  ```agl
  program def main() -> unit =
    let x = ask "A"
    let res = exec "ls"
  ```

- A module-level `var` of a module with a static root ([Library modules and
  cycles](modules.md#library-modules-and-cycles)) needs an annotation when its
  initializer would infer a single enum member: `var v = Some(1)` infers
  `Option::Some`, so no later write could hold `None`, and it is rejected;
  `var v: Option[int] = Some(1)` is fine. A module-level `let` is never
  reassigned and has no such restriction.
- A binding of a function value has a function type:

  ```agl
  def classify(n: int) -> text = if n > 0 => "positive" else => "non-positive"

  program def main() -> unit =
    let double = fn(x: int) => x * 2   # double: int -> int
    let f: int -> text = classify      # explicit function type annotation
  ```

## Constant expressions

Several positions accept only a **constant expression**: a root `let`/`var`
initializer in a module with a [static
root](modules.md#library-modules-and-cycles), a `builtin var` default, an
[attribute argument](attributes.md#constant-arguments), and a
[`@config`](attributes.md#config) value. A constant expression is one of:

- a literal, a literal container, or a constructor application;
- a [`resource`/`resource-dir`](expressions.md#resource-and-resource-dir) call;
- a unary operator over a constant expression (`-1`, `not true`);
- a [template](strings-and-interpolation.md) whose holes are all constant
  expressions;
- a reference to a constant `let` or `var` declared at the root, or in a named
  scope region, of the **same module**.

Other reads, calls other than constructors and resource lookups, binary
operators, and `%${NAME}`, `%!{…}`, and `%!${NAME}` holes are not constant.

A constant reference resolves by the ordinary [scope
rules](scopes.md#names-and-visibility) and may name a binding declared further
down the file; a binding defined in terms of itself, directly or through
another binding, is a static error.

A binding a host can override — a [module
parameter](attributes.md#module-parameters) or an [engine
setting](host-environment.md#engine-settings) — is a constant too. Reading one
at run time yields the value the host settled on, while a position the host
evaluates before any binding initializes, such as a `builtin var` default or a
`@config` value, uses the declared initializer.

## `builtin var` — host-backed bindings

```ebnf
builtin_var_def ::= attributes? "builtin" NEWLINE? "var" name ":" type_expr ["=" expr]
```

A `builtin var` declares a body-less, host-backed, **mutable** binding with a
mandatory type and an optional default, which must be a [constant
expression](#constant-expressions) of that type. The `builtin` marker may be on
the same line as `var` or on the line directly above it. A declaration may
appear only at the root, or in a named scope region, of a module whose path
identity lies under `std` — whether that module is the entry program or an
import; elsewhere it is a static error. Its host identity is its defining
module, scope path, and name, so equal names in different scopes or modules are
independent.

A host seed, when supplied, is the initial value; otherwise the declared
default is. Reading a binding with neither raises an error. Reads and writes
name the binding exactly as for an exported `var` (a qualifier always works; an
import tail or `use` may contribute a bare name), and a write takes effect from
its program point onward, like any `var` mutation. The root `std/config`
bindings are the program's [engine settings](host-environment.md#engine-settings);
other standard-library modules use `builtin var` for their own ambient host
values.

## Names, namespaces, and constructors

Identifier capitalization carries **no** meaning: a name's case never
determines whether it denotes a type, a value, or a constructor
([Lexical structure](lexical-structure.md)). What a name denotes is fixed by
how it is declared and the position it appears in.

AgL keeps **two namespaces**: a *type* namespace and a *value* namespace. A
name may exist in both at once without collision. A `record` or `exception`
declaration introduces a type name *and* a same-spelled value binding for its
constructor:

```agl
record Box[T]
  value: T
# 'Box' the type lives in the type namespace;
# 'Box' the constructor lives in the value namespace.
let b: Box[int] = Box(value = 1)
```

An enum, an alias of an enum, and an alias of a structural type have no
constructor of their own (an enum's members are its constructors); neither
does an alias whose target is one of its own type parameters, since a type
parameter shadows any type of the same name (in `type G[Col] = Col`, `Col`
is the parameter). Such a name lives only in the type namespace, at the
module root or in a named scope, so a value of the same spelling — a builtin,
an imported function, an enclosing declaration — stays visible beside it:

```agl
enum render
  | Plain
  | Fancy
def show(style: render) -> text =
  render(1)   # the builtin 'render'
```

Where no value of that spelling is visible, using the name as a value,
qualifying a constructor with it, or calling it is a static type error,
whether the name is declared locally or imported.

A named scope region occupies neither namespace: it only prefixes the names of
the declarations inside it. Beside an own `scope Geo`, an imported type `Geo`
stays visible in type position, and an imported value `Geo` in value position.

### Constructors in the value namespace

Record constructors and injected enum-member constructors are ordinary bindings
in the value namespace. Written directly, a constructor binds its arguments
positional-greedy ([Constructors](expressions.md#constructors)). A constructor
with at least one required field (no `=` default) is also an **ordinary
function value**: it can be stored, passed, and called. Reached through a
variable, it is called **positionally**, in field declaration order, since a
function value has no named parameters:

```agl
record Box[T]
  value: T

def apply[A, B](x: A, f: A -> B) -> B = f(x)

program def main() -> unit =
  let mk: int -> Box[int] = Box   # the constructor as a function value
  print(mk(1).value)              # called positionally
  print(apply(42, mk).value)      # passed to a higher-order function
```

A **generic** constructor used as a value needs expression-local evidence for
its instantiation, exactly like a generic `def` used as a value: an
annotation, or a surrounding higher-order call's other arguments or result. A
bare `let f = Some` is a static error.

A fieldless constructor reference, or one whose every field has a default,
constructs its value immediately instead; use `fn() => Marker` where a
`() -> Marker` function is required ([Fieldless and all-defaulted constructor
references](expressions.md#fieldless-and-all-defaulted-constructor-references)).
See [Generics](generics.md) for constructor typing and inference.

### Overload sets, shadowing, and ambiguity

A spelling is decided at the first [lookup
step](scopes.md#names-and-visibility) that finds anything for it: a
declaration of the module itself at that path wins; otherwise exactly one
distinct declaration provided by the imports and `use` declarations there is
selected, however many routes reach it; two or more distinct provided
declarations are a **static scope ambiguity error**, reported at the
reference. A spelling reached through a rename — a type alias, `import … as`,
`use … as` — is its target, so reaching one declaration both directly and
through a rename, or through two renames, is never an ambiguity ([Type
aliases](types.md#type-aliases) covers aliases of applied types).

Several visible constructors may share an unqualified member name. In ordinary
value position, a bare reference must resolve to exactly one constructor
candidate at the deciding lookup step — two of the module's own, or two
contributed ones when the module declares none, are a **static scope ambiguity
error**. This holds even when an expected enum type contains one of the
candidates: scope resolves the name before that type checks the expression.
**Qualify** the reference: an inline member with its owning enum
(`Holder::Tagged`), a record with its own name (`Saved::Saved`). Enum-member patterns and `is` tests differ:
their scrutinee's static enum type selects among the bare candidates. A
module-qualified member spelling (`mylib::Tagged`, `::Tagged`) selects the same
member in every position, unless a same-named declaration claims its value
spelling as described below
([Modules](modules.md#module-qualified-enum-members)).

```agl
enum Holder[T]
  | Empty
  | Tagged(by: T)

enum Other
  | Tagged(label: text)      # same unqualified name 'Tagged'

let h: Holder[int] = Holder::Tagged(by = 7)   # bare 'Tagged' is ambiguous here
```

A declaration of the module itself claims the spelling over any member an
import injects, and a member of an enum the module declares claims it over an
imported declaration. Otherwise an enum member's bare spelling, an injected
convenience, yields at a lookup step to a record or exception constructor of
that very name that an import or `use` declaration reaches at that step,
whichever module declares each and whichever contributions reach them; the
member stays reachable qualified. Where `a` declares `enum Color = Red | Green`
and `b` declares `record Red`, after `import a`, `import b`, `use a::Color`,
and `use b::Red`, bare `Red` is `b`'s record and `Color::Red` the member.
Yielding applies in value position only: an enum-member pattern or `is` test
selects among the bare candidates by its scrutinee's type
([Constructor patterns](pattern-matching.md#constructor-patterns)).

A **nearer ordinary binding shadows** a constructor or a set of same-named
constructors entirely: an inner `let`, `var`, or function parameter named
`Tagged` hides the outer constructors for the rest of its scope, like any other
shadowing.

```agl
def shadow(Tagged: int) -> int = Tagged * 10   # parameter hides the constructor
```

Whether an ordinary declaration may claim a constructor's spelling **in that
constructor's own scope** depends on whether the constructor stays reachable:

- An inline **enum member** may be claimed. Its name is `Owner::member`, and
  the unqualified spelling is an injected convenience, so the member is
  reachable qualified. The claiming declaration owns expression position,
  while case-pattern constructor lookup stays independent.
- A constructor **declared in another module** may be claimed, since module
  qualification still reaches it. This covers the **prelude** names —
  exception types (`Abort`, `AgentParseError`, …), enum members (`Some`,
  `AgentCommand`, …), and records (`ExecResult`, `AgentRequest`). They are
  conveniences, not reserved words.
- A **record**, **exception**, or **type alias** declared in the *same* module
  may **not** be claimed. Its constructor name is the declaration itself, with
  no qualified spelling to fall back on, so a second declaration of that name
  is a duplicate-declaration error.

<!-- agl-check: error -->
```agl
enum Color
  | Red
  | Blue

let Red = 5                 # allowed — 'Color::Red' still names the member
let ExecResult = 0          # allowed — declared in another module
def AgentCommand(n: int) -> int = n + 1

scope Collision
  record Widget
    x: int

  let Widget = 1              # error: 'Widget' is already declared in this scope
end Collision
```

Use [`::name`](modules.md) to reach the module's own top-level declaration past
any shadowing: it skips every nearer binding and lookup step and reads none of
the module's imports or uses:

```agl
enum Color
  | Red
  | Blue

let Red = 5

program def main() -> unit =
  let Red = "local"
  print(::Red)                # 5: the root 'let', not the local or the member
```

## Lexical scoping

All binding is statically scoped. The program root is a scope, and these
constructs each introduce a nested scope:

1. each loop iteration (a fresh scope per iteration),
2. each `if` branch body,
3. each `case` branch body,
4. a `try` body,
5. each `catch` body,
6. each function call (a fresh call scope with parameters bound).

`let` and `var` bind in the *current* scope only; bindings never escape their
scope. A lambda closes over its definition environment but is not
self-recursive: the name a `let` binds it to is not in scope inside its body
([Functions](functions.md#lambdas-are-not-self-recursive)).

### Shadowing

Inner scopes may **shadow** outer bindings with a new `let` or `var`.
Shadowing is not mutation; the outer binding is unchanged when the inner
scope ends:

```agl
program def main(condition: bool) -> unit =
  let x = "outer"
  if condition =>
    let x = "inner"    # shadows the outer x
    print "uses %{x}"  # inner
  print "uses %{x}"    # outer
```

### Mutation across scopes

`:=` reaches *through* scopes to the nearest visible mutable binding:

```agl
enum Review
  | Pass
  | Fail(issues: array[text])

program def main(spec: text) -> unit =
  var artifact: text = ask "Implement %{spec}"
  let review: Review = ask "Review %{artifact}"
  case review of
    | Fail(issues) => artifact := ask "Fix %{issues} in %{artifact}"
    | Pass => ()
  print artifact
```

### Loop scope

Each loop iteration opens a fresh scope whose bindings the `until` condition
sees; see [Iteration order and scope](control-flow.md#iteration-order-and-scope).

### Pattern and catch variables

Pattern variables and `catch` binders are immutable and scoped to their
branch or handler body. See [Pattern matching](pattern-matching.md) and
[Exceptions](exceptions.md).

### Function parameter scope

Each call opens a fresh scope with the function's parameters bound. A default
is evaluated at each call that omits its argument, but its names resolve in the
function's **definition** scope, never the call site:

```agl
let default-limit: int = 3

def summarize(doc: text, limit: int = default-limit) -> text =
  "[%{limit}] %{doc}"

program def main() -> unit =
  let default-limit = 100      # not seen by the default
  print(summarize("notes"))    # [3] notes
```
