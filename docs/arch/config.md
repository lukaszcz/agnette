# Configuration

AgM is configured through layered TOML files resolved relative to a *config context* — the AgM home, the current project directory, and the invocation directory — and merged so that more specific scopes override more general ones. Commands read only the sections they need.

## Config Context

The project directory comes from walking up from the invocation directory to a recognizable project layout, else from `PROJ_DIR` ([workspaces.md](workspaces.md)), so a command behaves the same from any workspace. `context.py` also memoizes reads repeated within one invocation (activation index, package pins, active package set), keyed by content stamps of their inputs.

## AgM Home and the Standard Library

One AgM home holds config, sandbox settings, the AgL global library, and the package store: `AGM_HOME`, else a populated `<install-prefix>/.agm`, else `~/.agm`. `just install` copies the templates from the repository's `config/` into it.

The standard library resolves from `AGM_STDLIB` (unchecked override), then a development `std` checkout containing the invocation's anchor, then the active managed `std` store package, then the shipped tree located by `agnette.stdlib_locator` ([package-store.md](package-store.md)).

## Highlighting and Editor Assets

`just install` also refreshes the editor modes and generated definitions in
`<AgM home>/highlighting/`, the KDE XDG data directory and bat's configuration,
preserving existing Pandoc defaults unless forced.
Prefix installs isolate AgM home and XDG destinations. Emacs packages a generated
vocabulary with hand-written font-lock; micro remains hand-written.
See [highlighting](highlighting.md) for components and
[development tooling](../commands/dev.md) for destinations and usage.

## Layering and Precedence

General configuration merges from least to most specific: install prefix, AgM home, project config directory, then the invocation directory's `.agm/config.toml`. Tables merge by key. Path-valued settings resolve relative to their defining file (else the invocation directory when that path does not exist) and interpolate `%{VAR}` *leniently* — an unresolved hole stays verbatim, so an irrelevant section never prevents loading.

Sections are consumed by specific features: `[run]`, `[exec]`, `[repl]`, `[agent]` ([agents.md](agents.md)), `[modules]`, `[packages]` (exact package-version pins), `[deps]` (workspace dependency branches), `[warnings]` (static-warning levels by name, merged by key). The `agm config` group copies project config into a workspace, prints its environment, and creates missing files.

## AgL Settings

A **program route** is the selected program's qualified config table; a **module route** is a declaring module's table. `@config` entries on the selected `program def` and a package's manifest `[config]` table supply further tiers; neither applies to `agm check` or the REPL.

| Kind | Precedence (highest first) |
| --- | --- |
| Engine settings (`std/config` builtin vars) | source write > CLI flag > program route > `@config` > manifest `[config]` > `[exec]` > default |
| Static-warning levels (`[warnings]`) | source `@allow` > `--warning` (`exec`, `check`, `repl`) > `[warnings]` > catalog default |
| Program arguments (`program def` parameters) | CLI flag > `@opt-env` > program route > signature default |
| Module parameters (`@param` bindings) | CLI flag > `@opt-env` > program route > `@config` > manifest `[config]` > module route > initializer |

- `config/engine_keys.py` is the pure catalog of engine settings (kind, default, path-ness, validity rule); CLI flags, the checker, IR validation, and the evaluator all derive from it. `cli_support/engine_seeds.py` resolves the CLI-over-config tiers once per run.
- `config/general.py::warning_levels_from_merged` validates `[warnings]` (an unknown name or level is a host configuration error); `agm/cli_support/warning_levels.py` resolves flags over it into the invocation's `WarningPolicy` ([agl/hosting.md](agl/hosting.md)). Registered commands pass no flags. `warnings` is a schema section, never a module route.
- `config/qualified_keys.py` routes a table to a module by path suffix (ambiguous suffixes are errors) and keeps layer provenance. A package-registered program is also addressed by its command path (`[dv.review]`), with each path prefix as a lower-specificity inherited group table. AgM's own section names are excluded from module routing.
- `cli_support/param_config.py` resolves module parameters across both routes; unused-key diagnostics treat registered command tables as program routes, even when a module route collides. `commands/exec_program.py` resolves program arguments and engine settings from the program route; [agl/hosting.md](agl/hosting.md) describes how hosts seed the resolved values.

## Sandbox Configuration

Sandbox settings are per-command JSON profiles discovered in the AgM home, the project config directory, and the invocation directory's `.sandbox/`, merged across them, and shared by `agm run` and AgL sandboxed calls ([sandbox.md](sandbox.md)).

## Code Entry Points

- `src/agnette/config/context.py` — config context, project discovery, per-invocation memo; `home.py` — AgM home resolution (a leaf).
- `src/agnette/config/general.py` — layer loading, merging, and per-feature readers.
- `src/agnette/config/engine_keys.py`, `qualified_keys.py` — engine-key catalog; qualified AgL key routing.
- `src/agnette/config/module_roots.py` — AgL module roots; `src/agnette/stdlib_locator.py` — the shipped stdlib tree.
- `src/agnette/config/sandbox/` — sandbox settings discovery and merging.
- `src/agnette/agm/commands/config/` — the `agm config` commands; `config/` (repository root) — templates and editor integrations.
- `tools/install_highlighting.py`, `tools/install_emacs_mode.py` — generated definitions, Pandoc defaults and Emacs packaging.
