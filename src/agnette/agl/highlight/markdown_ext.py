"""Pass hidden fence metadata to Pygments through superfences' default highlighter."""

import base64
import re
from copy import copy
from itertools import chain, takewhile
from typing import cast

from markdown import Markdown
from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor
from pymdownx.superfences import (
    RE_NESTED_FENCE_START,
    SuperFencesBlockPreprocessor,
    highlight_validator,
)

from agnette.agl.highlight.context import (
    FenceMetadata,
    MarkdownContainer,
    fence_metadata,
    fence_opening,
)
from agnette.agl.highlight.pygments_lexer import document_context
from agnette.util.text import normalize_newlines

_DELIMITER = re.compile(r"`+|<!--|-->")
_BACKTICKS = re.compile(r"`+")
_OPTION = "agl_context"
_BLOCK_PREFIX = re.compile(r"(?: {0,3}>[ \t]?| {0,3}(?:[-+*]|[0-9]+[.)])[ \t]+)")
_HTML_TAG = re.compile(r"<(?P<closing>/)?(?P<tag>[A-Za-z][A-Za-z0-9:-]*)\b[^>]*>")
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_CDATA_TAGS = frozenset({"script", "style", "xmp", "iframe", "noembed", "noframes"})


def _is_escaped(line: str, index: int) -> bool:
    """Check whether an odd run of backslashes precedes *index*."""
    start = index
    while start and line[start - 1] == "\\":
        start -= 1
    return (index - start) % 2 == 1


def _has_code_span_closer(lines: list[str], line_index: int, offset: int, ticks: int) -> bool:
    """Check whether this code span closes before its Markdown paragraph ends."""
    paragraph = takewhile(
        lambda line: line.strip() and fence_opening(line) is None,
        (lines[index] for index in range(line_index + 1, len(lines))),
    )
    return any(
        len(run.group()) == ticks
        for line in chain([lines[line_index][offset:]], paragraph)
        for run in _BACKTICKS.finditer(line)
    )


def _is_indented_code(line: str) -> bool:
    """Check whether a line has code-block indentation after Markdown containers."""
    index = 0
    while prefix := _BLOCK_PREFIX.match(line, index):
        index = prefix.end()
    indentation = re.match(r"[ \t]*", line[index:])
    return indentation is not None and len(indentation.group().expandtabs(4)) >= 4


def _advance_html_tags(line: str, tags: list[str]) -> None:
    """Track the end of a raw HTML block, ignoring markup inside CDATA elements."""
    for match in _HTML_TAG.finditer(line):
        tag = cast(str, match.group("tag")).lower()
        closing = match.start("closing") >= 0
        if tags and tags[-1] in _CDATA_TAGS and (not closing or tag != tags[-1]):
            continue
        if closing:
            if tag in tags:
                while tags.pop() != tag:
                    pass
        elif tag not in _VOID_TAGS and not str(match.group()).endswith("/>"):
            tags.append(tag)


def _start_html_block(line: str, md: Markdown, tags: list[str]) -> bool:
    """Start tracking a block-level HTML element, if this line opens one."""
    container = MarkdownContainer.from_line(line)
    content = line[len(container.prefix) :]
    if (match := _HTML_TAG.match(content)) is None:
        return False
    tag = cast(str, match.group("tag")).lower()
    block_tags = cast(list[str], getattr(md, "block_level_elements"))
    if match.start("closing") >= 0 or tag not in block_tags:
        return False
    _advance_html_tags(content, tags)
    return True


def _scan_comments(
    lines: list[str], line_index: int, comment_depth: int, code_span: int | None
) -> tuple[int, int | None]:
    """Advance HTML comment depth and the open code span across one line.

    Comment delimiters inside matched inline code spans, and escaped openers, are text.
    """
    line = lines[line_index]
    index = 0
    while (delimiter := _DELIMITER.search(line, index)) is not None:
        index = delimiter.start()
        token = delimiter.group()
        if token.startswith("`"):
            if code_span is not None:
                if len(token) == code_span:
                    code_span = None
            elif (
                comment_depth
                or _is_escaped(line, index)
                or not _has_code_span_closer(lines, line_index, delimiter.end(), len(token))
            ):
                # The rest of the run may still open a span.
                index += 1
                continue
            else:
                code_span = len(token)
        elif code_span is None:
            if token == "-->":
                comment_depth = max(0, comment_depth - 1)
            elif comment_depth or not _is_escaped(line, index):
                comment_depth += 1
        index = delimiter.end()
    return comment_depth, code_span


