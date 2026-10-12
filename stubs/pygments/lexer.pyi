from collections.abc import Iterator

from pygments.token import _TokenType

class Lexer:
    name: str
    aliases: list[str]
    filenames: list[str]
    mimetypes: list[str]
    def __init__(self, **options: object) -> None: ...
    def get_tokens_unprocessed(self, text: str) -> Iterator[tuple[int, _TokenType, str]]: ...
