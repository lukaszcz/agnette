"""Shared assertions over rendered CLI help."""

from __future__ import annotations

from collections.abc import Collection

import agnette.agm.cli as cli
from agnette.agm.cli_support.execution_options import EXECUTION_OPTION_SPECS
from agnette.config.engine_keys import ENGINE_KEY_NAMES

#: Every flag spelling of the shared execution options.
EXECUTION_OPTION_FLAGS = frozenset(
    flag
    for spec in EXECUTION_OPTION_SPECS
    for declaration in spec.declarations
    for flag in declaration.split("/")
)


def execution_options_section(text: str) -> str:
    """Return the body of *text*'s ``Execution options`` section."""
    _, heading, rest = text.partition("Execution options:\n")
    assert heading, "no Execution options section"
    section, _, _ = rest.partition("\n\n")
    return section


def execution_options_is_last(text: str) -> bool:
    """Report whether *text*'s ``Execution options`` section ends it."""
    return "\n\n" not in text.partition("Execution options:\n")[2].rstrip("\n")


def assert_lists_execution_options(text: str) -> None:
    """Assert *text*'s ``Execution options`` section lists exactly the shared flags."""
    section = execution_options_section(text)
    listed = {token for token in section.split() if token.startswith("--")}
    assert listed == EXECUTION_OPTION_FLAGS
    assert "--help" not in section


def print_exec_help(
    *,
    tokens: list[str],
    file: str | None,
    code: str | None,
    program: str | None = None,
    module_paths: list[str] | None = None,
    no_stdlib: bool = False,
) -> bool:
    """Print the help *tokens* request, through the discovery ``agm exec`` shares."""
    from agnette.agm.cli_support.program_discovery import ExecProgramDiscovery

    discovery = ExecProgramDiscovery(
        code=code,
        requested_program=program,
        module_paths=module_paths,
        no_stdlib=no_stdlib,
    )
    return cli._exec_print_help(discovery, tokens=tokens, file=file, code=code, program=program)


def engine_settings_section(text: str) -> str:
    """Return the body of *text*'s ``Engine settings`` section."""
    _, heading, rest = text.partition("Engine settings:\n")
    assert heading, "no Engine settings section"
    section, _, _ = rest.partition("\n\n")
    return section


#: The engine settings that also have a negative flag, rendered beside the positive one.
_NEGATABLE_ENGINE_SETTINGS = ("trace", "strict-json", "timeout", "debug")


def assert_lists_engine_settings(text: str, *, qualified: Collection[str] = ()) -> None:
    """Assert *text*'s ``Engine settings`` section lists every engine setting.

    Each setting is listed under its bare flag, or, for a name in *qualified*,
    under its ``--std.config.`` flag; a negatable one lists its negative beside it.
    """

    def flag(name: str, prefix: str = "") -> str:
        return f"--{prefix}std.config.{name}" if name in qualified else f"--{prefix}{name}"

    tokens = engine_settings_section(text).split()
    listed = {
        t
        for t, before in zip(tokens, ["", *tokens], strict=False)
        if t.startswith("--") and before != "/"
    }
    negatives = {t for t, before in zip(tokens, ["", *tokens], strict=False) if before == "/"}
    assert listed == {flag(name) for name in ENGINE_KEY_NAMES}
    assert negatives == {flag(name, "no-") for name in _NEGATABLE_ENGINE_SETTINGS}
