"""The commands the mounted packages register, by command or alias path."""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from agnette.packages.manifest import CommandSpec, expanded_commands
from agnette.packages.model import PackageInfo

__all__ = ["CommandRegistry", "RegisteredCommand", "command_registry"]


class RegisteredCommand(NamedTuple):
    """A registered command or group, with the package that registers it."""

    package: PackageInfo
    spec: CommandSpec


type CommandRegistry = dict[tuple[str, ...], RegisteredCommand]


def command_registry(packages: Iterable[PackageInfo]) -> CommandRegistry:
    """Map every command, group, and alias path (aliases expanded) of *packages* to its owner.

    The first package registering a path wins, as in the mounted selection's
    order. Each package's command table should already be complete.
    """
    registry: CommandRegistry = {}
    for package in packages:
        for path, spec in expanded_commands(package.manifest).items():
            registry.setdefault(tuple(path.split()), RegisteredCommand(package, spec))
    return registry
