"""Hidden AgL source, body-offset projection, and adjacent Markdown fence metadata."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from agnette.agl.highlight.categories import Span
from agnette.agl.highlight.lexical import declared_names

_CONTAINER = re.compile(r"[ \t]*(?:(?:>[ \t]*|(?:[-+*]|[0-9]+[.)])[ \t]+)[ \t]*)*")
_LIST = re.compile(r"(?:[-+*]|[0-9]+[.)])[ \t]+")
_FENCE = re.compile(r"(?P<delimiter>`{3,}|~{3,})(?P<header>.*)$")
_LANGUAGE = re.compile(r"\.?agl(?:[ \t]+.*)?$")
# Markdown attributes quote values only; quotes/backslashes in words are literal.
_ATTRIBUTE = re.compile(r"""[^ =}]+=".*?"|[^ =}]+='.*?'|[^ =}]+=[^ =}]+|(?P<word>[^ =}]+)| +""")
_MARKER = re.compile(
    r"<!--\s*agl-(?:check:\s*(?P<check>skip|fragment|error)\s*"
    r"|context:[ \t]?(?P<context>.*?)[ \t]*)-->[ \t]*$"
)


def _list_margin(match: re.Match[str]) -> str:
    return " " * len(match.group())


@dataclass(frozen=True, slots=True)
class MarkdownContainer:
    """Authored Markdown prefix and its list-continuation form."""

    prefix: str

    @classmethod
    def from_line(cls, line: str) -> "MarkdownContainer":
        return cls(next(_CONTAINER.finditer(line)).group())

    @property
    def continuation(self) -> str:
        return _LIST.sub(_list_margin, self.prefix)

    @property
    def has_list(self) -> bool:
        return _LIST.search(self.prefix) is not None

    def continues_at(self, prefix: str) -> bool:
        """List continuations may add indentation; other prefixes must match."""
        if not self.has_list:
            return self.continuation == prefix
        margin = self.continuation
        return prefix.startswith(margin) and not prefix[len(margin) :].strip(" \t")

    def content(self, line: str) -> str | None:
        """Strip the continuation prefix, or report a container boundary."""
        prefix = self.continuation
        depth = prefix.count(">")
        if not depth:
            if line.startswith(prefix):
                return line[len(prefix) :]
            return "" if not line.strip() else None
        container = re.match(rf"(?:[ \t]*>){{{depth}}}", line)
        if container is None:
            return None
        body = line[container.end() :]
        margin = len(prefix.rsplit(">", 1)[1])
        whitespace = len(body) - len(body.lstrip(" \t"))
        if margin > 1 and whitespace < margin and body.strip():
            return None
        return body[min(margin, whitespace) :]


def fence_opening(line: str) -> tuple[MarkdownContainer, str, str] | None:
    """Split any opening fence into its container, delimiter, and header."""
    container = MarkdownContainer.from_line(line)
    fence = _FENCE.fullmatch(line[len(container.prefix) :])
    if fence is None:
        return None
    return container, fence.group("delimiter"), fence.group("header")


def _agl_header(header: str) -> bool:
    header = header.strip(" \t")
    if _LANGUAGE.fullmatch(header):
        return True
    if not (header.startswith("{") and header.endswith("}")):
        return False
    attributes = header[1:-1].replace("\t", " ")
    offset = 0
    while match := _ATTRIBUTE.match(attributes, offset):
        word: str | None = match.group("word")
        if word is not None and word.startswith("."):
            return word == ".agl"
        offset = match.end()
    return False


@dataclass(frozen=True, slots=True)
class FenceMetadata:
    """An AgL opening fence and its contiguous metadata in the same container."""

    check: str | None
    context: str
    start: int  # 0-based first metadata line, or the opening fence if absent
    prefix: str  # Markdown indentation/blockquotes, not AgL indentation
    delimiter: str

    @property
    def container(self) -> MarkdownContainer:
        return MarkdownContainer(self.prefix)

    def body_line(self, line: str) -> str:
        """Remove the container, allowing compact/blank quotes; retain AgL indentation."""
        content = self.container.content(line)
        return line if content is None else content

    def closes(self, content: str) -> bool:
        """Accept matching closing fences at least as long as the opening."""
        return (
            re.fullmatch(
                rf"{re.escape(self.delimiter[0])}{{{len(self.delimiter)},}}[ \t]*", content
            )
            is not None
        )

    def with_option(self, line: str, option: str) -> str:
        """Inject an option into either superfences header form."""
        if line.rstrip().endswith("}") and "{" in line:
            head, _, tail = line.rpartition("}")
            return head + " " + option + "}" + tail
        return line + " " + option


def fence_metadata(lines: Sequence[str], opening: int) -> FenceMetadata | None:
    """Read metadata directly before an AgL fence at a valid 0-based line index.

    Check and context comments may interleave. Preserve context order and AgL
    indentation, removing only one optional space after the colon. Blank lines,
    prose, other comments, and container changes end the metadata block. If check
    markers repeat, the one nearest the fence wins. Non-AgL lines return None.
    """
    fence = fence_opening(lines[opening])
    if fence is None or not _agl_header(fence[2]):
        return None
    container, delimiter, _ = fence
    prefix = container.continuation
    start = opening
    check: str | None = None
    context: list[str] = []
    while start > 0 and not container.has_list:
        previous = lines[start - 1]
        previous_container = MarkdownContainer.from_line(previous)
        if not previous_container.continues_at(prefix):
            break
        marker = _MARKER.fullmatch(previous[len(previous_container.prefix) :])
        if marker is None:
            break
        if marker.group("check") is not None:
            if check is None:
                check = marker.group("check")
        else:
            context.append(marker.group("context"))
        start -= 1
        if previous_container.has_list:
            break
    return FenceMetadata(check, "\n".join(reversed(context)), start, prefix, delimiter)


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
