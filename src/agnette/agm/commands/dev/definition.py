"""Find the declaration of the AgL name at a source position.

Prints one ``PATH:LINE:COLUMN: TEXT`` line per declaration (absolute PATH, 1-based
LINE and COLUMN, TEXT the declaration's source line) for editor integrations.
Scope resolution must succeed; type-checking results refine member and pattern
lookups when available.

Exit-code contract:
    0  a declaration was found
    1  none was found, or the file, position, module roots, or program was invalid
"""

import sys
from pathlib import Path
from typing import TextIO

from agnette.agm.cli_support.args import DevDefinitionArgs
from agnette.agm.cli_support.exec_roots import prepare_entry_file
from agnette.core.fs import read_text_arg
from agnette.util.text import normalize_newlines


def run(args: DevDefinitionArgs) -> None:
    """Print the declarations of the name at ``args.line``:``args.column`` in ``args.file``."""
    from agnette.agl import PipelineDriver
    from agnette.agl.diagnostics import format_diagnostic
    from agnette.agl.navigation import definitions, source_offset

    entry_path = Path(args.file).resolve()
    stdin: TextIO = sys.stdin
    source = normalize_newlines(stdin.read() if args.stdin else read_text_arg(entry_path))
    offset = source_offset(source, args.line, args.column)
    if offset is None:
        print(f"Error: position {args.line}:{args.column} is outside the file", file=sys.stderr)
        raise SystemExit(1)
    prepared = prepare_entry_file(
        entry_path, source, module_paths=args.module_paths, no_stdlib=args.no_stdlib
    )
    if prepared is None:
        raise SystemExit(1)
    if prepared.resolved is None:
        for diag in prepared.diagnostics:
            print(format_diagnostic(diag, source_name=args.file), file=sys.stderr)
        raise SystemExit(1)
    checked = PipelineDriver.typecheck_resolved(prepared.resolved)
    found = definitions(prepared.resolved, checked, source, offset)
    for location in found:
        print(f"{location.path}:{location.line}:{location.column}: {location.text}")
    if not found:
        raise SystemExit(1)
