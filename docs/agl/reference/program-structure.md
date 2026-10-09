# Program Structure

[← Index](index.md)

## Modules and programs

A source file is a **module**. Its root is static: it holds declarations and
[constant](bindings-and-scope.md#constant-expressions) `let`/`var`
initializers. Executable code — bare expressions, assignments, loops, agent
calls — lives in function bodies, and a run starts in a
[`program def`](#program-definitions). Items are separated by newlines or
semicolons. There is no syntactic distinction between *statements* and
*expressions*: every item is an expression with a well-defined type. The
complete syntax is in [Grammar](grammar.md#programs-and-blocks).

The module block is the only block that may be empty. A source file holding no
items — blank, or nothing but comments — is a legal module that declares and
exports nothing; running it still requires a `program def`.

## Program definitions

`program def` declares an **entry point**: a function the host can run.

```agl
@doc("Publish one artifact.")
program def main(
  @doc("Artifact to publish.") @arg-pos artifact: text,
  @doc("Target environment.") @opt-short("t") target: text = "staging",
) -> unit =
  print "%{artifact} -> %{target}"
```

Rules:

- The result is `unit`, written or inferred; any other result is a static error.
- It declares no type parameters and cannot be `builtin`, `extern`, or a
  method (no `self` receiver).
- It is legal at the module root or as a member of a named scope region, never
  in a nested block.
- It is addressed by its declaration path: `main`, `review::main`, or, in a
  package, `review-tools/review::main` ([Packages](packages.md#programs-and-commands)).
- Otherwise it is an ordinary function: callable from any code, first-class,
  and subject to the usual [function](functions.md) rules.

### Entry selection

A host runs one **selected** program from the entry module: its only
`program def` implicitly, or one chosen by path when several exist
(`agm exec FILE -p review::main`). Modules reached through imports contribute
no entries; their `program def`s stay ordinary functions. A run initializes
every module's static bindings, then calls the selected program.

Inline `-c` source without a `program def` is wrapped in a synthetic
parameterless `program def main`. Its statements and declarations are
resolved in source order, with the same verdicts as the same declarations in
a file: a root `let`/`var` is a declaration whose name collides with a scope
path or another declaration's path exactly as there, and `::x` reaches it. The
REPL has no entry: a `program def` declared there is an ordinary function.

### Parameters

The selected program's value parameters are its **external inputs**. The
parameter list defaults to the **named-only** zone. On the command line a
required `name: text` fills a positional slot, while a defaulted parameter —
and a required `bool`, `Option[T]`, or `Optional[T]` — is addressed by
`--name`. `@arg-pos` opens a positional slot; `@arg-std` accepts both.
Presentation attributes (`@doc`, `@opt-name`, `@opt-short`, `@opt-env`,
`@opt-metavar`, `@opt-hidden`) shape the flag; see
[Attributes](attributes.md#host-parameter-attributes).

Each parameter resolves as CLI token > `@opt-env` variable > qualified config
table > declared default. A required parameter with no external value is a
host invocation error, reported before anything executes. Parameter types
must be JSON-wire-serializable: `text` crosses verbatim, every other type
reads its external text as strict JSON or an [AgL value syntax
literal](host-environment.md#value-syntax); `unit`, function types, and
types reaching a dict whose key is not `Hashable` or not decodable (an
exception key) are rejected. A name-addressable parameter cannot spell an
[engine setting](host-environment.md#engine-settings) name, since both share
one flag and config namespace. Full resolution and help rules:
[Host environment](host-environment.md#program-arguments).

An `@param` static binding is a separate host input owned by its declaring
module, not a value parameter of a `program def`. A selected program exposes
the marked bindings from its transitive import closure alongside its own
parameters; see [Module parameters](host-environment.md#module-parameters).

### Program-selected configuration

A `program def` may carry [`@config`](attributes.md#config), stating the
values its own selection gives to module parameters and engine settings, in
source. It has no effect unless that `program def` is the one a host selects
to run; see [Host-configurable settings](host-environment.md#host-configurable-settings)
for where it ranks.

## Module items

### Import declarations

`import`, `use`, and `export` declarations are **header-only**: they must
appear before any other declaration or expression, at the module root or
inside a named scope region. See [Modules](modules.md) and
[Grammar](grammar.md#import-and-export-declarations) for their syntax.

### Named scope regions

A named scope region is a module item containing nested regions, header
`import`, `use`, and `export` declarations, static declarations, `program def`
declarations, and `let`/`var` bindings. Its `scope`/`end` syntax, declaration
and binder paths, and visibility rules are described in
[Named scopes](scopes.md).

### Declarations

Declarations stand at the module root or in a named scope region, never in a
nested block, unless noted otherwise. Every declaration that defines a name —
the type, function, and binding forms below, along with their parameters and
fields — may carry an [attribute](attributes.md) prefix; `import`, `use`,
`export`, and infix declarations may not.

- **Type declarations** (`record`, `enum`, `exception`, `type`). They may
  refer to types declared later. `record`, `enum`, and `type` declarations
  may be **generic** (`record Box[T]`, `type Pair[A, B] = …`); see
  [Generics](generics.md). `builtin` may prefix any of the four for a
  host-recognized declaration. A scoped `builtin` type carries its scope path
  as part of its nominal identity like any scoped type, but its complete
  scoped name is shared with the host across the whole program and may be
  declared only once at that path; see
  [Built-in functions](functions.md#built-in-functions).
- **`import`/`use`/`export` declarations.** A scoped import tail or use
  contributes bare names only to its own region and nested regions; an
  import's qualifier route stays module-wide. A scoped export re-roots its
  forwarded atoms under the region's path. See
  [Named scopes](scopes.md#import-and-export) and
  [Modules](modules.md#imports-and-use-inside-a-scope-region).
- **Function declarations** — ordinary `def`s, `program def` entry points
  (never a method), body-less host-implemented `builtin def`s, and body-less
  companion-backed `extern def`s (file-backed modules only; see
  [Python FFI](ffi.md)). Root and same-scope `def`s may refer to declarations
  that appear later, enabling mutual recursion (see
  [Functions](functions.md)). An ordinary `def` may be **generic**
  (`def id[T](x: T) -> T = x`). A scoped `builtin def` dispatches to the same
  host implementation as a root one; with first parameter `self` in a type
  scope it is a builtin method (`Agent::ask`), selected by ordinary member
  syntax. Its complete scoped name is subject to the same whole-program
  uniqueness as a `builtin` type.
- **`builtin var` declarations** — body-less host-backed mutable bindings,
  declarable only by a module whose path identity lies under `std`, whether
  it is the entry program or an import. `std/config` exclusively owns the
  engine settings ([Host environment](host-environment.md#engine-settings));
  other standard-library modules own their domain-specific ambient bindings.
- **Infix declarations** — operator fixities, at the module root only
  ([User-defined operators](lexical-structure.md#user-defined-operators)).
- **`let`/`var` bindings** — a plain `let` or `var` is legal anywhere an item
  is. The scope-path-prefixed spelling (`let A::x = …`, `var A::count = …`)
  declares a member of that scope instead, and is legal only at the module
  root or inside a named scope region. A module-level binding's own name may
  not repeat a named scope's name at the same scope path. In a module with a
  static root, a module-level binding is visible regardless of textual order,
  exactly like a `def`; elsewhere it keeps the textual order its root
  statements execute in. See [Named scopes](scopes.md#names-and-visibility).

## Blocks

### The block's value

An executable block — a function body, a branch or loop body, a suite
initializer, a parenthesized block — is a sequence of items whose **value** is
the value of its last item. A module root is static and has no block value.

`let` binds one immutable name and `var` one mutable name
([Bindings and scope](bindings-and-scope.md)), each scoped over its
**continuation**: the rest of the block, and any enclosing continuation that
evaluates after the block, such as a loop's `until` condition. A **final**
`let` or `var` has no in-block continuation, so it makes the block's value
`unit` — or bottom when its initializer does not return:

```agl
program def main() -> unit =
  var n = 0
  do[3]
    n := n + 1
    let doubled = n * 2    # final binder: the body is unit-valued
  until doubled > 4        # the binding is visible to `until`
  print n
```

### Expression statements

Every item before the last is in a **discarded-value position**: a bare
expression there must have type `unit` or bottom, the type of an expression
that does not return normally (`raise`, `return`, `break`, `continue`).
Binders and declarations are valid intermediate items. A loop body's value is
discarded on every iteration, so the same rule applies to its last item.
Side-effecting forms (`print`, `:=`, loops, an `if` without `else`) have type
`unit` and return `()`. A discarded-value position expects `unit`, so a
type-directed call such as `ask` or `exec` there takes its `unit` form:

```agl
program def main() -> unit =
  exec "make build"
  ask "Log a status update."
  print "done"
```

To run a value-producing expression for effect, bind it to the wildcard:
`let _ = call()`.

`=` is not an expression operator, so `n = 2` as a block item is a syntax
error. Use `:=` to reassign a mutable binding, `let`/`var` to introduce a new
binding, or `==` to compare for equality.

## Inline forms

Small workflows fit on one line: `;` separates items inline exactly as a
newline does. `if`/`until` conditions are operator chains, so a `case` or `if`
expression there must be parenthesized; loop bodies admit `case`, `if`, `try`,
and nested loops directly:

```agl
enum Review
  | Pass
  | Fail(issues: array[text])

def refine(spec: text, reviewer: Agent, impl: Agent) -> text =
  var a: text = impl.ask("Implement %{spec}"); var r: Review = Pass
  do[5] r := reviewer.ask("Review %{a}"); case r of Fail(issues) => a := impl.ask("Fix %{issues} in %{a}") | Pass => () until r is Pass
  a

def settle(draft: text, reviewer: Agent) -> Review =
  var r: Review = Pass
  do[3] r := reviewer.ask("Review %{draft}") until (case r of Pass => true | Fail(issues) => issues.size() == 0)
  r
```

A body after `=>` is a single item or a suite (`()` is the idiomatic no-op
body); which items an inline body admits, and which final forms an inline `try` body bars, is specified in
[Inline bodies](grammar.md#inline-bodies).
