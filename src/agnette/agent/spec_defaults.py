"""Configured defaults for agent specifications: Pi provider, model, reasoning effort, sandbox.

Host policy applied when an agent is used, never when its AgL value is built;
``Session::default().agent`` alone reports the resolved spec it was opened with.
Each empty field of a native spec, in order (provider, model, ``thinking``),
takes its setting (``thinking`` reads ``effort``) from the most specific
``[agent.<cli>...]`` table on the path named by the already-resolved fields
before it that sets it. A setting of ``""`` stops the lookup and passes no flag.
``AgentCommand`` and non-empty fields are never touched. ``sandbox`` is the host
``AgentSandbox`` text of a resolved native spec (`configured_sandbox`), decoded by AgL hosts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeAlias, cast

from agnette.agent.spec import (
    NATIVE_AGENT_SPECS,
    AgentCommand,
    AgentSpec,
    model_fields,
    payload_items,
)
from agnette.core.toml import TomlDict, toml_dict

#: Maps the spec an agent value decodes to onto the spec actually dispatched.
AgentSpecResolver: TypeAlias = Callable[[AgentSpec], AgentSpec]

#: The config key supplying each spec field's default.
_SETTING_KEYS: Mapping[str, str] = MappingProxyType({"thinking": "effort"})

#: Text settings every ``[agent.<cli>...]`` table allows besides its CLI's name fields.
_TABLE_KEYS = frozenset({"effort", "sandbox"})

#: Each CLI's name fields, outermost first; ``[agent.<cli>]`` nests one table level per field.
_CLI_NAME_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {spec_cls.CLI_NAME: model_fields(spec_cls) for spec_cls in NATIVE_AGENT_SPECS}
)


class AgentConfigError(ValueError):
    """The ``[agent]`` config section is malformed."""


@dataclass(frozen=True)
class AgentConfig:
    """The validated ``[agent]`` section."""

    table: TomlDict

    @classmethod
    def from_merged(cls, merged: TomlDict) -> AgentConfig:
        """Validate the ``[agent]`` section of *merged* config, or raise `AgentConfigError`."""
        section = merged.get("agent", {})
        if not isinstance(section, dict):
            raise AgentConfigError(f"agent must be a table of {_cli_names()} tables")
        table = toml_dict(section)
        for cli, value in table.items():
            if cli not in _CLI_NAME_FIELDS or not isinstance(value, dict):
                raise AgentConfigError(f"agent.{cli} must be one of the tables {_cli_names()}")
            _validate_table(toml_dict(value), ("agent", cli), _CLI_NAME_FIELDS[cli])
        return cls(table)

    def setting(self, cli: str, names: tuple[str, ...], key: str) -> str | None:
        """*key* of the most specific ``[agent.<cli>.<names prefix>]`` table setting it.

        Prefixes containing an empty name address no table; ``None`` when none sets *key*.
        """
        for depth in range(len(names), -1, -1):
            if not all(names[:depth]):
                continue
            table = self.table
            for name in (cli, *names[:depth]):
                table = toml_dict(table.get(name))
            value = table.get(key)
            if isinstance(value, str):
                return value
        return None

    def sandbox_settings(self) -> dict[str, str]:
        """Every non-empty ``sandbox`` setting's text, by dotted config key."""
        settings: dict[str, str] = {}

        def collect(table: TomlDict, path: tuple[str, ...]) -> None:
            for key, value in table.items():
                if isinstance(value, dict):
                    collect(toml_dict(value), (*path, key))
                elif key == "sandbox" and value:
                    settings[".".join((*path, key))] = cast(str, value)

        collect(self.table, ("agent",))
        return settings


def _cli_names() -> str:
    return ", ".join(sorted(_CLI_NAME_FIELDS))


def _validate_table(table: TomlDict, path: tuple[str, ...], name_fields: tuple[str, ...]) -> None:
    """Allow text ``effort``, ``sandbox`` and *name_fields* settings; one name table per field."""
    for key, value in table.items():
        key_path = ".".join((*path, key))
        if isinstance(value, dict):
            if not name_fields:
                raise AgentConfigError(f"{key_path} nests too deeply")
            _validate_table(toml_dict(value), (*path, key), name_fields[1:])
        elif key not in _TABLE_KEYS and key not in name_fields:
            raise AgentConfigError(f"{key_path} is not a known setting")
        elif not isinstance(value, str):
            raise AgentConfigError(f"{key_path} must be text")


def with_configured_defaults(spec: AgentSpec, config: AgentConfig) -> AgentSpec:
    """*spec* with each empty field filled from its configured default, outermost first."""
    if isinstance(spec, AgentCommand):
        return spec
    values = payload_items(spec)
    fields = (*model_fields(type(spec)), "thinking")
    for depth, field in enumerate(fields):
        if values[field]:
            continue
        names = tuple(values[name] for name in fields[:depth])
        setting = config.setting(spec.CLI_NAME, names, _SETTING_KEYS.get(field, field))
        if setting is not None:
            values[field] = setting
    return type(spec)(**values)


def configured_sandbox(spec: AgentSpec, config: AgentConfig) -> str | None:
    """The ``sandbox`` text of the most specific table on resolved *spec*'s names.

    ``None`` for ``AgentCommand``, or when no table sets it or the most specific one sets ``""``.
    """
    if isinstance(spec, AgentCommand):
        return None
    values = payload_items(spec)
    names = tuple(values[name] for name in model_fields(type(spec)))
    return config.setting(spec.CLI_NAME, names, "sandbox") or None


def configured_defaults_resolver(merged: TomlDict) -> AgentSpecResolver:
    """The `with_configured_defaults` resolver for *merged* config; raises `AgentConfigError`."""
    config = AgentConfig.from_merged(merged)
    return lambda spec: with_configured_defaults(spec, config)
