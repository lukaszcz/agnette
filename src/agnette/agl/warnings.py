"""The catalog of named static warnings.

Every warning the AgL static passes can report has one entry: its name, the
level it is reported at unless configured otherwise, and whether a function may
declare its call results exempt from it (a warning about the value in a hole).
The catalog is data only — the analyses that raise each warning live in the
pass that owns it, and a :class:`~agnette.agl.diagnostics.Diagnostic` carries
just the entry's name. ``WARNING_CATALOG`` is the one lookup (a name is valid
iff it is a key); ``WARNING_DEFAULT_LEVELS`` and ``EXEMPTABLE_WARNINGS`` are the
derived views the level policy and ``@exempts`` validation read.

It is a top-level leaf importing nothing under ``agnette``, so any layer may
name a warning without pulling a pass in with it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

__all__ = [
    "EXEMPTABLE_WARNINGS",
    "NOOP_PARSE_RETRIES",
    "SHELL_QUOTED_HOLE",
    "TAB_INDENTATION",
    "UNQUOTED_EXEC_HOLE",
    "WARNING_CATALOG",
    "WARNING_DEFAULT_LEVELS",
    "StaticWarning",
    "WarningLevel",
]

#: How a warning is treated: ``allow`` drops it, ``warn`` reports it, ``deny`` makes it an error.
type WarningLevel = Literal["allow", "warn", "deny"]


@dataclass(frozen=True, slots=True)
class StaticWarning:
    """One catalog entry."""

    name: str
    default_level: WarningLevel = "warn"
    exemptable: bool = False


TAB_INDENTATION = StaticWarning("tab-indentation")
NOOP_PARSE_RETRIES = StaticWarning("noop-parse-retries")
UNQUOTED_EXEC_HOLE = StaticWarning("unquoted-exec-hole", exemptable=True)
SHELL_QUOTED_HOLE = StaticWarning("shell-quoted-hole")

WARNING_CATALOG = MappingProxyType(
    {
        entry.name: entry
        for entry in (TAB_INDENTATION, NOOP_PARSE_RETRIES, UNQUOTED_EXEC_HOLE, SHELL_QUOTED_HOLE)
    }
)

#: Each warning's level before any configuration.
WARNING_DEFAULT_LEVELS: Mapping[str, WarningLevel] = MappingProxyType(
    {name: entry.default_level for name, entry in WARNING_CATALOG.items()}
)

#: The warnings a function may declare its call results exempt from.
EXEMPTABLE_WARNINGS = frozenset(name for name, entry in WARNING_CATALOG.items() if entry.exemptable)
