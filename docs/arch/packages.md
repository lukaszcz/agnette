# Packages

A package is a portable, versioned AgL module collection: a `package.toml` manifest plus a module tree in `src/`, mounted under the package name. A manifest may declare version-floor dependencies, PEP 508 requirements for its Python companions, CLI commands backed by `program def` entries, and default config; literal `resource` paths in its modules must exist in the tree. `agm pkg init` scaffolds a development package. The store, installation, and the managed `std` package are covered in [package-store.md](package-store.md).

## Package Model

- **Identity** is the package name plus its complete canonical version; semantic precedence governs minimums and highest-version selection.
- **Dependencies** are floors: installation picks the highest installed version meeting each floor (else installs its path or URL source), and activation keeps one version per name — development checkouts before the stores. A project install also draws on the global selection ([package-store.md](package-store.md)). A `std` floor must fall within the running AgM release line.
- **Origin.** A package is *installed* (an immutable store tree) or *local* (a development checkout or editable selection); this decides whether its warnings are reported ([agl/hosting.md](agl/hosting.md)). A store-selected package (installed or editable) carries its store ([package-store.md](package-store.md#effective-selection)).
- **Development packages** are live checkouts found as the nearest ancestor `package.toml` of the entry file (or working directory); discovery mounts it and its path dependencies, and the rest come from the active selection.
- **Import visibility** is enforced by the AgL module loader ([agl/modules.md](agl/modules.md)): a package module may import only its own tree, its declared dependencies, and the standard library. Loose roots are unrestricted.
- **Manifest `[config]`** holds package-author defaults for programs the package owns, keyed by root and by registered command paths ([config.md](config.md)).
- **Python requirements** are checked against, and installed into, AgM's own interpreter ([core.md](core.md)) — for project-store packages, a per-project overlay instead ([package-store.md](package-store.md)); an unsatisfied one fails a run before its companions import.

## Discipline Validation

`agm pkg check` validates a development package without modifying it; `pkg create` and `pkg install` run the same validation once. Source-free manifest and tree checks come first, then the module graph is loaded and name-resolved with runtime visibility, rejecting unresolved modules, imports outside the closure, invalid command programs, missing resources, `@param` spellings shared within a registered program's closure, invalid `[config]` keys, and `@inherit` addresses naming no command of the package or a declared dependency, or no leaf of its target program (its arguments, the `@param` spellings of its module closure, and engine names). Unknown manifest keys are rejected when validating but tolerated when loading, so stored packages stay manageable. A distribution is validated against the source tree it ships from, with exclusion-specific checks and a digest proving the staged copy matches; a `.agmpkg` archive is materialized and resolved once.

## Registered Commands

Commands come from two equivalent sources:

- The manifest `[commands]` hierarchy maps nested tables to CLI path words, naming a program or describing a group; `[aliases]` maps alternate paths. Group prose lives here.
- `@command` on a `program def` ([agl/frontend/scope.md](agl/frontend/scope.md)). A parse-only, AST-level scan (`source_commands.py`) merges these registrations into the manifest wherever a command table is needed (install, check, archive, editable resolution, `exec`'s owning development checkout, and every mounted one once per run by `complete_packages` when `@inherit` resolves; `PackageInfo.live_source` builds every live-source package — checkout, path or editable dependency — with the matching incomplete-table flag); it reuses the module loader's parse cache and needs no module graph. The first path is canonical and further paths merge into manifest aliases. Conflicting declarations of one path are errors.

Each invocation derives its command registry from the selected manifests, so project pins and the project store affect dispatch, help, and completion. The registry is *layered*: project-store registrations rank above every global one, as if shadow-installed after them, so a project package's commands shadow same-path global commands inside the project and nowhere else (`pkg install --project` reports them like `--shadow` does). `effective_command_index` is its single derivation; `exec` builds its own `command_registry` (for `@inherit` and nested-route command tables) from the mounted roots ordered by `by_command_priority`, the same priorities, so it cannot disagree with dispatch: a development checkout ranks as the selected package of its name, and one no store selects ranks below every store registration (dispatch never runs it). A command path may not start with a reserved AgM name (`command_catalog.py`); activation rejects path conflicts between packages of one store unless installed with `--shadow`. Dispatch runs the program through the same host as `agm exec`, with the same parameter projection and precedence ([cli.md](cli.md)). An editable package re-scans its source when its commands are needed: operations that persist command state (install) fail on a scan error, while reads (dispatch, help, completion, `pkg sync`) fall back to the manifest's own commands.

## Code Entry Points

- `src/agnette/packages/manifest.py` — manifest schema, alias expansion, distribution-manifest view.
- `src/agnette/packages/model.py`, `development.py`, `dependencies.py` — identity and version selection, development-package discovery, dependency closure.
- `src/agnette/packages/discipline.py` — package validation; `source_commands.py` — `@command` scan and merge; `command_registry.py` — command and alias path to registering package, and `@inherit` address splitting shared by validation and the host.
- `src/agnette/packages/layout.py` — pure layout constants.
- `src/agnette/agl/modules/roots.py`, `loader.py` — package-root mounting and import visibility.
- `src/agnette/agm/commands/pkg/` — the package CLI; `src/agnette/agm/cli_dispatch.py`, `commands/exec_program.py` — registered-command dispatch and execution.
- `packages/sysone/` — the repository's `sysone/jev` package (TypeSafe SDK companion), outside the coverage gates.
