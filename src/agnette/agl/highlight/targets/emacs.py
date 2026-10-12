"""Emacs vocabulary inventory from canonical language leaves, without font-lock rules."""

import json

from agnette.agl.highlight.grammar import Grammar
from agnette.agl.keywords import (
    KEYWORDS,
    KW_FALSE,
    KW_NULL,
    KW_TRUE,
    OPERATOR_SOFT_KEYWORDS,
    SOFT_KEYWORDS,
)
from agnette.agl.scope.symbols import BUILTIN_CALL_NAMES
from agnette.agl.syntax import BUILTIN_TYPE_NAMES
from agnette.util.ident import IDENT_STOP


def generate(grammar: Grammar) -> str:
    """Export deterministic Lisp data; grammar topology does not affect inventories."""
    inventories = {
        "agl-keywords": KEYWORDS,
        "agl-constant-keywords": {KW_TRUE, KW_FALSE, KW_NULL},
        "agl-operator-keywords": OPERATOR_SOFT_KEYWORDS,
        "agl-soft-keywords": SOFT_KEYWORDS,
        "agl-contextual-builtins": set(BUILTIN_CALL_NAMES),
        "agl-primitive-type-names": set(BUILTIN_TYPE_NAMES),
    }
    lines = [
        ";;; agl-inventory.el --- Generated AgL vocabulary -*- lexical-binding: t; -*-",
        ";; Generated from Agnette's canonical language inventories.",
        "",
    ]
    for name, words in inventories.items():
        items = " ".join(json.dumps(word, ensure_ascii=False) for word in sorted(words))
        lines.append(f"(defconst {name} '({items}))")
    # Emacs bracket expressions require a literal closing bracket first.
    stops = "][" + "".join(sorted(IDENT_STOP - {"[", "]"}))
    lines.extend(
        (
            f"(defconst agl--ident-stop-chars {json.dumps(stops, ensure_ascii=False)})",
            "",
            "(provide 'agl-inventory)",
            ";;; agl-inventory.el ends here",
            "",
        )
    )
    return "\n".join(lines)
