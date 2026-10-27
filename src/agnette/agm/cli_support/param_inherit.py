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
from agnette.packages.command_registry import CommandRegistry, RegisteredCommand
from agnette.packages.manifest import command_paths_for_program, split_program_reference
from agnette.packages.model import PackageInfo

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


@dataclass(frozen=True)
class _Target:
    """A registered command's program, addressed as its own command resolves it."""

    package: PackageInfo
    module_segments: tuple[str, ...]
    declaration_path: tuple[str, ...]
    command_paths: tuple[tuple[str, ...], ...]

    def key(self, leaf: str) -> QualifiedConfigKey:
        """Return the program-route key of *leaf*."""
        return QualifiedConfigKey(
            self.module_segments, self.declaration_path, leaf, self.command_paths
        )


def _target_of(registered: RegisteredCommand) -> _Target | None:
    """Return the program *registered* runs, or ``None`` for a group or malformed reference."""
    reference = registered.spec.program
    if reference is None:
        return None
    try:
        module_segments, declaration_path = split_program_reference(reference)
    except ValueError:
        return None
    command_paths = command_paths_for_program(registered.package.manifest, reference)
    return _Target(registered.package, module_segments, declaration_path, command_paths)


def _split_address(address: str, registry: CommandRegistry) -> tuple[_Target, str] | None:
    """Split *address* at its longest registered command path; the rest is the target leaf."""
    words = tuple(address.split("."))
    for length in range(len(words) - 1, 0, -1):
        registered = registry.get(words[:length])
        target = None if registered is None else _target_of(registered)
        if target is not None:
            return target, ".".join(words[length:])
    return None


def _target_value(
    config: GeneralConfig,
    target: _Target,
    leaf: str,
    command_tables: frozenset[tuple[str, ...]],
    cwd: Path,
) -> ConfiguredValue | None:
    """Return *leaf*'s configured value for *target*: its program route, else its manifest.

    The target's CLI, environment, ``@config``, ``@inherit`` and declaration
    routes never contribute, so inheritance is not transitive. Nor does the
    inheriting program: only registered command tables are routes.
    """
    key = target.key(leaf)
    # A leaf matching several routes is an error of the target program alone, so
    # each address is resolved once, independent of the inheriting program.
    routes = known_route_tables((key,), command_tables)
    found = resolve_qualified_values(config, (key,), routes).get(key)
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
    resolved: dict[HostParamKey, InheritedConfigValue] = {}
    for key, request in requests.items():
        for address in request.addresses:
            split = _split_address(address, scope.registry)
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
            found = _target_value(config, target, leaf, command_tables, scope.cwd)
            if found is not None:
                resolved[key] = InheritedConfigValue(found, address)
    return resolved
