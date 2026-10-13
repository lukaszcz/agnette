"""Portable lexical grammar with local captures, not lexer promotion history."""

import re
import sys
from dataclasses import replace

from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import (
    Capture,
    Grammar,
    Pop,
    Push,
    Rule,
    State,
    character_class,
    keyword_rule,
)
from agnette.agl.keywords import KEYWORDS, OPERATOR_SOFT_KEYWORDS, SOFT_KEYWORDS
from agnette.agl.operator_names import is_operator_name_char
from agnette.agl.scope.symbols import BUILTIN_CALL_NAMES
from agnette.agl.syntax import BUILTIN_TYPE_NAMES
from agnette.agl.value_syntax.lexical import ESCAPE_DECODE
from agnette.util.ident import IDENT_STOP, is_identifier_start

# Explicit sets preserve Python's Unicode vocabulary without engine-specific classes.
_START = character_class(
    chr(point) for point in range(sys.maxunicode + 1) if is_identifier_start(chr(point))
)
_OPERATOR = character_class(
    chr(point) for point in range(sys.maxunicode + 1) if is_operator_name_char(chr(point))
)
_STOP = character_class(IDENT_STOP)[1:-1]
_NAME = f"{_START}[^{_STOP}]*"
# Holes open at `%`, optionally followed by `!` to shell-quote the value.
_ENV = Rule(r"%!?\$\{" + _NAME + r"\}", Category.ENV_HOLE)
_HOLE = Rule(r"%!?\{", Category.INTERPOLATION, action=Push("interpolation"))
_ESCAPE = Rule(
    r"\\(?:"
    + character_class(ESCAPE_DECODE)
    + r"|u[dD][89aAbB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}"
    r"|u(?:[0-9a-cA-Ce-fE-F][0-9a-fA-F]{3}|[dD][0-7][0-9a-fA-F]{2}))",
    Category.STRING_ESCAPE,
)
_CONSTANTS = frozenset({"true", "false", "null"})
_RESERVED_OPERATORS = frozenset(
    {"=>", "==", "=", "!=", "<=", "<", ">=", ">", "->", "-", "+", "*", "/", "|"}
)
_OPERATOR_END = f"(?!{_OPERATOR})"
_CALL = keyword_rule(BUILTIN_CALL_NAMES, Category.BUILTIN_CALL, token_start=True)
_RESERVED = keyword_rule(KEYWORDS, Category.KEYWORD, token_start=True).pattern
_PLAIN = f"(?!{_RESERVED}){_NAME}"
_QUALIFIER = r"::(?![ \t]*\[)"  # Type arguments are core call syntax, not paths.
_DECLARATION = keyword_rule(
    KEYWORDS & {"record", "enum", "type", "exception"}, Category.KEYWORD, token_start=True
).pattern

# These same-line captures deliberately do not track declarations, enum owners,
# attribute arguments, module headers, or softword/layout promotion windows.
_CONTEXT = (
    Rule(
        f"(@)([ \\t]*)({_PLAIN})",
        captures=(Capture(1, Category.ATTRIBUTE), Capture(3, Category.ATTRIBUTE)),
    ),
    Rule(
        f"({_DECLARATION})([ \\t]+)({_PLAIN})",
        captures=(Capture(1, Category.KEYWORD), Capture(3, Category.TYPE)),
    ),
    Rule(
        r"(\|" + _OPERATOR_END + r")([ \t]*)(" + _PLAIN + ")",
        captures=(Capture(1, Category.OPERATOR), Capture(3, Category.CONSTRUCTOR)),
    ),
    Rule(
        f"(?:{_PLAIN}(?:/{_PLAIN})*/\\*{_OPERATOR_END}(?:{_QUALIFIER})?"
        f"|{_PLAIN}(?:/{_PLAIN})+(?:{_QUALIFIER})?|{_PLAIN}{_QUALIFIER})",
        Category.MODULE,
    ),
)

