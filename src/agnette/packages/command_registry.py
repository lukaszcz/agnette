"""The commands the mounted packages register, by command or alias path."""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from agnette.packages.manifest import (
    CommandSpec,
    command_paths_for_program,
    expanded_commands,
    split_program_reference,
)
from agnette.packages.model import PackageInfo

__all__ = [
    "CommandRegistry",
    "ProgramTarget",
    "RegisteredCommand",
    "command_registry",
    "split_command_address",
]


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


class ProgramTarget(NamedTuple):
    """A registered command's program, addressed as its own command resolves it."""

    package: PackageInfo
    module_segments: tuple[str, ...]
    declaration_path: tuple[str, ...]
    command_paths: tuple[tuple[str, ...], ...]


def _program_target(registered: RegisteredCommand) -> ProgramTarget | None:
    """Return the program *registered* runs, or ``None`` for a group or malformed reference."""
    reference = registered.spec.program
    if reference is None:
        return None
    try:
        module_segments, declaration_path = split_program_reference(reference)
    except ValueError:
        return None
    command_paths = command_paths_for_program(registered.package.manifest, reference)
    return ProgramTarget(registered.package, module_segments, declaration_path, command_paths)


def split_command_address(
    address: str, registry: CommandRegistry
) -> tuple[ProgramTarget, str] | None:
    """Split a dotted *address* at its longest command or alias path that runs a program.

    The non-empty remainder is the leaf; ``None`` when no such prefix exists.
    """
    words = tuple(address.split("."))
    for length in range(len(words) - 1, 0, -1):
        registered = registry.get(words[:length])
        target = None if registered is None else _program_target(registered)
        if target is not None:
            return target, ".".join(words[length:])
    return None
