# Packages

[← Index](index.md)

A package is a portable, versioned collection of AgL modules. To the language
it is a module tree with a name: every module the package provides has a
[slash path](modules.md#slash-path-identity) whose first segment is the package
name, and a package module sees only the modules it is entitled to. The
manifest format, dependency resolution, the package store, and the `agm pkg`
commands are host mechanisms described in the
[package command reference](../../commands/pkg.md); this chapter covers what a
package means to AgL source.

## Layout and identity

A package is a directory holding a `package.toml` manifest and a module tree
in its `src/` subdirectory. Everything else in the directory — prompts,
templates, data — is a resource, laid out freely outside the module tree.

```
review-tools/
  package.toml          # manifest: name, version, dependencies, commands
  src/                  # module tree: modules import as review-tools/...
    review.agl
    review.py           # extern companion, beside its module
    lint.agl
  prompts/              # resources, any layout outside the module tree
    review.md
```

The package name must be a single AgL identifier segment and not a reserved
keyword; validating a package also rejects a name AgM reserves for its own
commands or config sections ([`agm pkg`](../../commands/pkg.md#layout)). It is
the mandatory first segment of every module the package provides:
`review-tools/src/review.agl` has identity `review-tools/review`, both inside
the package and from any importer. The `src/` tree is mounted
under the package name; the package root itself is not a search root, so a
module file placed outside `src/` does not belong to the package and is not
mounted.

A package under development — a source checkout discovered from an execution
root or from a source file's containing tree — behaves exactly like an
installed one. Its modules keep their package-qualified identity, so a file
inside the package is compiled the same way whether it is executed directly,
imported, or invoked through a registered command.

## Import visibility

Package modules import with the ordinary [import forms](modules.md#imports).
What they may import is restricted:

- modules of the same package, through their package-qualified paths;
- modules of packages the manifest declares as dependencies, direct
  dependencies only — a dependency's own dependencies are not visible unless
  declared too;
- the standard library, which never needs a declaration.

An import outside this set is a static error naming the missing dependency,
even when a module with that path exists on some search root. Loose modules —
`-c` source, REPL entries, and files no package owns — remain unrestricted:
they may import any mounted package module alongside their loose neighbours.
Ownership follows the file's location, not the path spelling and not how the
file was reached: a file inside a package imports under its package's
entitlements whether an import reached it or it is the entry program, and a
loose module never borrows package visibility by sharing a declared
dependency's leading path segment.

<!-- agl-check: fragment -->
```agl
import review-tools/lint          # same package
import judge/verdict              # declared in [dependencies]
import std/fs                     # always visible
```

Ambiguity rules are those of every module: a package module path must resolve to exactly
one file across the mounted roots, and a loose module whose path collides with
a package module is an ordinary ambiguity error.

## Programs and commands

A package module may declare [`program def`](program-structure.md) entry
points like any other module. A program is addressed by its package-qualified
module route and declaration path, `review-tools/review::main`, which is how
the host selects it for execution and how a manifest `[commands]` entry names
it. Inside the package it is an ordinary function.

A program registers itself as a command by carrying
[`@command`](attributes.md#command), whose argument is the command path a
reader invokes; the program's `@doc` is that command's prose. A package's
commands are therefore declared where its programs are, and the manifest need
name only the commands no program claims. Declaring one command path in both
places — a manifest `[commands]` entry and a program's `@command` — is an
error whenever the two name different programs.

```agl
@command("dev review")
@doc("Review the working tree.")
program def main(strict: bool = false) -> unit =
  print "review (strict = %{strict})"
```

A registered command runs its program exactly as a directly executed selected
program runs: its own value parameters and every `@param` binding in its
transitive import closure form its host surface, with the same
[presentation attributes](attributes.md#host-parameter-attributes),
[flags](host-environment.md#program-arguments), and
[help](host-environment.md#help). Command
groups and aliases are defined in the
[package manifest](../../commands/pkg.md#commands), and either may name a
command a program registers.

## Configuration routes

A package-owned program reads its package-qualified program table, the table
of each command or [alias](../../commands/pkg.md#aliases) path naming it (with
inherited group tables for the path's prefixes), and its manifest
[`[config]`](../../commands/pkg.md#config) table ([Config-file
schema](host-environment.md#config-file-schema),
[Precedence](host-environment.md#precedence)); with `dev review` registered
for the program above, this configures it however it is run:

```toml
[dev.review]
strict = true
```

## Resources and companions

[`resource` and `resource-dir`](expressions.md#resource-and-resource-dir) in a
package-owned module are anchored at the package root, not at the module's
directory, so a resource path is stable wherever the package is mounted and
whoever imports it. The literal path is relative, forward-slash, and cannot
escape the package; it resolves during linking, and a missing target is a
static error. Package validation and archive creation check every literal
resource target, so a resource excluded from an archive fails creation rather
than producing a broken package.

<!-- agl-check: fragment -->
```agl
import std/fs

let prompt = resource("prompts/review.md")

program def main() -> unit =
  print(fs::read(prompt))
```

An [`extern def`](ffi.md) companion is the `.py` sibling of its module inside
the module tree and is included with the package; there is no separate
declaration for it. The third-party Python distributions a companion imports
are declared as PEP 508 requirements in the manifest:

```toml
[python]
dependencies = ["typesafe-sdk>=0.7,<1"]
```

Companions run in AgM's own interpreter, whose environment must satisfy the
requirements; installing and checking them is described in
[`agm pkg`](../../commands/pkg.md#python). A run that would import a companion
of a package with an unsatisfied requirement fails before evaluation, naming
the package and the requirement.

## Diagnostics

An import from a package module that reaches outside its package, its declared
dependencies, and the standard library is a visibility error naming the
undeclared package. A package with no `src/` module tree, a command that names
no valid `program def`, and a literal resource with no target are reported by
package validation before anything runs. All of these are static errors.