class ContextPreprocessor(Preprocessor):
    """Encode adjacent contexts before superfences or HTML comment stashing runs."""

    def _accepted_header(self, line: str) -> SuperFencesBlockPreprocessor | None:
        """Parse a header with SuperFences' configured pipeline, without changing its state."""
        fenced = cast(SuperFencesBlockPreprocessor, self.md.preprocessors["fenced_code_block"])
        fenced.get_hl_settings()
        probe = copy(fenced)
        probe.clear()
        prefix = MarkdownContainer.from_line(line).prefix
        match = RE_NESTED_FENCE_START.match(line, len(prefix))
        if match is None:
            return None
        if match.group("unrecognized"):
            accepted = probe.handle_unrecognized(match)
        elif match.group("attrs"):
            accepted = probe.handle_attrs(match)
        else:
            accepted = probe.parse_options(match)
        return probe if accepted else None

    def accepts(self, line: str, context: str | None = None) -> bool:
        """Check whether SuperFences accepts this AgL header and optional context."""
        probe = self._accepted_header(line)
        return (
            probe is not None
            and probe.lang == "agl"
            and (context is None or probe.options.get(_OPTION) == context)
        )

    def run(self, lines: list[str]) -> list[str]:
        lines = normalize_newlines("\n".join(lines)).split("\n")
        removed: set[int] = set()
        body: FenceMetadata | None = None
        comment_depth = 0
        code_span: int | None = None
        html_tags: list[str] = []
        normalize_body = False
        for index, line in enumerate(lines):
            if body is not None:
                content = body.container.content(line)
                if content is not None:
                    if normalize_body:
                        lines[index] = body.prefix + content
                    if body.closes(content):
                        body = None
                    continue
                body = None
            if html_tags:
                _advance_html_tags(line, html_tags)
                continue
            if not comment_depth and (opening := fence_opening(line)):
                header = self._accepted_header(line)
                if header is None:
                    continue
                container, delimiter, _ = opening
                body = FenceMetadata(None, "", index, container.continuation, delimiter)
                metadata = fence_metadata(lines, index) if header.lang == "agl" else None
                normalize_body = metadata is not None and ">" in body.prefix
                if metadata is not None:
                    context_lines = {
                        i for i in range(metadata.start, index) if "agl-context:" in lines[i]
                    }
                    if context_lines:
                        payload = base64.urlsafe_b64encode(metadata.context.encode()).decode(
                            "ascii"
                        )
                        injected = metadata.with_option(line, f'{_OPTION}="{payload}"')
                        if not self.accepts(injected, metadata.context):
                            normalize_body = False
                            continue
                        list_metadata = any(
                            MarkdownContainer.from_line(lines[i]).has_list
                            for i in range(metadata.start, index)
                        )
                        for context_index in context_lines:
                            container = MarkdownContainer.from_line(lines[context_index])
                            if list_metadata and not container.has_list:
                                removed.add(context_index)
                            else:
                                lines[context_index] = container.prefix
                        lines[index] = injected
                continue
            if not comment_depth:
                if _is_indented_code(line) or _start_html_block(line, self.md, html_tags):
                    continue
            comment_depth, code_span = _scan_comments(lines, index, comment_depth, code_span)
        return [line for index, line in enumerate(lines) if index not in removed]


class AglHighlightExtension(Extension):
    """Remove hidden contexts, retaining the authored check markers and containers."""

    def extendMarkdown(self, md: Markdown) -> None:
        md.preprocessors.register(ContextPreprocessor(md), "agl_context", 40)


def validator(
    language: str,
    inputs: dict[str, str | bool],
    options: dict[str, str],
    attrs: dict[str, str | bool],
    md: Markdown,
) -> bool:
    """Decode our option; preserve superfences' option and attribute validation."""
    inputs = inputs.copy()
    if _OPTION in inputs:
        payload = inputs.pop(_OPTION)
        if not isinstance(payload, str):
            return False
        try:
            options[_OPTION] = base64.b64decode(payload, altchars=b"-_", validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            return False
    return highlight_validator(language, inputs, options, attrs, md)


def formatter(
    source: str,
    language: str,
    _class_name: str,
    options: dict[str, str],
    md: Markdown,
    **kwargs: object,
) -> str:
    """Delegate HTML/options unchanged, isolating context even on failure or nesting."""
    options = options.copy()
    token = document_context.set(options.pop(_OPTION, ""))
    try:
        fenced = cast(SuperFencesBlockPreprocessor, md.preprocessors["fenced_code_block"])
        return fenced.highlight(source, language, options, md, **kwargs)
    finally:
        document_context.reset(token)
