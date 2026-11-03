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

The declaration route is the module route of a module parameter, the function's qualified table of a function parameter (`[review.refine]`, `[review.Agent.review]` for a method), `[exec]` of an engine setting, and absent for a program argument; an engine setting has no `@opt-env` or `@inherit`, and a source write to it outranks every tier from its program point onward. For a call omitting `sandbox`, the agent's `[agent]` `sandbox` sits just above `[exec]` (`ParamValueTiers.from_manifest` tells manifest `[config]` apart; [agents.md](agents.md)).

- `config/engine_keys.py` is the pure catalog of engine settings (kind, default, path-ness, validity rule); the host parameters (`runtime/engine_config.py:engine_setting_params`), the checker, IR validation, and the evaluator all derive from it. `PipelineDriver.preflight_arguments` ranks them with the other host parameters (`runtime/param_resolution.py`) and returns one `EngineSettings` (values plus each key's winning `ParamTier`); a configured `Option[text]` setting reads a blank or non-positive value as an explicit none (`HostParamInfo.config_option_text`; `core/parse.py:normalize_option_text` is the one rule). Settings decode onto the standard identity through `type_schema.py:engine_setting_decoders`, built over the reserved defs carrying the loaded `builtin` declarations' `@name` spellings (`typecheck/builtin_contracts.py:loaded_builtin_names`): lowering stores them on the executable, package validation builds them from its resolutions. `agm repl` has no program: `host_params/config.py:programless_engine_values` runs the same declaration-route resolution and the same `resolve_host_params` over its CLI flags and `[exec]`, so there is one ranking and decoding path, and `[exec]` keys no one declares warn through the same claims index; it decodes `DECLARATION_VALUED_ENGINE_KEYS` (`agent`, `sandbox`) only after the session loads its library image. Package validation decodes manifest engine leaves through that path too (`host_params/projection.py:decode_engine_config_value`). `cli_support/engine_seeds.py` derives `trace` and the trace decision from the ranks (`trace_decision` is the only copy of the rule); a shadowing parameter takes the bare flag and the bare program-route leaf, leaving the engine setting its qualified spelling and `[exec]`.
- `config/general.py::warning_levels_from_merged` validates `[warnings]` (an unknown name or level is a host configuration error); `agm/cli_support/warning_levels.py` resolves flags over it into the invocation's `WarningPolicy` ([agl/hosting.md](agl/hosting.md)). Registered commands pass no flags. `warnings` is a schema section, never a module route.
- `config/qualified_keys.py` routes a table to a module by path suffix. A package-registered program is also addressed by its command path (`[dv.review]`), with each path prefix as a lower-specificity inherited group table. AgM's own section names are excluded from module routing. A table at a leaf position is a nested route, not a value, when it is the table of a command registered by any mounted package (as declared, the selected program's owner completed from source) or a route table of a declaration or the program (the tables of every claimed route, plus caller-supplied `nested_routes`/`command_tables`); one `ConfigSource` applies that predicate to value resolution, ambiguity checks and warnings alike, so other tables are native values everywhere.
- `config/claims.py` is the one claims index, `table → leaf → claimants` over the program, declaration (`[exec]` included) and manifest routes (`host_params/config.py` builds it); a `ConfigSource` scan of the leaves actually set yields the values (each claimant's leaf per specificity tier), the ambiguity errors (a set leaf claimed by several distinct parameters on any of their table tiers) and the undeclared-key warnings (a set leaf no claim consumes); each resolved value carries its layer's `PathAnchor`. `qualified_keys.py:resolve_route_leaf`/`resolve_manifest_leaf` resolve one leaf of a route through it (`@inherit`).
- `host_params/config.py` is the one resolver of every host parameter kind, keyed by `HostParamKey` (`agl/ir/static_keys.py`: a binding key, which also identifies engine settings, or a `FunctionParamKey`, which identifies a function parameter, a program's own arguments included); `PipelineDriver.preflight_arguments` merges its tiers with `@config` ([agl/hosting.md](agl/hosting.md)). `host_params/inherit.py` resolves `@inherit` addresses: it splits each (`split_command_address`) at the longest command or alias path registered by a mounted package (the run's `command_registry`, `packages/command_registry.py`, built on first use from the mounted packages with command tables completed from source, so a run without `@inherit` scans no other checkout) and reads the remainder as that command's leaf from its program tables, then its package's manifest `[config]` — never its CLI, environment, `@config`, `@inherit` or declaration routes, so inheritance is not transitive; every unregistered address warns, whatever tier covers the parameter, and yields nothing. `host_params/config.py` builds the claims index (above): program arguments and every reached or declared parameter claim their spellings, and the execution options (`EXEC_OPTION_KEYS`, `config/general.py`, also the source of the CLI execution options) are declare-only claims on `[exec]`; unused-key diagnostics treat registered command tables as program routes, even when a declaration route collides, and a `program def`'s declaration route declares all its arguments. `ConfigRoute` (`config/qualified_keys.py`) is the one source of a declaration's table paths and specificity tiers (`table_tiers`, which feed the index); an engine setting's route is a fixed top-level `section`, not a module route. `commands/exec_program.py` passes the resolved tiers to preflight; [agl/hosting.md](agl/hosting.md) describes how hosts seed the resolved values.

## Sandbox Configuration

Sandbox settings are per-command JSON profiles discovered in the AgM home, the project config directory, and the invocation directory's `.sandbox/`, merged across them, and shared by `agm run` and AgL sandboxed calls ([sandbox.md](sandbox.md)).

## Code Entry Points

- `src/agnette/config/context.py` — config context, project discovery, per-invocation memo; `home.py` — AgM home resolution (a leaf).
- `src/agnette/config/general.py` — layer loading, merging, and per-feature readers.
- `src/agnette/config/engine_keys.py`, `qualified_keys.py`, `claims.py` — engine-key catalog; qualified AgL key routing; the claims index.
- `src/agnette/config/module_roots.py` — AgL module roots; `src/agnette/stdlib_locator.py` — the shipped stdlib tree.
- `src/agnette/config/sandbox/` — sandbox settings discovery and merging.
- `src/agnette/agm/commands/config/` — the `agm config` commands; `config/` (repository root) — templates and editor integrations.
- `tools/install_highlighting.py`, `tools/install_emacs_mode.py` — generated definitions, Pandoc defaults and Emacs packaging.
