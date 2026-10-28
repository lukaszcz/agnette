"""Render AgL exceptions and host parameter diagnostics that end a CLI command."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, NoReturn

from agnette.agm.cli_support.program_options import CommandUsageError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agnette.agl.diagnostics import Diagnostic
    from agnette.agl.runtime.types import ProgramDeclInfo


def exit_for_agl_exception(error: Exception) -> None:
    """Print a safe AgL error and exit, or return for an unrelated exception."""
    from agnette.agl.diagnostics import AglError, format_diagnostic
    from agnette.agl.runtime.boundary import AglException
    from agnette.agl.runtime.request import AgentCancelled
    from agnette.agl.semantics.exceptions import AglRaise

    if isinstance(error, AglError):
        message = "\n".join(format_diagnostic(d) for d in error.diagnostics())
        exit_code = 1
    elif isinstance(error, AgentCancelled):
        message = "Agent call cancelled — execution aborted."
        exit_code = 130
    elif isinstance(error, (AglRaise, AglException)):
        message = f"AgL exception: {error}"
        exit_code = 2
    else:
        return

    print(message, file=sys.stderr)
    raise SystemExit(exit_code) from None


def fail_with_diagnostics(
    diagnostics: "Sequence[Diagnostic]",
    usage_diagnostics: "Sequence[Diagnostic]",
    *,
    source_name: str | None = None,
    program: "ProgramDeclInfo | None" = None,
) -> NoReturn:
    """Report host parameter *diagnostics* and fail the command.

    Prints each diagnostic that is not among *usage_diagnostics*. When any usage
    diagnostic exists, raises one :class:`CommandUsageError` folding their messages with
    ``; ``; otherwise exits 1.
    """
    from agnette.agl.diagnostics import configured_diagnostics, format_diagnostic

    for diag in configured_diagnostics(diagnostics, usage_diagnostics):
        print(format_diagnostic(diag, source_name=source_name), file=sys.stderr)
    if usage_diagnostics:
        raise CommandUsageError("; ".join(diag.message for diag in usage_diagnostics), program)
    raise SystemExit(1)
