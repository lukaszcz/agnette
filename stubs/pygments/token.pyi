class _TokenType(tuple[str, ...]):
    def __getattr__(self, name: str) -> _TokenType: ...

Comment: _TokenType
Error: _TokenType
Keyword: _TokenType
Name: _TokenType
Number: _TokenType
Operator: _TokenType
Punctuation: _TokenType
String: _TokenType
Text: _TokenType
