"""Hidden AgL source and body-offset projection, independent of document syntax."""

from dataclasses import dataclass

from agnette.agl.highlight.categories import Span
from agnette.agl.highlight.lexical import declared_names


@dataclass(frozen=True, slots=True)
class HiddenContext:
    """Declarations/imports prepended at a line boundary, never displayed."""

    source: str = ""

    @property
    def prefix(self) -> str:
        """Keep authored newlines; separate nonempty context from the body."""
        return self.source + (
            "\n" if self.source and not self.source.endswith(("\n", "\r")) else ""
        )

    def prepend(self, body: str) -> str:
        return self.prefix + body

    @property
    def declared_names(self) -> tuple[frozenset[str], frozenset[str]]:
        """Seed the lexical fallback even when the context is incomplete."""
        return declared_names(self.source)

    def body_spans(self, spans: list[Span]) -> list[Span]:
        """Drop hidden spans and clip crossing spans, using original-source offsets."""
        offset = len(self.prefix)
        return [(max(a - offset, 0), b - offset, c) for a, b, c in spans if b > offset]
