"""Implementation of the ``agm check FILE...`` command.

Behaviour: run the full **static** AgL pipeline (parse, module loading, scope
resolution, type checking, match compilation, and lowering) over each given
``.agl`` file, independently and in argument order, printing GNU-style
diagnostics to stderr. Unlike ``agm exec``, no file needs to declare a
``program def`` — library modules can be checked too. A declared ``program
def`` is validated as part of the module it lives in, but ``check`` never
selects, validates its arguments, or runs one: it never evaluates anything
and never invokes an agent. Every file is checked even when an earlier one
failed; exit code is 1 iff any file produced an error-severity diagnostic,
was unreadable/missing, or had an invalid module-root configuration.

Module resolution, file reading (via ``core.fs``) and diagnostic rendering are shared with
``agm exec``; ``check`` skips ``exec``'s program discovery/selection/preflight.
Lowering (rather than stopping at match compilation, as the REPL's internal
check-only entry path does) is required here because some static errors — an invalid
``resource``/``resource-dir`` path — surface only during lowering, not
during type checking. ``check_prepared`` reaches exactly that far and no further: it
never resolves or validates a program's arguments, so a required ``program
def`` value parameter with no default is silently accepted, unlike under
``agm exec``.

Warnings (on ``RunResult.warnings``) are a separate channel from error
diagnostics, printed to stderr but never affecting the exit code — the same
channel discipline ``agm exec`` uses (see ``commands/exec_program.py``). A warning the
warning policy denies is an error diagnostic.

Exit-code contract:
    0  no file produced an error-severity diagnostic
    1  some file did, was unreadable/missing, or had an invalid module-root
       configuration
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from agnette.agm.cli_support.args import CheckArgs
from agnette.agm.cli_support.exec_roots import prepare_entry_file
from agnette.core.fs import read_text_arg_or_none

if TYPE_CHECKING:
    from agnette.agl.warning_policy import WarningPolicy


def run(args: CheckArgs, *, warning_policy: "WarningPolicy | None" = None) -> None:
    """Statically check each file in ``args.files``, independently, in order.

    *warning_policy* is the invocation's (``None``: the catalog defaults).
    """
    from agnette.agl import PipelineDriver
    from agnette.agl.diagnostics import format_diagnostic

    runtime = PipelineDriver(
        get_sandbox_context=None, resolve_agent_spec=None, warning_policy=warning_policy
    )
    had_error = False

    for file in args.files:
        entry_path = Path(file)
        source = read_text_arg_or_none(entry_path)
        if source is None:
            # ``read_text_arg_or_none`` already printed a consistent
            # "Error: cannot read ..." message; keep checking the remaining
            # files.
            had_error = True
            continue

        prepared = prepare_entry_file(
            entry_path, source, module_paths=args.module_paths, no_stdlib=args.no_stdlib
        )
        if prepared is None:
            # ``prepare_entry_file`` already printed the canonical
            # "Error: invalid module roots configuration ..." message.
            had_error = True
            continue
        result = runtime.check_prepared(prepared)

        for diag in result.warnings:
            print(format_diagnostic(diag, source_name=file), file=sys.stderr)
        if not result.ok:
            for diag in result.diagnostics:
                print(format_diagnostic(diag, source_name=file), file=sys.stderr)
            had_error = True

    if had_error:
        raise SystemExit(1)
