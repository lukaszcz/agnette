"""AgL parser package.

Public API
----------
- :func:`parse_program_seeded` — parse AgL source text into a ``syntax.Program`` AST.
- :class:`AglSyntaxError` — span-aware parse error raised on lex/parse failure.

This package is the **only** place in the codebase that imports both ``lark``
and ``agnette.agl.syntax``.  Everything downstream of the ``AglSyntaxError`` +
``syntax.Program`` boundary depends only on the AST dataclasses, never on
Lark.  (See ``src/agnette/agl/CLAUDE.md`` for the firewall rule.)
"""

from __future__ import annotations

from agnette.agl.parser.errors import AglSyntaxError
from agnette.agl.parser.parser import (
    has_open_verbatim_block,
    has_unterminated_triple_quoted_string,
    is_incomplete_source,
    parse_program_seeded,
    parse_repl_transcript,
    parse_type_expr_seeded,
)

__all__ = [
    "AglSyntaxError",
    "has_open_verbatim_block",
    "has_unterminated_triple_quoted_string",
    "is_incomplete_source",
    "parse_program_seeded",
    "parse_repl_transcript",
    "parse_type_expr_seeded",
]
