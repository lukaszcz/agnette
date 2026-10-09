# Named Scopes

[← Index](index.md)

A named scope is a namespace inside a module. It groups static declarations
under a `::` path without creating a module or a runtime value. Scopes may be
nested and extended by later declarations.

## Regions and declaration paths

A `scope` region starts at the module root or inside another scope region and
ends with the same path:

```agl
scope Geometry
  record Point
    x: int
    y: int

  def origin() -> Point = ::Geometry::Point(0, 0)

  scope Format
    def label(point: ::Geometry::Point) -> text = "%{point.x},%{point.y}"
  end Format
end Geometry

def Geometry::translate(point: Geometry::Point) -> Geometry::Point =
  Geometry::Point(point.x + 1, point.y)

program def main() -> unit =
  let origin = Geometry::origin()
  print(Geometry::Format::label(Geometry::translate(origin)))
```

The closer is mandatory and repeats the complete header path: `scope A::B`
closes with `end A::B`. A multi-segment region is equivalent to nested
single-segment regions. A region's items may sit in an indented block under
the header, as above, or at the header's own layout level; the closer stands
at the header's column either way.

A scope path is part of a declaration's name: `scope Agent` containing
`def f()` declares `Agent::f`, exactly as `def Agent::f()` does, and
`def Point::norm(self)` beside `record Point` declares `Point::norm`. A region
is not itself an entity: it never hides or stops lookup of another declaration
(a type position skips it), though a binding, `def`, or type may not share its
name at the same path ([below](#names-and-visibility)). Repeating a region,
or mixing a region with declaration paths, therefore extends the same scope:

```agl
scope Text
  def normalize(value: text) -> text = value
end Text

def Text::display(value: text) -> text = "[%{normalize(value)}]"

program def main() -> unit =
  print(Text::display("ready"))
```

For the same reason, scopes of one path combine across modules: a module may
declare more members beneath a scope or type path an import also provides,
and both sets are reachable through the one path:

<!-- agl-check: fragment -->
```agl
# In m.agl
scope Agent
  def f() -> int = 1
end Agent

record Geo
  x: int

# In entry.agl
import m::*

scope Agent
  def g() -> int = 2
end Agent

scope Geo
  def make() -> int = 3
end Geo

def h() -> int = Agent::f() + Agent::g()   # f from m, g declared here
def k(p: Geo) -> int = Geo::make()          # the type Geo from m, Geo::make here
```

The own `scope Geo` region never hides the imported type `Geo`: a type
position looks for a type at that path, and the region declares none.

A region contains nested regions, header `use`, `import`, and `export`
declarations, static declarations (`def`, `program def`, `extern def`,
`record`, `enum`, `exception`, `type`, every `builtin` form), and `let`/`var`
bindings. Bare expressions, `:=` assignments, and infix declarations are not
allowed there.

## Binder paths

A region admits `let` and `var` bindings, typed exactly as at the module root.
A single-name binder may instead carry a plain (unanchored, argument-free)
scope path, relative to its enclosing region, declaring the binding at that
path directly — the same declaration-path shorthand `def` and the type forms
have:

```agl
scope Config
  let retries = 3
  let Limits::timeout = 30     # declares Config::Limits::timeout
end Config

var Config::attempts: int = 0
```

A binding's initializer runs at its position in the module body, in item order
with the rest of the module's initializers, wherever its region falls in the
source text. A region never defers, reorders, or repeats initialization.

## Import and export

`import` and `export` are header items of a region, like `use`, and precede
its other items. A scoped import tail's bare contribution belongs to its own
region; its qualifier route stays available module-wide. A scoped export
re-roots every atom it forwards under the region's own path. See
[Modules](modules.md#imports-and-use-inside-a-scope-region).

A simple (single-name, not `_`) scoped `let`/`var` is exported under its
declaration path exactly like a root one
([Modules](modules.md#re-exports-and-visibility)); any other scoped binding is
private to its module.

## Builtin declarations

A region admits every `builtin` form. A `builtin def`, `record`, `enum`, or
`exception` is identified by its complete scoped path, so a scoped builtin is
distinct from a root one of the same terminal name — `Agent::ask` coexists
with root `ask` ([Builtin type
declarations](types.md#builtin-type-declarations)). A `builtin var` is identified by its defining module as well
as its path ([`builtin
var`](bindings-and-scope.md#builtin-var-host-backed-bindings)). A
`builtin def` whose first parameter is `self` in a type scope is a builtin
method. Builtin declarations are a standard-library authoring form; see
[Built-in functions](functions.md#built-in-functions) and [Record
types](types.md#record-types).

## Names and visibility

Inside `scope S1::S2`, a spelling `p` — bare or qualified — is tried as
`S1::S2::p`, then `S1::p`, then `p`. Each step reads the module's own
declarations at that full path together with what the imports and `use`
declarations written in that scope or an enclosing one (the module root
included) provide at that path. The first step that finds a declaration
decides:

- the module's own declaration at that path wins;
- otherwise the one declaration the imports and uses provide there is
  selected, however many routes reach it;
- two or more distinct provided declarations are an ambiguity, reported where
  the spelling is used.

A nearer step therefore beats an enclosing one even when the nearer
declaration is provided by a `use` and the enclosing one is the module's own:

<!-- agl-check: fragment -->
```agl
import lib

def f() -> int = 2

scope S
  use lib::*

  def g() -> int = f()   # lib::f: step S finds it first
end S
```

Function parameters and function-body `let`/`var` bindings are nearer than
every step and shadow as usual. `::name` reads only the module's own
declarations from the root, bypassing every step, nearer binding, import, and
`use`. Outside a scope, a member is spelled by its exact path; scope paths
never suffix-match, so `Outer::Inner::work` does not make `Inner::work`
available at the root. How an import's routes reach another module's paths is
described in [Modules](modules.md#suffix-and-anchored-references).

A static declaration (`def` or a type) is visible throughout its scope
regardless of textual order, across separate blocks of the same scope too. A
`let` or `var` binding of a module with a static root ([Library modules and
cycles](modules.md#library-modules-and-cycles)) — every file-backed module —
is visible the same way: a `def` may read, and for a `var` write, a binding
declared later in the same region or at the module root.

Without a static root — the REPL, and an inline `-c` program with no
`program def` — a `let` or `var` binding is visible only to references that
follow its declaration, in its own region or elsewhere in the module, as its
root statements execute. A member of an earlier `scope A` block then cannot
see a binding a later `scope A` block introduces. The same holds for a binding
reached through `use`, wherever the `use` is written.

A `let` or `var` name may not repeat a named scope's name at the same scope
path, in either declaration order — the rule a `def` or type name follows too.

A scoped `var` is assigned through its path (`A::count := 1`) or, inside its
region or after a `use`, its bare name — the same forms that read it,
including through an import. `:=` on a scoped `let`, a `def`, or a type is the
immutable-binder error, and a path with no member is an error.

### Type scopes and methods

Every record, enum, and exception establishes a same-named scope. An inline
enum member declares a record in its enum's scope, so `Review::Pass` is an
ordinary scoped record type and constructor; a referenced member stays at its
own declaration path. A `def` whose first parameter is `self` is a method when
its enclosing scope resolves to a record, enum, enum member, or exception; it
is called through a receiver with `.`. A `def` in the same scope without
`self` is a scoped function called by its path. A declaration-path method and
a method in a `scope Type` region declare members of the same type scope:

```agl
record Point
  x: int
  y: int

def Point::shift(self, amount: int) -> Point =
  Point(x = self.x + amount, y = self.y)

scope Point
  def total(self) -> int = self.x + self.y
end Point

program def main() -> unit =
  let point = Point(x = 2, y = 3)
  let shifted = point.shift(4)
  print(shifted.total())
```

A method's receiver is the type declared at its scope path, read by the steps
above: inside `scope r`, `def Geo::m(self)` extends `r::Geo`, never a root
`Geo`. A qualified call `Point::norm(p)` follows scope-path lookup, while
`p.norm()` selects among the visible methods; receiver resolution, builtin
receivers, and selection are described in [Methods](functions.md#methods).

### Injected enum members

An enum member's terminal name is also a bare constructor candidate, injected
wherever its enum is reached:

- **Where.** An enum reached at a lookup step — declared there, imported, made
  bare by a `use`, or named by a type alias — injects its members' terminal
  names at that step. Inside `scope S`, an `enum Color = Red | Green` makes
  `Red` bare within `S` and its nested regions, but declares no `S::Red`.
- **Whose.** An injected member counts as the module's own only when the
  module declares its enum. A member reached through the module's own alias of
  an imported enum competes as an imported declaration.
- **Precedence.** The module's own declaration at that path claims the
  spelling over an injected member, and a member of an own enum claims it over
  an imported declaration. In value position, a member of an imported enum
  yields to a record or exception constructor of that name an import or `use`
  reaches at the same step ([Overload sets, shadowing, and
  ambiguity](bindings-and-scope.md#overload-sets-shadowing-and-ambiguity)).
- **Ambiguity.** Otherwise, in value position the bare name must select exactly
  one constructor candidate; an expected enum type checks the selected
  constructor but never chooses among several. In a pattern or `is` test, the
  scrutinee's static enum type selects the member instead
  ([Constructor patterns](pattern-matching.md#constructor-patterns)).
- **Kind.** An injected name is a constructor, never a type: `Red` is not a
  type annotation.

```agl
scope Paint
  enum Color
    | Red
    | Green

  let Red = 3                  # own declaration claims bare 'Red'

  def warm() -> Color = Color::Red
  def code(c: Color) -> int =
    case c of
      | Red => Red             # pattern: the member; body: the 'let'
      | Green => 0
end Paint

program def main() -> unit =
  print(Paint::code(Paint::warm()))
```

A root record or exception is constructed by its bare name (`Point(...)`); a
scoped one by its full path, or bare after a `use` of its scope. Explicit type
arguments are covered in [Constructor type
arguments](generics.md#constructor-type-arguments).

## Using a scope

`use` contributes selected members of a local scope, or of a scope in an
imported module, as bare names in its enclosing module or scope region. It is
a header declaration:

```agl
use Math::*
use Text::{show as format}

scope Math
  def add(left: int, right: int) -> int = left + right

  scope Metrics
    def scale(value: int) -> int = value * 2
  end Metrics
end Math

scope Text
  def show(value: int) -> text = "value %{value}"
end Text

program def main() -> unit =
  let result = add(1, 2) + Metrics::scale(3)
  print(format(result))
```

A use in a scope region contributes only to that region and its nested
regions, never to the region's qualified surface: after `scope S` writes
`use lib::{Geo}`, `S::Geo` names nothing outside `S`. Unlike a type alias, a
scope rename (`use S as M`) does not forbid declarations beneath `M`: `def
M::g()` declares `M::g`, not `S::g`, and `M::` reaches both it and `S`'s
members. Targets, tails, renames, `hiding`, and how several
scopes of one path combine are described in
[Modules](modules.md#use-declarations).

## REPL

A scoped `let`/`var`, like a scoped `def` or type, persists across REPL
entries by its full path: a later entry may extend a scope, and a later
same-path declaration replaces the earlier one. A duplicate path within one
entry is still an error. `:reset` clears every scoped binding.

A named scope region is retained state, not a replaceable member: once an
entry declares `scope A`, a later entry cannot declare a root `let`, `var`, or
`def` named `A`, and a retained binding or function `A` cannot later be
reopened as `scope A`.

A region is at its written path in the REPL as in a file. Opening a region,
or declaring, beneath a name the session declares as a type alias is rejected
in whichever entry completes the pair. An `import` in a later region of the
same scope path replaces an earlier region import of the same module.
