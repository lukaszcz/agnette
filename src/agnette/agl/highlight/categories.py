"""Target-neutral highlighting vocabulary; offsets are half-open source ranges."""

from enum import StrEnum


class Category(StrEnum):
    """Lexical and scope-resolved categories shared by highlighting front ends."""

    TEXT = "text"
    COMMENT = "comment"
    KEYWORD = "keyword"
    KEYWORD_CONSTANT = "keyword_constant"
    OPERATOR_WORD = "operator_word"
    TYPE = "type"
    CONSTRUCTOR = "constructor"
    FUNCTION = "function"
    BUILTIN_CALL = "builtin_call"
    MODULE = "module"
    ATTRIBUTE = "attribute"
    PARAMETER = "parameter"
    LOCAL = "local"
    NAME = "name"
    STRING = "string"
    STRING_ESCAPE = "string_escape"
    INTERPOLATION = "interpolation"
    VERBATIM = "verbatim"
    ENV_HOLE = "env_hole"
    NUMBER = "number"
    OPERATOR = "operator"
    PUNCTUATION = "punctuation"
    PLACEHOLDER = "placeholder"
    ERROR = "error"


type Span = tuple[int, int, Category]
