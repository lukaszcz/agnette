"""Hidden AgL source, body-offset projection, and adjacent Markdown fence metadata."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from agnette.agl.highlight.categories import Span
from agnette.agl.highlight.lexical import declared_names

_FENCE = re.compile(
    r"^(?P<prefix>[ \t]*(?:>[ \t]*)*)(?P<delimiter>`{3,}|~{3,})[ \t]*agl(?:[ \t]+.*)?$"
)
_MARKER = re.compile(
    r"<!--\s*agl-(?:check:\s*(?P<check>skip|fragment|error)\s*"
    r"|context:[ \t]?(?P<context>.*?)[ \t]*)-->[ \t]*$"
)


@dataclass(frozen=True, slots=True)
class FenceMetadata:
    """An AgL opening fence and its contiguous metadata in the same container."""

    check: str | None
    context: str
    start: int  # 0-based first metadata line, or the opening fence if absent
    prefix: str  # Markdown indentation/blockquotes, not AgL indentation
    delimiter: str

    def body_line(self, line: str) -> str:
        """Remove the container, allowing compact/blank quotes; retain AgL indentation."""
        depth = self.prefix.count(">")
        if not depth:
            return line.removeprefix(self.prefix)
        container = re.match(rf"(?:[ \t]*>){{{depth}}}", line)
        if container is None:
            return line
        body = line[container.end() :]
        margin = len(self.prefix.rsplit(">", 1)[1])
        whitespace = len(body) - len(body.lstrip(" \t"))
        return body[min(margin, whitespace) :]


def fence_metadata(lines: Sequence[str], opening: int) -> FenceMetadata | None:
    """Read metadata directly before an AgL fence at a valid 0-based line index.

    Check and context comments may interleave. Preserve context order and AgL
    indentation, removing only one optional space after the colon. Blank lines,
    prose, other comments, and container changes end the metadata block. If check
    markers repeat, the one nearest the fence wins. Non-AgL lines return None.
    """
    fence = _FENCE.fullmatch(lines[opening])
    if fence is None:
        return None
    prefix = fence.group("prefix")
    start = opening
    check: str | None = None
    context: list[str] = []
    while start > 0:
        previous = lines[start - 1]
        if not previous.startswith(prefix):
            break
        marker = _MARKER.fullmatch(previous[len(prefix) :])
        if marker is None:
            break
        if marker.group("check") is not None:
            if check is None:
                check = marker.group("check")
        else:
            context.append(marker.group("context"))
        start -= 1
    return FenceMetadata(
        check, "\n".join(reversed(context)), start, prefix, fence.group("delimiter")
    )


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
