"""Optional Pygments adapter over the exact AgL highlighting tiers."""

from collections.abc import Iterator
from contextvars import ContextVar

from pygments.lexer import Lexer
from pygments.token import (
    Comment,
    Error,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Text,
    _TokenType,
)
from pygments.util import get_bool_opt

from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.context import HiddenContext
from agnette.agl.highlight.lexical import lexical_spans
from agnette.agl.highlight.semantic import semantic_spans

_TOKENS: dict[Category, _TokenType] = {
    Category.TEXT: Text,
    Category.COMMENT: Comment.Single,
    Category.KEYWORD: Keyword,
    Category.KEYWORD_CONSTANT: Keyword.Constant,
    Category.OPERATOR_WORD: Operator.Word,
    Category.TYPE: Keyword.Type,
    Category.CONSTRUCTOR: Name.Class,
    Category.FUNCTION: Name.Function,
    Category.BUILTIN_CALL: Name.Builtin,
    Category.MODULE: Name.Namespace,
    Category.ATTRIBUTE: Name.Decorator,
    Category.PARAMETER: Name.Variable,
    Category.LOCAL: Name.Variable,
    Category.NAME: Name,
    Category.STRING: String,
    Category.STRING_ESCAPE: String.Escape,
    Category.INTERPOLATION: String.Interpol,
    Category.VERBATIM: String.Other,
    Category.ENV_HOLE: String.Interpol,
    Category.NUMBER: Number.Integer,
    Category.OPERATOR: Operator,
    Category.PUNCTUATION: Punctuation,
    Category.PLACEHOLDER: Name.Variable,
    Category.ERROR: Error,
}


document_context: ContextVar[str] = ContextVar("agl_highlight_context", default="")


class AglLexer(Lexer):
    """Highlight original-source spans; ``context`` is hidden, ``semantic`` defaults on."""

    name = "AgL"
    aliases = ["agl"]
    filenames = ["*.agl"]
    mimetypes = ["text/x-agl"]

    def __init__(self, **options: object) -> None:
        super().__init__(**options)
        self.context = HiddenContext(str(options.get("context", document_context.get())))
        self.semantic = get_bool_opt(options, "semantic", True)

    def get_tokens_unprocessed(self, text: str) -> Iterator[tuple[int, _TokenType, str]]:
        if self.semantic:
            spans = semantic_spans(text, self.context)
        else:
            types, constructors = self.context.declared_names
            spans = lexical_spans(text, types, constructors)
        cursor = 0
        for start, end, category in spans:
            if start > cursor:
                yield cursor, Text, text[cursor:start]
            value = text[start:end]
            token = _TOKENS[category]
            if category == Category.NUMBER and "." in value:
                token = Number.Float
            yield start, token, value
            cursor = end
        if cursor < len(text):
            yield cursor, Text, text[cursor:]
