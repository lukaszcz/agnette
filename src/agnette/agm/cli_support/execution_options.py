"""Shared declarations and help display for the AgL options that are not engine settings.

Engine settings are host parameters (``host_params/surface.py``), not
execution options.
"""

from __future__ import annotations

from dataclasses import dataclass

import click

from agnette.config.general import EXEC_OPTION_KEYS

#: Widest option label kept on its description's line, at most half the help width.
_LABEL_COLUMN_MAX = 34


@dataclass(frozen=True, slots=True)
class ExecutionOptionSpec:
    """One shared execution option's Click declaration and help grouping."""

    name: str
    declarations: tuple[str, ...]
    metavar: str | None
    group: str


@dataclass(frozen=True, slots=True)
class _HelpGroup:
    """One help row: the grouped options' labels, joined by `` / ``, and their help."""

    key: str
    description: str


#: Click declaration, metavar, and help group of each ``[exec]`` option key.
_DECLARATIONS: dict[str, tuple[tuple[str, ...], str | None, str]] = {
    "echo": (("--echo/--no-echo",), None, "echo"),
    "max-call-depth": (("--max-call-depth",), "N", "call_depth"),
}

EXECUTION_OPTION_SPECS: tuple[ExecutionOptionSpec, ...] = tuple(
    ExecutionOptionSpec(key.replace("-", "_"), *_DECLARATIONS[key]) for key in EXEC_OPTION_KEYS
)

_SPEC_BY_NAME = {spec.name: spec for spec in EXECUTION_OPTION_SPECS}
#: Every execution option's parameter name; ``agm exec``, ``agm repl``, and
#: registered commands all accept each one.
EXECUTION_OPTION_NAMES: frozenset[str] = frozenset(_SPEC_BY_NAME)
_HELP_GROUPS: tuple[_HelpGroup, ...] = (
    _HelpGroup("echo", "Echo agent turn output to stderr."),
    _HelpGroup("call_depth", "Maximum call depth."),
)


def execution_option_spec(name: str) -> ExecutionOptionSpec:
    return _SPEC_BY_NAME[name]


def execution_option_flags() -> frozenset[str]:
    return frozenset(
        flag
        for spec in EXECUTION_OPTION_SPECS
        for declaration in spec.declarations
        for flag in declaration.split("/")
    )


def _execution_option_rows() -> tuple[tuple[str, str], ...]:
    """Return one ``(options, help)`` row per help group."""
    rows: list[tuple[str, str]] = []
    for group in _HELP_GROUPS:
        labels = [
            " ".join(filter(None, (spec.declarations[0].replace("/", " / "), spec.metavar)))
            for spec in EXECUTION_OPTION_SPECS
            if spec.group == group.key
        ]
        rows.append((" / ".join(labels), group.description))
    return tuple(rows)


def write_execution_options(formatter: click.HelpFormatter) -> None:
    """Write the ``Execution options`` section to *formatter*."""
    with formatter.section("Execution options"):
        formatter.write_dl(
            _execution_option_rows(), col_max=min(_LABEL_COLUMN_MAX, formatter.width // 2)
        )


def execution_options_section(*, width: int) -> str:
    """Render the ``Execution options`` section, wrapped to *width*."""
    formatter = click.HelpFormatter(width=width)
    write_execution_options(formatter)
    return formatter.getvalue()
