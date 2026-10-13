"""Source-selection adapter for ``agm exec``.

The shared execution pipeline lives in :mod:`agnette.agm.commands.exec_program` so
registered package commands and explicit ``agm exec`` references use identical
parameter, configuration, engine-setting, and runtime handling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agnette.agm.cli_support.args import ExecArgs
from agnette.agm.cli_support.exec_target import is_installed_reference
from agnette.agm.cli_support.program_options import ProgramHelpRequested, exec_program_help
from agnette.agm.commands import exec_program
from agnette.agm.commands.exec_program import RegisteredProgramUsageError
from agnette.agm.parser import exit_with_usage_error

if TYPE_CHECKING:
    from agnette.agl.warning_policy import WarningPolicy


def run(args: ExecArgs, *, warning_policy: "WarningPolicy | None" = None) -> None:
    """Run a file, inline source, or installed program reference under *warning_policy*.

    A help request the CLI layer did not already recognize — a ``-h`` bundled
    into a short group of the program's own — reaches the selected program's
    command here, and renders that command's help instead of running it.
    """
    try:
        if args.file is not None and is_installed_reference(args.file, code=args.code):
            exec_program.run_registered(args.file, args=args, warning_policy=warning_policy)
            return
        exec_program.run(args, warning_policy=warning_policy)
    except ProgramHelpRequested as exc:
        print(
            exec_program_help(exc.command, file=args.file, program=args.program),
            end="",
        )
    except RegisteredProgramUsageError as exc:
        exit_with_usage_error(["exec"], f"error: {exc.message}")
