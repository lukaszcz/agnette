"""Span-aware lexical error for the AgL lexer."""

from __future__ import annotations

from typing import Self

from agnette.agl.diagnostics import AglError, SourceSpan


class LexError(AglError):
    """A lexical error with an associated source span.

    Raised by the scanner or layout filter when the input is lexically invalid
    (unknown escape sequence, misaligned dedent, etc.).  The ``span``
    attribute carries 1-based line/column information so the runtime can emit
    a :class:`~agnette.agl.diagnostics.Diagnostic` with a precise source location.
    """

    span: SourceSpan

    def __init__(self, message: str, *, span: SourceSpan) -> None:
        super().__init__(message, span=span)

    def renewed(self) -> Self:
        """Return an equal error that has not been raised.

        A recorded failure is raised through a renewed copy at each use, so its
        traceback and context never accumulate on the shared object.
        """
        clone = type(self).__new__(type(self))
        clone.args = (str(self),)
        clone.span = self.span
        clone.related = self.related
        return clone


class IncompleteInputError(LexError):
    """Lexing ran out of input where more input could still complete the construct.

    A REPL checks for this type to decide whether to keep prompting instead of
    reporting the error immediately.
    """


class UnterminatedTripleQuotedStringError(IncompleteInputError):
    """Lexing reached end of input inside an unclosed triple-quoted string."""


class ModuleRouteError(LexError):
    """A slash-joined run of names, a module route, where an operand is expected.

    *route* is the run as written; *divides* is whether the position admits
    division, which spaces around the slash would write.
    """

    def __init__(self, route: str, *, divides: bool, span: SourceSpan) -> None:
        advice = f"'{route}' is a module route: import the module and reach its names with '::'"
        super().__init__(
            f"{advice}, or write ' / ' with spaces to divide" if divides else advice, span=span
        )
        self.route = route
        self.divides = divides

    def renewed(self) -> Self:
        clone = super().renewed()
        clone.route = self.route
        clone.divides = self.divides
        return clone
