# Packages

A package is a portable, versioned collection of AgL modules that can register `agm` commands.
See the [AgL package reference](../agl/reference/packages.md) for what a package means to AgL
source.

| Command | Description |
|---|---|
| `agm pkg init [DIR] [--name NAME] [--version VERSION]` | Scaffold a new package |
| `agm pkg check [DIR]` | Validate a package directory |
| `agm pkg create [DIR] [-o FILE]` | Validate and write a `<name>-<version>.agmpkg` archive |
| `agm pkg install SRC [--project] [--editable] [--reinstall] [--shadow]` | Install and activate a directory or archive |
| `agm pkg switch NAME@VERSION [--project]` | Switch the active version |
| `agm pkg uninstall NAME[@VERSION] [--project]` | Remove the active package or an exact stored version |
| `agm pkg list` | List installed versions and active editable packages |
| `agm pkg info NAME` | Show an active package's metadata and dependency status |
| `agm pkg sync [--project]` | Install the active packages' unsatisfied Python requirements |

`DIR` defaults to the current directory. `install`, `uninstall`, `switch`, and `sync` act on the
global store unless given `--project` (see [Project store](#project-store)). `--dry-run` is
available on commands that report planned operations; read-only `check`, `list`, and `info` do not
accept it.

## Quick start: a package with a command

```sh
agm pkg init review-tools
# creates review-tools/package.toml and review-tools/src/main.agl
```

Replace the starter program in `review-tools/src/main.agl` (any module under `review-tools/src/`
works) and register it in `review-tools/package.toml`:

```agl
@doc("Review a change")
program def review(target: text, strict: bool = false) -> unit =
  print("reviewing %{target}")
```

```toml
[commands]
pr-review = { program = "review-tools/main::review" }
```

```sh
agm pkg check review-tools
agm pkg install --editable review-tools   # source edits take effect immediately
agm pr-review --target src --strict
agm pr-review --help
```

## Layout

```
review-tools/
  package.toml        # manifest
  src/                # module tree: modules import as review-tools/...
    main.agl
    main.py           # optional extern companion beside its module
  prompts/            # resources: anything outside the module tree
```

The package name is one AgL identifier segment and not a reserved keyword. `pkg check`,
`pkg create`, and `pkg install` also reject a name AgM reserves: a built-in command or root alias
(`wsp`, `wt`), or one of AgM's own config sections (`agent`, `deps`, `modules`, `packages`,
`params`, `warnings`). Package modules may
import only their own tree, packages declared in `[dependencies]`, and `std`.

## Manifest

`package.toml` supports `[package]` (required), `[dependencies]`, `[python]`, `[commands]`,
`[aliases]`, and `[config]`.
`pkg check`, `pkg create`, and `pkg install` reject a key the schema does not define, so a
misspelled field is an error rather than silently ignored.

### `[package]`

```toml
[package]
name = "review-tools"          # required: one AgL identifier segment, not a keyword or reserved name
version = "1.0.0"              # required: complete semantic version
description = "Review workflows"
license = "MIT"
authors = ["Ada <ada@example.test>"]
repository = "https://example.test/review-tools"
keywords = ["review", "workflow"]
```

A package identity is the complete version, build metadata included: `1.0.0+linux` and
`1.0.0+macos` are distinct packages.

### `[dependencies]`

```toml
[dependencies]
std = "0.1"                                          # minimum version; "0.1" means 0.1.0
helpers = { version = "1.2.0", path = "../helpers" }  # local checkout, relative to the package
remote = { version = "2.0.0", url = "https://example.test/remote.agmpkg", hash = "sha256=<64 hex>" }
```

- A value is a minimum version with no upper bound; build metadata is ignored when matching.
- `path` and `url` are optional development and download sources; they cannot be combined. A
  `url` requires a SHA-256 `hash` with a `sha256=`, `sha256:`, or `sha256-` prefix.
- `std` is an AgM compatibility contract: the running AgM must be at least the declared version
  and in the same release line (same minor for `0.x`, same major from `1.x`).
- Only direct dependencies are importable; a dependency's own dependencies are not.

### `[python]`

```toml
[python]
dependencies = ["typesafe-sdk>=0.7,<1", "tomli; python_version < '3.11'"]
```

`dependencies` lists the third-party Python distributions the package's
[companions](../agl/reference/ffi.md) import, as [PEP 508](https://peps.python.org/pep-0508/)
requirements. Companions run in AgM's own interpreter, so requirements are checked against its
environment: one whose marker does not apply is always satisfied, and a requested extra holds when
every requirement the installed distribution declares for that extra is itself satisfied.
An invalid requirement, a direct URL reference (`name @ url`), and a marker that does not evaluate
as a requirement's — one referring to `extra`, `extras`, or `dependency_groups`, or comparing
values incomparably (such as `os_name ~= "posix"`) — are manifest errors. A distribution may be
listed more than once, for example with markers selecting per-environment variants.
Requirements are stored verbatim, as declared, and are part of the package's content hash.
[`install`](#commands) and [`sync`](#commands) install unsatisfied requirements into that
environment; [`check`](#commands) and
[`info`](#commands) only report them. The activation index is the source of truth: `sync` installs
only the requirements of active packages. A program whose run imports one of the package's
companions while a requirement is unsatisfied fails before running, with an error naming the
package and the requirement. It suggests `agm pkg sync` when the package is the active one (same
name and root), otherwise installing it with `agm pkg install [--editable] <root>`, as for a
development checkout or a project-pinned version that is not active.

### `[commands]`

```toml
[commands]
pr-review = { program = "review-tools/main::review" }

[commands.pr-review.batch] # multi-level command: agm pr-review batch
program = "review-tools/main::batch"
```

- Nested table components become command words: `[commands.devel.review]` registers `devel
  review`. The equivalent quoted flat form, `[commands."devel review"]`, is also accepted. A path
  cannot start with a built-in command or root alias (`wsp`, `wt`). `pkg check`/`create`/`install`
  also reject a path (including an `@command` path) starting with one of AgM's config sections,
  like a package name; an already-installed registration still loads, dispatches, and uninstalls,
  but one starting with a schema section (`agent`, `deps`, `modules`, `packages`, `params`,
  `warnings`) gets no command-path config table, exact or inherited.
- `program` names the `program def` to run as `<module>::<program>`: `review-tools/main::review` is
  program `review` in module `review-tools/main`, file `review-tools/src/main.agl`. Must belong to
  this package, take no type parameters, return unit; its signature arguments and the module
  parameters it reaches become the command's host surface.
- `doc` is the command's prose. A command naming a program never states it: that program's source
  `@doc` supplies it, and an installed manifest records the result. Command help shows it, then
  signature options followed by one section for each module declaring a visible reached module
  parameter. Parameter `@doc` attributes describe their options.
- Omit `program` for a command group (must have descendant commands), whose only prose is its own
  `doc` (TOML multiline strings work; a paragraph holding an indented line keeps its layout in
  help). Undeclared parent groups work automatically, with generated help listing their
  descendants. A listing shows the opening paragraph of each command's `doc`,
  falling back to a generated summary when there is none.

```toml
[commands.devel]
doc = "Development workflows"

[commands.devel.review]
program = "review-tools/main::review"

[aliases]
dv = "devel"
rev = "devel review"
```

`agm devel`, `agm devel --help`, and `agm help devel` show the group's guidance and a generated
subcommand listing. Leaf commands generate usage and option help from their program signatures and
reached module and function parameters, so authored help is optional. Missing program arguments, and values
from the command line or the environment that do not parse, show that command's usage; a
configured value that does not parse is a configuration error.

A program may register its own command instead, via
[`@command`](../agl/reference/attributes.md#command) on the `program def`
([Programs and commands](../agl/reference/packages.md#programs-and-commands)). The following
replaces the `[commands.devel.review]` entry above; the manifest needs entries only for commands
no program claims. Declaring one path in both places is an error whenever the two name different
programs, as is two programs claiming the same path.

```agl
@command("devel review")
@doc("Review changes")
program def review(target: text) -> unit =
  print "reviewing %{target}"
```

### `[aliases]`

`[aliases]` and command groups are always manifest-declared, never via program attributes. Each
key is an alternate command path (same restrictions as commands) naming a canonical command or
group in this package, never another alias; an alias cannot overwrite another command or alias,
but may target a command a program registers via `@command`. Group aliases expose all canonical
descendants: the example supports both `agm dv review` and `agm rev`. Aliases participate in
activation conflicts, project pins, help, and completion like commands.

Config tables use dots between path words: `[rev]`, `[dv.review]`, and `[devel.review]` all
address the same program route (signature arguments, engine settings, and resolving module and
function parameters, bare or dotted-qualified), even via `agm exec`. Different keys in these
tables combine; setting the same key through multiple spellings in one layer is an ambiguity error,
and a later config layer overrides an earlier one. CLI flags still take precedence. Unused-key
diagnostics validate a registered command table against that command's program route, not a
declaration route with the same dotted path.

A group table also supplies defaults to every command beneath it: `[devel]` and, through the
`dv = "devel"` alias, `[dv]` both feed `devel review`, however it is invoked. Its own command's
exact table always wins over an inherited group table, regardless of which config layer set either
one; among group tables, a deeper one wins, and a same-depth pair (an alias and its canonical, or
two sibling group tables) setting one key in one config layer is an ambiguity error, just like two
spellings of an exact table. A command's own table also feeds every registered command beneath
it — `[devel.sub]` feeds `devel sub review` when `devel sub` is itself a command — and a key it
sets is never reported as an unconsumed argument on that ancestor command, since the descendant
command may be the one that consumes it.

### `[config]`

```toml
[config]                          # root leaves: defaults for every program the package owns
trace = true
"std.http.http-timeout" = "30s"   # module or function parameter, bare or dotted spelling

[config.devel]                    # a registered command, group, or alias path: `devel`
timeout = "1h"

[config.devel.review]             # command `devel review`
agent = "claude/opus"
```

Package-author defaults: root leaves apply to every program the package owns, and each nested
table names one of the package's own registered command, group, or alias paths
(`@command`-registered paths included). A leaf is an engine setting, the bare or dotted spelling of
a `@param` binding or `@param` function parameter (`"devel.review.refine.max-steps"`), or the
external name of a name-addressable signature argument of a program registered under that table's
command (or one beneath it; at the root, any command's), and must be reachable from the package's
own import graph: a dependency or `std` module the package never imports contributes no spelling.
A bare leaf names a `@param` binding or `@param` function parameter of the closure, or a signature
argument that shadows the engine setting's name (the parameter wins, as for flags); the engine
setting is then addressed as `"std.config.NAME"`. `pkg check`/`create`/`install` reject an
unregistered nested table, an unknown leaf, an engine value that fails to decode, and a word that
is ambiguously both a registered subcommand and a setting spelling. At run time, whenever the
selected program is owned by the package (run as a registered command, an installed reference, or
a file path), its command table beats an inherited group table beats the root, ranking below
config-file program, command, and group tables and `@config`, and above `[exec]` and a parameter's
declaration route (see the Configuration bullet below); a program argument it sets is beaten by
its CLI value, `@opt-env` and those tables. A relative path-valued setting, such as `trace-file`
or a `path` parameter, resolves against the package root, else the current working directory
when that path does not exist there. Validation checks spellings against the whole closure; a leaf in
a command's own table that the selected program does not consume (a parameter outside its import
graph, or a bare name a reserved flag shadows) is ignored with a run-time warning, like an unused
config-file key.

## Registered commands

An active package's commands run as `agm COMMAND ...` (longest matching path wins), appear in
`agm help` and shell completion, and support `--help`. A command comes from the manifest's
`[commands]` table, a program's own `@command` attribute, or both merged.

- **Host parameters.** Signature value parameters project onto the command's CLI as for `agm exec`:
  positional-capable parameters fill trailing words in order, name-addressable ones take
  `--name VALUE` (`--name`/`--no-name` for `bool`). Every `@param` binding and `def` parameter
  the selected program reaches is a module or function parameter too: its external name supplies a
  resolving bare flag and dotted qualified flags, including `--no-...` where its type permits.
  Help groups visible module and function parameters by declaring module, and completion offers
  their resolving spellings. See [Program arguments](agl.md#program-arguments) and
  [Host parameters](agl.md#host-parameters). A registered command reserves `--echo`/`--no-echo`,
  `--max-call-depth`, and `-h`/`--help`; its parameters may reuse other spellings `agm exec`
  reserves, such as `--module-path` or `-p`, which `agm exec` still rejects.
- **Run-time options.** `--echo`/`--no-echo` and `--max-call-depth` work as for
  [`agm exec`](agl.md#agm-exec), anywhere after the command path. The engine settings
  (`--strict-json`, `--agent`, `--timeout`, `--trace`, ...) are parameters of the command's
  surface like any other, so a same-named parameter shadows the bare flag and
  `--std.config.NAME` always addresses the setting.
  There is no `--warning` flag: warning levels come from the [`[warnings]`](config.md#warnings)
  config table only.
- **Configuration.** Omitted signature arguments and engine settings use the program route:
  the program's qualified table (e.g. `[review-tools.main.review]` for
  `review-tools/main::review`) or a registered command path (`[pr-review]`, or `[dv.review]`
  for command `dv review`). Both name the same program regardless of how it runs, so setting
  one key through both in one config layer is an error. Below either, an inherited group table —
  a proper prefix of a command path, such as `[dv]` for `dv review` (see
  [Aliases](#aliases)) — supplies further defaults. The program's own
  [`@config`](../agl/reference/attributes.md#config) entries rank below the program route: an
  engine setting falls through to `@config` before `[exec]`, a module or function parameter
  falls through to `@config` before its declaration route, and a signature argument reads
  `@config` too (`main::x`). Below `@config`, the owning
  package's manifest [`[config]`](#config) supplies further defaults — its own command table,
  then an inherited group table, then the root — still above `[exec]` and a parameter's
  declaration route; signature arguments read the manifest tier too. A resolving program-route
  leaf — its bare external name, or a dotted qualified spelling as a quoted key — still overrides
  both. CLI
  values win over all of them; `@opt-env` reaches a signature argument and a module or function
  parameter alike, but not an engine setting, which only a source `std/config` write outranks. See
  [Configuration](agl.md#configuration).
- **Conflicts.** Two active packages cannot own the same command path; install the later one with
  `--shadow` to make it the owner. Shadowing is recorded per store tree, so a rebuilt activation
  index preserves it.
- **Editable packages** re-scan the module tree and re-read the manifest on every dispatch, so
  command edits — including a program's `@command` — apply without reinstalling. The scan is
  best-effort: a module that cannot be parsed, or a registration that conflicts with another in
  the same package, registers nothing and leaves the manifest's own commands standing, so a
  mid-edit module does not break the rest of the CLI. `check` reports those errors. A command path
  that collides with another active package's is still a conflict, resolved as below. An installed store package or an archive instead carries
  one complete command table, merged at `install` or `create` time; nothing rescans it afterwards.
  A package activated without `--shadow` cannot acquire a conflicting command later.

## Commands

**`init`** creates `DIR` when missing and writes `package.toml` (name from the directory, version
`0.1.0`, and no dependencies) plus a starter `src/main.agl` unless one
exists; refuses a directory that already holds a manifest.

**`check`** scans the module tree for `@command`-registered programs, merges them with
`[commands]`, then validates the manifest, the `src/` module tree, command program references,
literal `resource` targets, import visibility, `@param` spellings (no two parameters a registered
program reaches may share a fully qualified spelling), `@inherit` addresses, and dependency
satisfiability without modifying anything — the same diagnostics `install` reports. The `std`
floor is checked against the running AgM; other dependencies resolve from the store, then a
`path`; a `url` counts as satisfiable and is not fetched. Each `[python]` requirement AgM's interpreter environment does not satisfy is
reported with its status (as `info` shows it) and fails the check; `check` never installs it —
`sync` does, once the package is active.

An [`@inherit`](../agl/reference/attributes.md#inherit) address must start with a command or alias
path that runs a program and that the package or a declared dependency registers (a dependency's
`@command` registrations count, whether it is installed, editable, or a `path`); the rest must
spell a name-addressable argument of that program, a `@param` spelling of its module's import
closure, or an engine setting name. Loose sources are checked for address syntax only.

**`create`** runs the same validation on the *distribution*, after merging the module tree's
`@command` registrations into the manifest, then writes a deterministic archive beside `DIR` (or
at `-o FILE`) whose manifest already carries that merged command table. The distribution excludes
hidden paths, VCS and cache directories, `.agmpkg` files, and anything matched by `.gitignore`
files; a resource excluded this way fails creation. The archived manifest drops `path` sources, so
every path-only dependency needs a stored version or `url` first.

**`install`** takes a directory or archive, resolves the dependency closure, validates, and stores
the distribution in `<AgM-home>/packages/<name>/<version>/` with a SHA-256 `RECORD`. From a
directory source it first merges the module tree's `@command` registrations into the manifest, so
the stored package carries one complete command table. Activation is published atomically only
after the resulting selection validates; a failed install leaves nothing active. If a package with
the same name and version already exists with different contents, install reports that identity and
suggests `--reinstall`. That flag completely replaces the immutable store tree, restoring the
previous tree if activation fails. It cannot be combined with `--editable`. Dependencies resolve
from the store first, then a declared `path` (installed alongside), then a `url` (fetched and
hash-verified; never in `--dry-run`). Versions are kept side by side, one per identity (build
metadata included, as above). `--editable` activates the source directory in place: no copy, no
`RECORD`, edits visible immediately, and its command table is re-derived from source on each
dispatch. Before activation is published, if AgM's interpreter environment does not satisfy some
`[python]` requirement of a package in the new selection, the union of every selected package's
requirements is installed into it with `uv pip install --python <interpreter>`
(`<interpreter> -m pip install` when `uv` is not on `PATH`). The installer resolves them jointly:
already satisfied distributions are left alone, and conflicting requirements across packages fail
the install rather than downgrading one. An installer failure or interruption fails the install and
leaves the store as it was. `--dry-run` prints the installer command instead of running it.

**`switch NAME@VERSION`** selects an exact stored version as globally active. Other stored versions
stay installed. Activation validates package dependencies and registered commands.

**`uninstall`** verifies the `RECORD`, validates the selection without that package, deactivates,
and removes the recorded files (plus cache and VCS residue). An editable package is only deactivated. Command
ownership displaced by the removed package is restored. `uninstall NAME@VERSION` removes that exact
stored version; when another version is active, activation stays unchanged. A matching active
editable package is deactivated. Python distributions are never removed.

**`list`** shows every stored version as `active` or `installed`, and every active editable
package.

**`info`** shows metadata, command registrations with their prose, whether each direct
dependency is active, unsatisfied, or missing, including the inferred `std` upper bound, and, as
`requires python package SPEC: STATUS`, each `[python]` requirement's status in AgM's interpreter
environment: `installed VERSION`, `installed VERSION (unsatisfied)`, `missing`, or
`not applicable` (its marker does not hold).

**`sync`** restores Python requirements for active packages in AgM's interpreter environment:
when some `[python]` requirement of an active package (store or editable) is unsatisfied, the
union of every active package's requirements is installed exactly as `install` does, and each
requirement needing installation is listed; otherwise it reports that all are satisfied and runs
no installer. An installer failure exits non-zero. `--dry-run` prints the installer command
instead of running it.

## Project store

Inside an AgM project, `--project` makes `install`, `uninstall`, `switch`, and `sync` act on the
project's own store, `<project>/packages` (`$PROJ_DIR/packages` for a split project,
`<repo>/.agm/packages` for an embedded one). The first `--project` install creates the directory;
`agm init` does not. Outside a project, `--project` is an error.

- The store is self-contained: its own activation index, lock, provenance, and `RECORD`s, and the
  same directory, archive, `--editable`, and `--reinstall` installs. Its trees are installed
  packages (their warnings are capped like the global store's) unless editable.
- Inside the project, package selection layers the stores: the global index, then the project
  index replacing same-named entries, then `[packages]` pins. A pin resolves in the project store
  first, then the global one. Outside the project, project packages are not selected at all, and a
  development checkout still takes precedence over every store.
- A project install resolves dependencies from the project store, then the global store's active
  selection; a path or URL dependency neither has is installed into the project store. A global
  install never sees project stores. A project package selected over a global one of the same
  name replaces it for every dependency check. `uninstall`, `switch`, and the install itself
  validate the resulting effective selection, so a project operation cannot leave a package
  without its dependency. Inside a project, `check` and `create` resolve dependencies the same
  way.
- A global `install`, `uninstall`, or `switch` run inside a project succeeds on its own terms, but
  warns when the project's effective selection no longer validates (for example, a project
  package depending on the global package just removed).
- `std` is global-only: `install --project` of it is rejected.
- `list` and `info` show the global store only. Project packages mount their modules but register
  no commands: run them with `agm exec NAME/MODULE::PROGRAM`. A project package replacing a global
  one therefore hides that package's commands inside the project.
- `uninstall`, `switch`, and `sync` with `--project` do nothing, and create nothing, until a
  `--project` install has created the project store.
- `sync --project` installs the Python requirements of the project store's active packages into
  AgM's interpreter environment, as `sync` does for the global store.

## Version pins

Select a stored version per project instead of the globally active one:

```toml
[packages]
review-tools = "1.2.3"
platform-tools = "1.0.0+linux"
```

Pins live in any layered `config.toml` (install prefix, AgM home, project `config/config.toml`,
then `.agm/config.toml`), merge by name with later layers winning, and select an exact identity that
must already be a valid stored version of the project store or, failing that, the global one. A pin
affects module roots, command dispatch, help, and completion for the invocation only; it never
installs or fetches. The `list` and `info` commands
report the active package set. A development checkout of the same name discovered from the
execution root still takes precedence.

## The `std` package

The standard library is a managed package at the running AgM's version. Installation accepts
only the shipped source at that version, and the package cannot be uninstalled.

## Limits

- Archives: 10,000 entries, 16 MiB ZIP metadata, 256 path components, 64 MiB per entry, 512 MiB
  total; no ZIP64.
- Downloads: 128 MiB, 30-second inactivity timeout, partial files removed on failure.
