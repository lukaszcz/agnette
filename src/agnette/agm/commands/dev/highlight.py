"""Export portable AgL highlighting definitions."""

import sys
from pathlib import Path

from agnette.agl.highlight.targets import TARGETS
from agnette.agm.cli_support.args import DevHighlightArgs
from agnette.core import fs


def run(args: DevHighlightArgs) -> None:
    """Generate a target on demand and write it to stdout or a UTF-8 file."""
    from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR

    rendered = TARGETS[args.target](AGL_GRAMMAR)
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        output = Path(args.output)
        fs.mkdir(output.parent)
        fs.write_text(output, rendered)
