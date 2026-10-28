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

General configuration merges from least to most specific: install prefix, AgM home, project config directory, then the invocation directory's `.agm/config.toml`; the loaders accept `cwd=None` to drop that last layer for cwd-independent reads (sync coverage), and relative paths then anchor only to their config file's directory. Tables merge by key. Path-valued settings anchor when read as paths by the declaration that types them (`PathAnchor` of the defining layer, a manifest's its package root; `GeneralConfig.anchors`): relative to their defining file (else the invocation directory when that path does not exist), interpolating `%{VAR}` *leniently* — an unresolved hole stays verbatim, so an irrelevant section never prevents loading.

Sections are consumed by specific features: `[run]`, `[exec]`, `[repl]`, `[agent]` ([agents.md](agents.md)), `[modules]`, `[packages]` (exact package-version pins), `[deps]` (workspace dependency branches), `[warnings]` (static-warning levels by name, merged by key). The `agm config` group copies project config into a workspace, prints its environment, and creates missing files.

## AgL Settings

A **program route** is the selected program's qualified config table; a **module route** is a declaring module's table. `@config` entries on the selected `program def`, a package's manifest `[config]` table, and `@inherit` addresses (another command's configured value) supply further tiers; none applies to `agm check` or the REPL.

| Kind | Precedence (highest first) |
| --- | --- |
| Host parameters (program arguments, function and module parameters, engine settings) | CLI > `@opt-env` > program route > `@inherit` > `@config` > manifest `[config]` > declaration route > default |
| Static-warning levels (`[warnings]`) | source `@allow` > `--warning` (`exec`, `check`, `repl`) > `[warnings]` > catalog default |

The declaration route is the module route of a module parameter, the function's qualified table of a function parameter (`[review.refine]`, `[review.Agent.review]` for a method), `[exec]` of an engine setting, and absent for a program argument; an engine setting has no `@opt-env` or `@inherit`, and a source write to it outranks every tier from its program point onward.

- `config/engine_keys.py` is the pure catalog of engine settings (kind, default, path-ness, validity rule); the host parameters (`runtime/engine_config.py:engine_setting_params`), the checker, IR validation, and the evaluator all derive from it. `PipelineDriver.preflight_arguments` ranks them with the other host parameters (`runtime/param_resolution.py`) and returns one `EngineSettings` (values plus each key's winning `ParamTier`); a configured `Option[text]` setting reads a blank or non-positive value as an explicit none (`HostParamInfo.config_option_text`; `core/parse.py:normalize_option_text` is the one rule). `agm repl` has no program: `host_params/config.py:resolve_programless_engine_settings` runs the same declaration-route resolution and the same `resolve_host_params` over its CLI flags and `[exec]`, so there is one ranking and decoding path. `cli_support/engine_seeds.py` derives `trace` and the trace decision from the ranks (`trace_decision` is the only copy of the rule); a shadowing parameter takes the bare flag and the bare program-route leaf, leaving the engine setting its qualified spelling and `[exec]`.
- `config/general.py::warning_levels_from_merged` validates `[warnings]` (an unknown name or level is a host configuration error); `agm/cli_support/warning_levels.py` resolves flags over it into the invocation's `WarningPolicy` ([agl/hosting.md](agl/hosting.md)). Registered commands pass no flags. `warnings` is a schema section, never a module route.
- `config/qualified_keys.py` routes a table to a module by path suffix (ambiguous suffixes are errors) and keeps layer provenance: each resolved value carries its layer's `PathAnchor`. A package-registered program is also addressed by its command path (`[dv.review]`), with each path prefix as a lower-specificity inherited group table. AgM's own section names are excluded from module routing. A table at a leaf position is a nested route, not a value, when it is the table of a command registered by any mounted package (as declared, the selected program's owner completed from source) or a route table of a declaration or the program (caller-supplied `nested_routes`/`command_tables` plus the scanned route's own); value resolution and every leaf scan (undeclared-key warnings, cross-route checks) share that one predicate, so other tables are native values everywhere.
- `host_params/config.py` is the one resolver of every host parameter kind, keyed by `HostParamKey` (`agl/ir/static_keys.py`: a binding key, which also identifies engine settings, or a `FunctionParamKey`, which identifies a function parameter, a program's own arguments included); `PipelineDriver.preflight_arguments` merges its tiers with `@config` ([agl/hosting.md](agl/hosting.md)). `host_params/inherit.py` resolves `@inherit` addresses: it splits each (`split_command_address`) at the longest command or alias path registered by a mounted package (the run's `command_registry`, `packages/command_registry.py`, built on first use from the mounted packages with command tables completed from source, so a run without `@inherit` scans no other checkout) and reads the remainder as that command's leaf from its program tables, then its package's manifest `[config]` — never its CLI, environment, `@config`, `@inherit` or declaration routes, so inheritance is not transitive; every unregistered address warns, whatever tier covers the parameter, and yields nothing. One configured leaf addressing two parameters — a reached module or function parameter's declaration route and the selected program's tables, program arguments included — is a host configuration error; unused-key diagnostics treat registered command tables as program routes, even when a declaration route collides, and a leaf is consumed when any route reading its table declares it (a `program def`'s declaration route declares all its arguments). `ConfigRoute` (`config/qualified_keys.py`) is the one source of a declaration's table paths (`exact_table_paths`, `table_paths`) and a `QualifiedConfigKey` is a route plus leaf; an engine setting's route is a fixed top-level `section`, not a module route. `commands/exec_program.py` passes the resolved tiers to preflight; [agl/hosting.md](agl/hosting.md) describes how hosts seed the resolved values. `report_undeclared_exec_keys` warns, once per `exec`/`repl` invocation, for `[exec]` keys outside the engine keys and `EXEC_OPTION_KEYS` (`config/general.py`, also the source of the CLI execution options).

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
