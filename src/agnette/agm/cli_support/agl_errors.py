"""Render AgL exceptions that escape a CLI execution boundary."""

from __future__ import annotations

import sys


def exit_for_agl_exception(error: Exception) -> None:
    """Print a safe AgL error and exit, or return for an unrelated exception."""
    from agnette.agl.diagnostics import AglError, format_diagnostic
    from agnette.agl.runtime.boundary import AglException
    from agnette.agl.runtime.request import AgentCancelled
    from agnette.agl.semantics.exceptions import AglRaise

    if isinstance(error, AglError):
        message = format_diagnostic(error.to_diagnostic())
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