_CODE = (
    Rule(r"[ \t]+"),
    Rule(r"#.*", Category.COMMENT),
    Rule('"""', Category.STRING, action=Push("triple-double")),
    Rule("'''", Category.STRING, action=Push("triple-single")),
    Rule('"', Category.STRING, action=Push("double")),
    Rule("'", Category.STRING, action=Push("single")),
    Rule(r"\$(?=[ \t]*$)", Category.VERBATIM, action=Push("verbatim-block")),
    Rule(r"\$", Category.VERBATIM, action=Push("verbatim")),
    *_CONTEXT,
    # Rules run only at token starts: names are consumed whole, including a+b.
    keyword_rule(_CONSTANTS, Category.KEYWORD_CONSTANT, token_start=True),
    keyword_rule(KEYWORDS - _CONSTANTS, Category.KEYWORD, token_start=True),
    # Inventory coloring is intentionally independent of layout/promotion history.
    keyword_rule(SOFT_KEYWORDS - OPERATOR_SOFT_KEYWORDS, Category.KEYWORD, token_start=True),
    keyword_rule(OPERATOR_SOFT_KEYWORDS, Category.OPERATOR_WORD, token_start=True),
    keyword_rule(BUILTIN_TYPE_NAMES, Category.TYPE, token_start=True),
    replace(_CALL, pattern=_CALL.pattern + r"(?=[ \t]*(?:\(|::))"),
    Rule(_NAME, Category.NAME),
    Rule(r"[0-9]+(?:\.[0-9]+)?", Category.NUMBER),
    Rule(r"\?[0-9]+", Category.PLACEHOLDER),
    Rule(r"\?" + _OPERATOR_END, Category.PLACEHOLDER),
    Rule(
        "(?:"
        + "|".join(
            re.escape(word) for word in sorted(sorted(_RESERVED_OPERATORS), key=len, reverse=True)
        )
        + ")"
        + _OPERATOR_END,
        Category.OPERATOR,
    ),
    Rule(_OPERATOR + "+", Category.NAME),
    Rule(r"::|:=|@", Category.OPERATOR),
    Rule(r"[(\[{]", Category.PUNCTUATION, action=Push("brackets")),
    Rule(r"[)\]{}:,.;]", Category.PUNCTUATION),
)


def _string_state(name: str, delimiter: str, *, multiline: bool = False) -> State:
    return State(
        name,
        (
            Rule(delimiter, Category.STRING, action=Pop()),
            *((Rule(r"$", action=Pop()),) if not multiline else ()),
            _ESCAPE,
            Rule(r"\\", Category.ERROR, action=Push("rejected-template")),
            _ENV,
            _HOLE,
        ),
        Category.STRING,
    )


# CODE tracks all brackets across lines; holes track only curly depth and unwind
# at line end, matching the scanner even when parentheses/squares are mismatched.
_BRACKET_CODE = tuple(rule for rule in _CODE if rule.category != Category.VERBATIM)
_INTERPOLATION_CODE = (
    Rule(r"\$", Category.ERROR),
    Rule(r"[(\[]", Category.PUNCTUATION),
    *(
        replace(rule, pattern=r"\{", action=Push("hole-braces"))
        if rule.action == Push("brackets")
        else rule
        for rule in _BRACKET_CODE
        if rule.category != Category.COMMENT
    ),
)


def _verbatim_rules() -> tuple[Rule, ...]:
    return (
        Rule(r"\\%", Category.VERBATIM),  # escapes a following hole opener
        _ENV,
        _HOLE,
    )


AGL_GRAMMAR = Grammar(
    (
        State("root", _CODE, Category.ERROR),
        State(
            "brackets",
            (
                Rule(r"[)\]}]", Category.PUNCTUATION, action=Pop()),
                Rule(r"\$", Category.ERROR),
                *_BRACKET_CODE,
            ),
            Category.ERROR,
        ),
        _string_state("double", '"'),
        _string_state("single", "'"),
        _string_state("triple-double", '"""', multiline=True),
        _string_state("triple-single", "'''", multiline=True),
        # Native child recovery pops only the child; multiline hosts survive
        # until their own closer/dedent. Single-line hosts then cascade at EOL.
        State(
            "interpolation",
            (
                Rule(r"$", action=Pop()),
                Rule(r"\}", Category.INTERPOLATION, action=Pop()),
                *_INTERPOLATION_CODE,
            ),
            Category.ERROR,
        ),
        State(
            "hole-braces",
            (
                Rule(r"\}", Category.PUNCTUATION, action=Pop()),
                Rule(r"$", action=Pop()),
                *_INTERPOLATION_CODE,
            ),
            Category.ERROR,
        ),
        State("rejected-template", (Rule(r"$", action=Pop()),), Category.ERROR),
        State(
            "verbatim",
            (Rule(r"$|(?=[ \t]+$)", action=Pop()), *_verbatim_rules()),
            Category.VERBATIM,
        ),
        State(
            "verbatim-block",
            (
                Rule(r"^(?=[^ \t\r\n])", action=Pop()),
                Rule(r"^[ \t]+$"),
                *_verbatim_rules(),
            ),
            Category.VERBATIM,
        ),
    )
)
