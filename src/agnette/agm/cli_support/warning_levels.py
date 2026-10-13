"""CLI-over-config resolution of the static-warning policy an invocation applies.

``agm exec``, ``agm check`` and ``agm repl`` build one :class:`WarningPolicy` per
invocation from ``--warning NAME=LEVEL`` flags over the merged ``[warnings]``
table over the catalog defaults; a registered package command passes no flags,
so only configuration applies. A source ``@allow`` is applied later, by the
policy itself, and outranks both.

:func:`resolve_warning_policy` is the pure resolution. :func:`command_warning_policy`
resolves it for a command that is about to run, exiting 1 on an invalid flag (a
usage error) or an invalid ``[warnings]`` table (a host configuration error).
:func:`advisory_warning_policy` serves help and completion, which must not fail on
either: it falls back to the catalog defaults.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

from agnette.agl.warning_policy import WarningPolicy
from agnette.agl.warnings import WarningLevel, warning_level_setting
from agnette.agm.parser import exit_with_usage_error
from agnette.config.context import current_config_context
from agnette.config.general import load_merged_config, warning_levels_from_merged

if TYPE_CHECKING:
    from agnette.config.context import ConfigContext
    from agnette.core.toml import TomlDict

__all__ = [
    "WarningFlagError",
    "advisory_warning_policy",
    "command_warning_policy",
    "parse_warning_flags",
    "resolve_warning_policy",
]


class WarningFlagError(ValueError):
    """A ``--warning`` flag is malformed, unknown, or contradicts another."""


def parse_warning_flags(values: Iterable[str]) -> dict[str, WarningLevel]:
    """Parse ``NAME=LEVEL`` flag values; a name may repeat only with the same level."""
    levels: dict[str, WarningLevel] = {}
    for value in values:
        name, separator, level_text = value.partition("=")
        if not separator:
            raise WarningFlagError(f"--warning {value!r} must be NAME=LEVEL")
        try:
            level = warning_level_setting(name, level_text)
        except ValueError as exc:
            raise WarningFlagError(f"--warning {exc}") from exc
        if levels.setdefault(name, level) != level:
            raise WarningFlagError(f"--warning {name} is given two different levels")
    return levels


def resolve_warning_policy(merged: TomlDict, flags: Iterable[str] = ()) -> WarningPolicy:
    """Resolve *flags* over *merged*'s ``[warnings]`` table over the catalog defaults.

    Raises :class:`WarningFlagError` for an invalid flag and :class:`ValueError`
    for an invalid table.
    """
    flag_levels = parse_warning_flags(flags)
    overrides: Mapping[str, WarningLevel] = {**warning_levels_from_merged(merged), **flag_levels}
    return WarningPolicy(overrides)


def _merged_config(context: ConfigContext | None) -> TomlDict:
    context = current_config_context() if context is None else context
    return load_merged_config(home=context.home, proj_dir=context.proj_dir, cwd=context.cwd)


def command_warning_policy(
    command_path: Sequence[str],
    flags: Iterable[str] = (),
    *,
    context: ConfigContext | None = None,
) -> WarningPolicy:
    """Return the policy *command_path* runs under, or exit 1 on an invalid flag or table."""
    try:
        merged = _merged_config(context)
    except ValueError as exc:
        print(f"Error: invalid configuration: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    try:
        return resolve_warning_policy(merged, flags)
    except WarningFlagError as exc:
        exit_with_usage_error(command_path, f"error: {exc}")
    except ValueError as exc:
        print(f"Error: invalid warnings configuration: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def advisory_warning_policy(
    flags: Iterable[str] = (), *, context: ConfigContext | None = None
) -> WarningPolicy:
    """Return the policy for help and completion: the resolved one, else the catalog defaults.

    The command itself reports an invalid flag or table when it runs.
    """
    try:
        return resolve_warning_policy(_merged_config(context), flags)
    except ValueError:
        return WarningPolicy()
