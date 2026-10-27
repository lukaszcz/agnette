"""General TOML-backed AgM configuration helpers."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Generic, NamedTuple, TypeVar

from agnette.agl.warnings import WarningLevel, warning_level_setting
from agnette.agm.project.layout import project_config_dir
from agnette.config.engine_keys import ENGINE_KEY_KINDS, ENGINE_KEY_NAMES, PATH_ENGINE_KEYS
from agnette.config.home import agm_home_dir, agm_path_candidates
from agnette.core.fs import mkdir, write_text
from agnette.core.parse import parse_timeout as parse_timeout
from agnette.core.toml import (
    TomlDict,
    dumps_toml,
    empty_toml_doc,
    load_toml_doc,
    load_toml_file,
    set_toml_table_value,
    toml_dict,
)
from agnette.util.interp import interp_preserving

_T = TypeVar("_T")
_K = TypeVar("_K")
_D = TypeVar("_D")


class PathAnchor(NamedTuple):
    """Where a relative path value resolves: its defining directory, else *cwd*."""

    config_dir: Path
    cwd: Path


@dataclass(frozen=True)
class GeneralConfig:
    """Path-normalized general configuration and its precedence-ordered layers.

    ``layers`` run from least to most specific and retain the provenance that
    qualified AgL config lookup needs when equivalent table aliases differ
    between files. ``merged`` preserves the ordinary key-wise view consumed by
    existing general-config readers. ``anchors`` holds each layer's
    :class:`PathAnchor` (``None``: values stay as written) so a value read
    from a layer later, once its type is known to be a path, anchors to its
    defining file.
    """

    layers: tuple[TomlDict, ...]
    merged: TomlDict
    anchors: tuple[PathAnchor | None, ...]

    @classmethod
    def from_layers(
        cls, layers: Iterable[TomlDict], anchors: Iterable[PathAnchor | None]
    ) -> GeneralConfig:
        """Build a config view from normalized layers in precedence order.

        *anchors* run parallel to *layers*.
        """

        ordered_layers = tuple(layers)
        merged: TomlDict = {}
        for layer in ordered_layers:
            merged = _merge_config(merged, layer)
        return cls(layers=ordered_layers, merged=merged, anchors=tuple(anchors))


#: The config table holding engine settings and execution options.
EXEC_CONFIG_SECTION = "exec"

# Known path-like fields per config section. Values interpolate ``%{name}``
# environment variables, expand ``~``, and resolve against the config file's directory
# before merging, so that relative paths are always interpreted relative to
# the config file that defines them.  When the config-dir-resolved path does
# not exist, cwd is used as a fallback.
_CONFIG_PATH_FIELDS: dict[str, Sequence[str]] = {
    # ``[exec]`` carries exactly the path-valued engine keys.
    EXEC_CONFIG_SECTION: PATH_ENGINE_KEYS,
}


def _merge_config(base: TomlDict, override: TomlDict) -> TomlDict:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_config(toml_dict(existing), toml_dict(value))
            continue
        merged[key] = value
    return merged


def config_file_candidates(
    *,
    home: Path,
    proj_dir: Path | None,
    cwd: Path | None,
    env: Mapping[str, str] | None = None,
) -> list[Path]:
    """Config files from least to most specific: install prefix, AgM home, project, *cwd*.

    A *cwd* of ``None`` omits the invocation-directory layer.
    """
    candidates = agm_path_candidates(home=home, relative_path=Path("config.toml"), env=env)
    if proj_dir is not None:
        candidates.append(project_config_dir(proj_dir) / "config.toml")
    if cwd is not None:
        candidates.append(cwd / ".agm" / "config.toml")
    return candidates


@dataclass(frozen=True)
class CommandSetting(Generic[_T]):
    """One ``[run]`` setting: a default plus its per-command overrides."""

    default: _T
    overrides: Mapping[str, _T]

    def for_command(self, command_name: str) -> _T:
        return self.overrides.get(command_name, self.default)


@dataclass(frozen=True)
class RunConfig:
    """Resolved run-command configuration: one shape per ``[run]`` setting."""

    alias: CommandSetting[str | None]
    memory: CommandSetting[str | None]
    swap: CommandSetting[str | None]
    pty: CommandSetting[bool]

    def alias_for(self, command_name: str) -> str | None:
        return self.alias.for_command(command_name)

    def memory_limit_for(self, command_name: str | None) -> str | None:
        if command_name is None:
            return self.memory.default
        return self.memory.for_command(command_name)

    def swap_limit_for(self, command_name: str | None) -> str | None:
        return self.swap.default if command_name is None else self.swap.for_command(command_name)

    def pty_for(self, command_name: str) -> bool:
        return self.pty.for_command(command_name)


def _anchor_path(value: str, config_dir: Path, cwd: Path) -> str:
    """Interpolate ``%{name}``, expand ``~`` in, and anchor the path *value*.

    A relative path resolves against *config_dir* when it exists there, else
    against *cwd*. A blank value, or one with unresolved holes, is not a usable
    path and is not anchored.
    """
    if not value.strip():
        return value
    interpolated, unresolved = interp_preserving(value, os.environ)
    expanded = os.path.expanduser(interpolated)
    path = Path(expanded)
    if unresolved or path.is_absolute():
        return expanded
    config_resolved = (config_dir / path).resolve()
    return str(config_resolved if config_resolved.exists() else (cwd / path).resolve())


def resolve_section_paths(
    section: TomlDict,
    fields: Sequence[str],
    config_dir: Path,
    cwd: Path,
) -> TomlDict:
    """Interpolate and anchor *section*'s path-like *fields* to *config_dir*.

    Config-file loading anchors a known section's path fields to the file's own
    directory, so a relative path is always read against the file defining it,
    else *cwd*.
    """
    resolved = dict(section)
    for key, value in section.items():
        if key in fields and isinstance(value, str):
            resolved[key] = _anchor_path(value, config_dir, cwd)
        elif isinstance(value, dict):
            resolved[key] = resolve_section_paths(toml_dict(value), fields, config_dir, cwd)
    return resolved


def anchor_path_value(value: object, anchor: PathAnchor | None) -> object:
    """Return *value*, a configured path, interpolated and anchored at *anchor*.

    The resolution a known path field gets at load time, for a value whose
    type is only known to be a path once read; a value that is no text, or has
    no *anchor*, is returned as written.
    """
    if anchor is None or not isinstance(value, str):
        return value
    return _anchor_path(value, anchor.config_dir, anchor.cwd)


def _resolve_config_file_paths(config: TomlDict, config_dir: Path, cwd: Path) -> TomlDict:
    """Anchor the path fields of the sections that explicitly list them.

    Every other section's values are typed by their declaration, so they
    anchor only once read as paths (:func:`anchor_path_value`).
    """
    resolved = dict(config)
    for section_name, fields in _CONFIG_PATH_FIELDS.items():
        section = resolved.get(section_name)
        if isinstance(section, dict):
            resolved[section_name] = resolve_section_paths(
                toml_dict(section), fields, config_dir, cwd
            )
    return resolved


def load_general_config(
    *,
    home: Path,
    proj_dir: Path | None,
    cwd: Path | None,
    env: Mapping[str, str] | None = None,
) -> GeneralConfig:
    """Load path-normalized config layers and their conventional merged view.

    Without a *cwd* (see :func:`config_file_candidates`) a relative path resolves
    against its own config directory alone.
    """

    layers: list[TomlDict] = []
    anchors: list[PathAnchor | None] = []
    for path in config_file_candidates(home=home, proj_dir=proj_dir, cwd=cwd, env=env):
        if path.is_file():
            raw = load_toml_file(path)
            anchor = PathAnchor(path.parent, path.parent if cwd is None else cwd)
            layers.append(_resolve_config_file_paths(raw, anchor.config_dir, anchor.cwd))
            anchors.append(anchor)
    return GeneralConfig.from_layers(layers, anchors)


def load_merged_config(
    *,
    home: Path,
    proj_dir: Path | None,
    cwd: Path | None,
    env: Mapping[str, str] | None = None,
) -> TomlDict:
    """Load the conventional merged config view.

    Kept for readers that do not need per-file provenance. Qualified config
    consumers must use :func:`load_general_config` and retain its layers.
    """

    return load_general_config(home=home, proj_dir=proj_dir, cwd=cwd, env=env).merged


def _setting_value(table: TomlDict, key: str, kind: type[_K]) -> _K | None:
    """Return ``table[key]`` when it is a *kind* value, treating empty text as absent."""
    value = table.get(key)
    if isinstance(value, str) and not value:
        return None
    return value if isinstance(value, kind) else None


def _command_setting(
    run_table: TomlDict,
    key: str,
    kind: type[_K],
    *,
    default: _D,
    global_key: bool = True,
) -> CommandSetting[_K | _D]:
    """Load one ``[run]`` setting from its bare key and its per-command tables.

    The default is ``[run].<key>`` when *global_key*, falling back to
    *default*; ``[run.<command>].<key>`` overrides it for that command.
    """
    resolved: _K | _D = default
    if global_key:
        global_value = _setting_value(run_table, key, kind)
        if global_value is not None:
            resolved = global_value
    overrides: dict[str, _K | _D] = {}
    for command_name, command_config in run_table.items():
        value = _setting_value(toml_dict(command_config), key, kind)
        if value is not None:
            overrides[command_name] = value
    return CommandSetting(default=resolved, overrides=overrides)


def load_run_config(*, home: Path, proj_dir: Path | None, cwd: Path) -> RunConfig:
    merged = load_merged_config(home=home, proj_dir=proj_dir, cwd=cwd)
    run_table = toml_dict(merged.get("run"))
    return RunConfig(
        # A bare ``[run] alias`` would rename every command, so alias is
        # per-command only.
        alias=_command_setting(run_table, "alias", str, default=None, global_key=False),
        memory=_command_setting(run_table, "memory", str, default=None),
        swap=_command_setting(run_table, "swap", str, default=None),
        pty=_command_setting(run_table, "pty", bool, default=True),
    )


def _optional_str(table: TomlDict, key: str) -> str | None:
    value = table.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _optional_positive_int(table: TomlDict, key: str) -> int | None:
    """Return a positive int config value, or ``None`` when absent/invalid.

    ``None`` lets the caller apply its own canonical default rather than baking a
    default into the config layer.
    """
    value = table.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _optional_bool(table: TomlDict, key: str, *, default: bool = False) -> bool:
    value = table.get(key)
    return value if isinstance(value, bool) else default


def _optional_timeout(table: TomlDict, key: str) -> float | None:
    value = table.get(key)
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and value > 0:
        return float(value)
    if isinstance(value, str) and value.strip():
        return parse_timeout(value)
    return None


@dataclass(frozen=True)
class ExecConfig:
    """Resolved exec-command configuration."""

    # Raw TOML values (``None`` = unset): exec/repl decode the bool and
    # ``parse_error_retries`` engine keys through the shared host-value decoder.
    strict_json: object | None
    timeout: float | None
    trace: object | None
    trace_file: str | None
    # Raw TOML value (a string or a native table): exec/repl decode it as a
    # host Agent value through the shared host-value decoder.
    agent: object | None = None
    # Raw TOML value (a string or a native table): exec/repl decode it as a
    # host AgentSandbox value through the shared host-value decoder.
    sandbox: object | None = None
    # Keep debugging artifacts (``std/fs`` temporary paths) past the host session.
    debug: object | None = None
    parse_error_retries: object | None = None


_ECHO_KEY = "echo"
_MAX_CALL_DEPTH_KEY = "max-call-depth"
#: The ``[exec]`` keys that are execution options rather than engine settings.
EXEC_OPTION_KEYS: tuple[str, ...] = (_ECHO_KEY, _MAX_CALL_DEPTH_KEY)


@dataclass(frozen=True)
class ExecOptions:
    """The ``[exec]`` options that are not engine settings."""

    # Echo streamed agent output on the host's stderr channel.
    echo: bool
    # Optional recursion call-depth override (None = use the canonical default).
    max_call_depth: int | None


def exec_options_from_merged(merged: TomlDict) -> ExecOptions:
    """Read the non-engine ``[exec]`` options of an already-merged config dict.

    Never fails: an engine setting is validated where it is decoded, with every
    other host parameter, so a malformed one does not surface here.
    """
    exec_table = toml_dict(merged.get(EXEC_CONFIG_SECTION))
    return ExecOptions(
        echo=_optional_bool(exec_table, _ECHO_KEY),
        max_call_depth=_optional_positive_int(exec_table, _MAX_CALL_DEPTH_KEY),
    )


def undeclared_exec_keys(merged: TomlDict) -> tuple[str, ...]:
    """Return the sorted ``[exec]`` keys of *merged* that are no engine setting or option."""
    declared = ENGINE_KEY_NAMES | frozenset(EXEC_OPTION_KEYS)
    return tuple(
        sorted(key for key in toml_dict(merged.get(EXEC_CONFIG_SECTION)) if key not in declared)
    )


def exec_config_from_merged(
    merged: TomlDict,
    *,
    program_table: dict[str, object] | None = None,
    package_table: dict[str, object] | None = None,
) -> ExecConfig:
    """Build :class:`ExecConfig` from an already-merged config dict.

    Every exec host already holds a merged config, so the ``[exec]`` section is
    derived from it rather than re-read. ``[exec]`` has no named sub-tables: a
    per-program override comes from the program's own qualified table, because
    an AgL program is identified by its module route rather than by a flat
    name the way ``[run.<command>]``'s settings are.

    When *program_table* is supplied, each engine key present in that already
    resolved qualified program table overrides the global ``[exec]`` value.
    *package_table* — a package-owned program's manifest ``[config]`` values —
    ranks between ``[exec]`` and *program_table*. Engine keys use kebab-case
    names: ``strict-json``, ``trace-file``.
    """
    exec_table = toml_dict(merged.get(EXEC_CONFIG_SECTION))

    # Effective precedence, low to high: [exec] < package manifest < the
    # qualified program table (exact route, then inherited groups).
    effective: TomlDict = dict(exec_table)
    for table in (package_table, program_table):
        if table is None:
            continue
        for key, _ in ENGINE_KEY_KINDS:
            if key in table:
                effective[key] = table[key]

    resolved_strict_json = effective.get("strict-json")

    resolved_timeout = _optional_timeout(effective, "timeout")

    resolved_trace = effective.get("trace")
    resolved_trace_file = _optional_str(effective, "trace-file")
    resolved_debug = effective.get("debug")
    # Keep every explicitly supplied engine value raw (``agent`` and
    # ``sandbox`` may be a native TOML table) so exec/repl decode and
    # validate it through the shared host-value decoder at their AgL host
    # boundary; other commands stay free of AgL imports.
    resolved_agent = effective.get("agent")
    resolved_sandbox = effective.get("sandbox")
    resolved_parse_error_retries = effective.get("parse-error-retries")

    return ExecConfig(
        strict_json=resolved_strict_json,
        timeout=resolved_timeout,
        trace=resolved_trace,
        trace_file=resolved_trace_file,
        agent=resolved_agent,
        sandbox=resolved_sandbox,
        debug=resolved_debug,
        parse_error_retries=resolved_parse_error_retries,
    )


def warning_levels_from_merged(merged: TomlDict) -> dict[str, WarningLevel]:
    """Read the ``[warnings]`` table of *merged*: static-warning levels by name.

    Layers have merged key by key already. An unknown warning name, or a level
    other than ``allow``/``warn``/``deny``, raises :class:`ValueError`.
    """
    table = merged.get("warnings", {})
    if not isinstance(table, dict):
        raise ValueError("[warnings] must be a table of warning names to levels")
    levels: dict[str, WarningLevel] = {}
    for name, value in table.items():
        try:
            levels[name] = warning_level_setting(name, value)
        except ValueError as exc:
            raise ValueError(f"[warnings] {exc}") from exc
    return levels


@dataclass(frozen=True)
class ReplConfig:
    """Resolved REPL configuration."""

    theme: str
    echo: bool = True
    echo_unit: bool = False


def load_repl_config(*, home: Path, proj_dir: Path | None, cwd: Path) -> ReplConfig:
    """Load ``[repl]`` configuration, merging all config layers."""
    merged = load_merged_config(home=home, proj_dir=proj_dir, cwd=cwd)
    section = toml_dict(merged.get("repl", {}))
    theme = section.get("theme", "auto")
    if not isinstance(theme, str) or theme not in ("dark", "light", "auto"):
        theme = "auto"
    return ReplConfig(
        theme=theme,
        echo=_optional_bool(section, "echo", default=True),
        echo_unit=_optional_bool(section, "echo-unit", default=False),
    )


def save_repl_setting(key: str, value: str | bool, *, home: Path) -> None:
    """Persist one ``[repl]`` setting (``theme``, ``echo``, ``echo-unit``, …) to config.toml.

    Single generic save path for every REPL setting: writes the raw TOML
    value as-is (a ``bool`` round-trips as a TOML boolean, a ``str`` as a TOML
    string), always to the home-level ``config.toml``.
    """
    path = agm_home_dir(home=home) / "config.toml"
    doc = load_toml_doc(path) if path.is_file() else empty_toml_doc()
    set_toml_table_value(doc, "repl", key, value)
    mkdir(path.parent)
    write_text(path, dumps_toml(doc), encoding="utf-8")
