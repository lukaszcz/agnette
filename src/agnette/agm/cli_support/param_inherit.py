"""``@inherit``: parameter defaults taken from other commands' configured values."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from agnette.agl.ir.static_keys import HostParamKey
from agnette.config.general import GeneralConfig, PathAnchor
from agnette.config.qualified_keys import (
    ConfiguredValue,
    QualifiedConfigKey,
    known_route_tables,
    resolve_manifest_values,
    resolve_qualified_values,
)
from agnette.packages.command_registry import (
    CommandRegistry,
    ProgramTarget,
    split_command_address,
)

__all__ = [
    "InheritRequest",
    "InheritScope",
    "InheritedConfigValue",
    "resolve_inherited_values",
]


@dataclass(frozen=True)
class InheritScope:
    """The command registry ``@inherit`` addresses resolve in, and the invocation directory."""

    registry: CommandRegistry
    cwd: Path


class InheritRequest(NamedTuple):
    """One parameter's ``@inherit`` addresses, in written order, and its display *label*."""

    label: str
    addresses: tuple[str, ...]


class InheritedConfigValue(NamedTuple):
    """The configured value of the first address that yielded one, with that address."""

    configured: ConfiguredValue
    address: str


def _key(target: ProgramTarget, leaf: str) -> QualifiedConfigKey:
    """Return the program-route key of *leaf* for *target*."""
    return QualifiedConfigKey(
        target.module_segments, target.declaration_path, leaf, target.command_paths
    )


def _target_value(
    config: GeneralConfig,
    target: ProgramTarget,
    leaf: str,
    command_tables: frozenset[tuple[str, ...]],
    command_routes: frozenset[tuple[str, ...]],
    cwd: Path,
) -> ConfiguredValue | None:
    """Return *leaf*'s configured value for *target*: its program route, else its manifest.

    The target's CLI, environment, ``@config``, ``@inherit`` and declaration
    routes never contribute, so inheritance is not transitive. Nor does the
    inheriting program: only registered command tables (*command_routes*) are routes.
    """
    key = _key(target, leaf)
    # A leaf matching several routes is an error of the target program alone, so
    # each address is resolved once, independent of the inheriting program.
    found = resolve_qualified_values(config, (key,), command_routes).get(key)
    if found is None and target.package.manifest.config:
        found = resolve_manifest_values(
            target.package.manifest.config,
            (key,),
            PathAnchor(target.package.root, cwd),
            command_tables,
        ).get(key)
    return found


def resolve_inherited_values(
    config: GeneralConfig,
    requests: Mapping[HostParamKey, InheritRequest],
    scope: InheritScope,
) -> dict[HostParamKey, InheritedConfigValue]:
    """Resolve each request's first address that yields a configured value.

    An address is split at the longest prefix registered as a command or alias
    path in *scope*; one with no such prefix draws a warning and contributes
    nothing. Every unregistered address warns, though one already yielded a value.
    """
    command_tables = frozenset(scope.registry)
    command_routes = known_route_tables((), command_tables)
    resolved: dict[HostParamKey, InheritedConfigValue] = {}
    for key, request in requests.items():
        for address in request.addresses:
            split = split_command_address(address, scope.registry)
            if split is None:
                print(
                    f"warning: @inherit address '{address}' of parameter {request.label} "
                    "names no registered command and will be ignored",
                    file=sys.stderr,
                )
                continue
            if key in resolved:
                continue
            target, leaf = split
            found = _target_value(config, target, leaf, command_tables, command_routes, scope.cwd)
            if found is not None:
                resolved[key] = InheritedConfigValue(found, address)
    return resolved
