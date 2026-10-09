# Modules

[← Index](index.md)

AgL programs are composed from file-based modules. An `import` makes another
module's public declarations available through qualified routes. An import tail
or a `use` declaration adds selected declarations to a bare namespace.

## Slash-path identity

A module identity is its slash path: the relative path to its `.agl` file,
without the suffix. For example, `utils/strings.agl` has identity
`utils/strings`. Each segment is an ordinary AgL name, so a kebab-case path such
as `review-tools/main` is a module identity like any other. An entry program that
no [package](packages.md) owns has no path identity; a file inside a package keeps
its package-qualified identity even when it is the entry.

A slash path is written byte-adjacent wherever it appears — in a header, a
qualifier, or a wildcard tail ([Qualifier chains](lexical-structure.md#qualifier-chains)).

A module must resolve to exactly one file across the configured library roots.
No matching file is an error; more than one matching file is also an error.
There is no root-priority shadowing. Wildcard imports select matching modules
from the same global module set.

A module inside a [package](packages.md) has the package name as the first
segment of its path and may import only its own package, the package's
declared dependencies, and the standard library.

## Imports

Syntax: [Import and export declarations](grammar.md#import-and-export-declarations).
An import always contributes the target module's full public qualified surface,
except for paths named by `hiding`. A positive `::` tail controls only its bare
contribution:

- `import m` contributes qualified routes only.
- `import m::*` makes every public member bare.
- `import m::{f, Config::timeout}` makes those paths bare: `f` and
  `Config::timeout`, not `timeout`.

A selected scope path includes its complete public subtree; selecting an enum
includes its inline member records, while a referenced record member keeps its
own declaration and export path. An item rename is additive: it adds a bare
path and leaves the selected source path available. An import alias instead
replaces the module's slash-path routes with one single-segment route; it makes
nothing bare and cannot be combined with a tail.

<!-- agl-check: fragment -->
```agl
import utils/strings
import text/format::{render as format}
import app/vocabulary::* hiding internal-word
import company/tools/config as settings
```

**Hiding.** `hiding` may accompany a plain import or a `::*` tail, not a
positive selection. It removes declarations, not spellings: the import brings
in each hidden declaration, and everything beneath it, under no spelling at
all — qualified or bare, through its owner, or through any type alias,
the imported module's own included. After `import m::* hiding Color::Red` and
`type C = Color`, none of `m::Color::Red`, `Color::Red`, or `C::Red` reaches
that member, in any position, and `hiding Option::Some` leaves no injected
bare `Some`. Hiding a scope path removes everything beneath it. Hiding an
alias removes the type it denotes and everything read beneath the alias: with
`type HGeo = Base` and `def Base::j()` in `hid`, `import hid::* hiding HGeo`
leaves neither `Base` nor `Base::j`, while an alias of a different type
survives (`hiding Box` keeps `type IntBox = Box[int]`). Only that import is
affected: another import of the same declaration, or the module's own
declarations, still reach it.

**Items.** Selected and hidden items must name public declarations of every
module matched by a wildcard. An item may be written through a type alias the
module exports, and then names what the module exports at the same path
beneath the alias's target: when `al` declares `type Geo = Base` and exports
`Base::Inner`, `import al::* hiding Geo::Inner` hides `Base::Inner`, and
`import al::{Geo::Inner}` makes `Geo::Inner` bare. An item naming an enum, or
an alias of one, also injects the enum's member names, as does the importing
module's own alias of an imported enum; through an alias a bare member is the
generic member declaration ([Type aliases](types.md#type-aliases)). An
injected member stays an imported declaration and does not claim its spelling
over another import: with `enum Color = Red | Green` in `a` and
`enum Light = Red | Amber` in `b`, a module with `import a`, `import b::*`,
and `type C = a::Color` finds bare `Red` ambiguous; `C::Red` and `Light::Red`
select each.

Repeated imports of a module combine their qualified routes and bare
contributions.

**Whole-path lookup.** Every spelling, bare or qualified, is looked up by its
whole path across the module's own declarations, its imports, and its `use`
declarations together, by the rule in [Names and
visibility](scopes.md#names-and-visibility). A qualified route may also match
several modules; a longer suffix, an anchored path, or an alias selects one.

## `use` declarations

A `use` declaration selects public members of an already-nameable named scope
or module root. Its target can be a local scope, a scope or module root reached
through an import route, or a scope anchored at the current module root
([syntax](grammar.md#import-and-export-declarations)). An unanchored target
with no `::` scope suffix that names an imported module targets that module's
root; the module must already be imported:

<!-- agl-check: fragment -->
```agl
import library
use library::*
use library as Alias
```

`use Scope::*` contributes every member of `Scope` bare; a braced or
single-atom tail selects members, and item renames add renamed bare paths.
`use T::member as N` is a single-item rename when the path ends at a member or
names a type, and a whole-target alias when it names a scope:

- A single-item rename of a type — `use e::Color as C`, or `use Point as P` for
  a type the module declares or makes bare — names the type, its constructor
  where it has one, and every path beneath it, exactly like
  `use e::{Color as C}` (`C` is a type, `C::Red` its member); the original
  spelling stays available. Such a rename is a type alias of the module's own:
  nothing may be declared beneath the new name
  ([Type aliases](types.md#type-aliases)).
- `use Scope as Alias` and `use library as Alias` contribute every member
  beneath `Alias`.

A renamed spelling is another name for the same declaration, so reaching a
declaration through a rename and directly is no ambiguity. `hiding` is valid
only with a `::*` tail and works as an import's `hiding` does. A `use`
contributes names only to its enclosing module or named scope region. It does
not make a module available, and its contributions are not exported: after
`use S::*` in `m`, an importer's `import m::*` reaches `S::f`, never bare `f`.

```agl
use Math::*

scope Math
  def add(left: int, right: int) -> int = left + right
end Math

program def main() -> unit =
  print(add(1, 2))
```

An explicit `/` anchors a module route, and a leading `::` anchors a local
scope path. An unanchored target follows [whole-path lookup](#imports), so all
scopes of that path combine: `use X::*`, where the module declares `scope X`
and an import also provides a scope `X`, exposes every member either declares,
each member path resolved by the same rule. A target written through a type
alias reaches what the same path through its target reaches where the alias is
declared (`use Geo::Sub::*` wherever `Geo::Sub::t()` reads). A named scope
exposed by an earlier `use` is already bare and can be the target of a later
`use`; a `use` reads only the uses written before it. Selection, renaming, and
hiding determine which nested scope paths the later declaration can target.

<!-- agl-check: fragment -->
```agl
import geo/shapes
use geo/shapes::Point::{distance as point-distance}
use /geo/shapes::Point::* hiding internal-distance

import library
use library::Outer::{Inner as Selected}
use Selected::*
```

## Imports and `use` inside a scope region

`import`, `use`, and `export` are header items inside a named scope region.
They precede the region's other items. A scoped import's qualified route is
available throughout its module, while a tail's bare names belong only to that
region and its nested regions. A scoped `use` likewise contributes only to its
enclosing region and nested regions; neither joins the region's qualified
surface, so a name a region's `use` or import tail provides is not reachable as
a member of that region from outside it.

<!-- agl-check: fragment -->
```agl
scope Vec
  import geom/planar::{Point}
  use Point::*
  def norm(p: Point) -> decimal = magnitude(p)
end Vec
```

A scoped export re-roots every forwarded atom under the region's own scope
path. An importer reaches it through that path.

## Wildcards

`import prefix/*` expands to one import per module whose slash path is `prefix`
or starts with `prefix/`. Its alias, tail, and hiding clause apply independently
to every matched module. An alias on a wildcard is a shared alias facade, not a
path rewrite.

<!-- agl-check: fragment -->
```agl
import tools/*::*
import domain/*::{render}
import codecs/* as codec
```

## Suffix and anchored references

A module qualifier ends in `::`:

<!-- agl-check: fragment -->
```agl
import company/tools/config
import service/config as settings

company/tools/config::timeout
config::timeout
settings::timeout
/company/tools/config::timeout
```

A non-aliased imported path may be named by any trailing sequence of its path
segments; a route matching several modules resolves by [whole-path
lookup](#imports). A leading `/` anchors a qualifier to the complete module
path and never matches an alias.

`::name` refers to a declaration in the current module root and bypasses a
lexical shadow, every enclosing scope, and every import and `use` — the
prelude included, so `::getenv` names nothing unless the module declares
`getenv`. The same form works for `::Type` and `::Type::Variant`.
Qualified type references follow the same routing rules and preserve
module-and-scope nominal identity.

A module route reads only that module's exports. The importing module's own
declarations never join it: after `import base::*` and an own
`def Base::mine()` written beneath `base`'s type `Base`, `Base::mine` reaches
the method, while `base::Base::mine` names nothing. When a leading segment
names both an own scope or type and an import route, the own declaration at
the full path wins and `/route::name` reaches the module.

### Module-qualified enum members

A **module qualifier** — an import route such as `mylib::` or an import alias
`L::`, or the current-module anchor `::` — followed by one terminal name
selects a constructor from that module's root surface:

1. a record or exception (or an alias of one) the module declares at its root
   under that name; otherwise
2. the one inline enum member of that name among the enums reachable at the
   module root under any name: those it declares, those its own type aliases
   name, and, through an import route, those it re-exports.

Only a declaration with a constructor claims the name: beside
`enum Color = Red | Green`, a root `enum Red`, `enum Red[T]`, or
`type Red = int` leaves `mylib::Red` selecting `Color::Red`. Two candidate
inline members make the spelling ambiguous in every position; qualify it with
the owning enum (`mylib::Color::Red`). For example, after
`export base::{Color}` in `mid`, `mid::Red` selects `base`'s `Color::Red`; when
`mylib` declares `type C = other::Color`, `mylib::Red` selects
`other::Color::Red`.

```agl
enum Color
  | Red
  | Green

def warm(c: Color) -> bool =
  case c of
    | ::Red => true
    | ::Green => false

program def main() -> unit =
  let c: Color = ::Green
  print(warm(::Red))
  print(c is ::Green)
  print(::Some(1))
```

Further rules:

- `::` reaches an imported or prelude constructor only through an alias the
  current module declares, with one fallback: a builtin enum's member when the
  module's root injects none, so `::Some` is `Option::Some` unless the module
  declares `Some`.
- The spelling denotes the same constructor in a value, a pattern, and an `is`
  test, except that a same-named `def` or `let` of the module claims the value
  spelling (see [claiming a constructor's
  spelling](bindings-and-scope.md#overload-sets-shadowing-and-ambiguity)).
- [Whole-path lookup](#imports) comes first: an own `scope mylib` declaring
  `Red` makes `mylib::Red` that declaration; `/mylib::Red` reaches the module.
- A referenced (non-inline) member keeps its own declaration path: when
  `mylib`'s `Color` references `Shared` from `other`, write `other::Shared` or
  bare `Shared`, never `mylib::Shared` (or `::Shared` inside `mylib`).
- `use mylib as L` is not a module qualifier: it contributes `L::Color::Red`
  but no `L::Red`.

## Re-exports and visibility

`def`, `record`, `enum`, `exception`, and `type` declarations, plus simple
`let`/`var` bindings, are exported under their full declaration paths. An
unannotated binding's exported type is the one inferred from its initializer,
exactly as an importer would see for an annotated binding's declared type —
except an unannotated `var` whose inferred type is too narrow to reassign, which
is rejected instead (see [Typing of
bindings](bindings-and-scope.md#typing-of-bindings)). Grouping helpers in a
[named scope](scopes.md) keeps them off a module's bare surface: an importer
reaches such a member through its scope path or makes it bare with an import
tail or `use` declaration.

An exported `var` is writable across a module boundary the same way it is
read: a qualified target, or a bare target reached through an import tail or
`use`. An exported `let` is immutable at every reference site, including
one reached through a re-export.

`export` forwards public declarations without injecting them into the exporting
module's local scope. A brace tail selects the declarations to forward; a plain
export forwards the complete public surface, and `hiding` removes paths from
that surface. Renames in a brace tail change the forwarded path. Methods are
ordinary declaration paths for these operations: a facade may write
`export metrics::{Point::norm}`, and an importer can call that re-export by its
qualified route, such as `facade::Point::norm(point)`. Separately, the import
makes the declaration route visible for dot selection, so `point.norm()` is
available. Selection, renaming, and `hiding` therefore control both direct-call
routes and dot-method visibility.

<!-- agl-check: fragment -->
```agl
export math/basic::{add, multiply as mul}
export math/advanced hiding internal-helper
export math/*
```

Export items and export `hiding` follow the import rules: an item may be
written through a type alias and names what the exported module exports at
that path beneath the alias's target, and `export al hiding Base::Inner`
withholds that declaration under every spelling the re-export would bring in,
`al`'s aliases included (`Geo::Inner` with `type Geo = Base`); a path a
`hiding` removes reads as hidden under every spelling, as do the paths beneath
it. Every declaration is exported at its written path.

Re-exports preserve the original defining-module identity. Conflicting exposed
names with different origins are static errors; duplicate paths to the same
origin are allowed. A public name cannot be both an ordinary declaration and a
named scope, including when either side is re-exported. Type declarations keep
their legal same-named, type-owned namespaces.

## Prelude

Every loaded entry and library module, except `std/prelude` itself, receives an
implicit `import std/prelude::*`. Any explicit import declaration whose expansion
includes `std/prelude`, including one inside a named scope region, supplies the
prelude contribution instead. Thus `import std/prelude` and `import std/*` leave
prelude names qualified-only, while `import std/prelude::* hiding ask` makes
every prelude member except `ask` bare.

```agl
import std/prelude::* hiding ask

program def main() -> unit =
  print("ready")
```

The prelude re-exports the receiver scopes declaring builtin-type methods, so
those methods are visible wherever the prelude is, through that ordinary
import route: `import std/prelude::* hiding text::trim` hides `trim` and keeps
the other methods. The `--no-stdlib` option disables the implicit prelude; an
explicit import of `std/prelude`, or of any module exporting a method, still
works under it.

## Standard library modules

The standard library is an ordinary module tree under `std/`, imported,
aliased, re-exported, and hidden exactly like any other module. Two modules
have a language-level role:

- `std/prelude` is the prelude described above. It declares nothing itself;
  it re-exports the declarations the language refers to.
- `std/config` declares the [engine settings](host-environment.md#engine-settings).

Every other `std/*` module carries no special status. Each module's own source
is its reference.

## Library modules and cycles

Every file-backed module has a static root: imports, declarations, and
`let`/`var` bindings are allowed there, while bare expressions and
assignments are not. Root binding initializers must be [constant
expressions](bindings-and-scope.md#constant-expressions). Put executable
workflow code in a `program def` body. Because every initializer is a
constant, declaration order does not matter for a static root's own `let`/`var`
bindings, root or scoped — see [Names and
visibility](scopes.md#names-and-visibility).

Imports, uses, and exports appear before other declarations at a module root and in
every named scope region. A region is one declaration for its enclosing root's
ordering; its own headers are ordered within the region.

Import cycles are valid. Functions and nominal types may refer to public
declarations across an import cycle, and every module in a cycle sees the same
exports, re-exports included, as it would outside the cycle.

## REPL

An entry resolves like a module in source order, except that its imports are
hoisted: an import may follow a declaration of the same entry, and the whole
entry sees it. How a session is split into entries never changes what a
spelling selects.

REPL imports and `use` declarations persist after a successful entry, retained
as written: a retained wildcard expands again on every later entry, so it picks
up modules added since, and a retained `use` reads its target afresh in every
later entry, seeing only the uses written before it. A retained `use` whose
target names nothing in a later entry contributes nothing there, and a name it
provided earlier fails where it is read.

A later successful entry replaces the earlier import declaration for every
module it names at the same scope path, whatever its form (`import lib` after
`import lib::*` drops the bare names). A later `use` replaces an earlier `use`
at the same scope path when both have the same form:

- a renaming `use … as N` replaces an earlier renaming use with the same new
  name `N`;
- a wildcard or tail use replaces an earlier wildcard or tail use of the same
  target, as that target resolves now;
- a rename never replaces a wildcard or tail use, nor the reverse.

A failed entry changes neither imports nor uses, and `:reset` clears both with
the session bindings. An explicit `import std/prelude` retained from a
successful entry suppresses the implicit prelude in later entries;
`--no-stdlib` disables it for the whole session.

## Diagnostics

Imports report a missing or ambiguous module path, a selected or hidden name
the module does not declare, or a header placed after a non-header item. A
`use` target must name a local or already imported scope. A qualified spelling
that selects nothing reports, in this order of precedence: a hidden member,
when a `hiding` removed the full path or a prefix of it from a contribution
that otherwise reaches it; else an unknown member, when a prefix of the chain
names something that qualifies (a scope path, type, type alias, or module
route — own or provided); else an unknown qualifier, which includes a chain
through a function, binding, or injected enum member. A `use` target, tail
item, or hidden item is read the same way, whether written through an alias
or not. A bare spelling that selects nothing is an unknown name, hidden or
not. A spelling is ambiguous only when two or more distinct declarations
compete at the step that decides it, never when the module itself declares
the path. These diagnostics identify a direct repair: import the required
module, use a longer suffix or an anchored path, add an alias, or adjust a
tail or hiding clause.
