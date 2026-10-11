"""Pass hidden fence metadata to Pygments through superfences' default highlighter."""

import base64
import re
from copy import copy
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

_COMMENT = re.compile(r"<!--|-->")
_OPTION = "agl_context"


class ContextPreprocessor(Preprocessor):
    """Encode adjacent contexts before superfences or HTML comment stashing runs."""

    def accepts(self, line: str, context: str | None = None) -> bool:
        """Use SuperFences' configured header pipeline without changing its state."""
        fenced = cast(SuperFencesBlockPreprocessor, self.md.preprocessors["fenced_code_block"])
        fenced.get_hl_settings()
        probe = copy(fenced)
        probe.clear()
        prefix = MarkdownContainer.from_line(line).prefix
        match = RE_NESTED_FENCE_START.match(line, len(prefix))
        if match is None:
            return False
        if match.group("unrecognized"):
            accepted = probe.handle_unrecognized(match)
        elif match.group("attrs"):
            accepted = probe.handle_attrs(match)
        else:
            accepted = probe.parse_options(match)
        return (
            accepted
            and probe.lang == "agl"
            and (context is None or probe.options.get(_OPTION) == context)
        )

    def run(self, lines: list[str]) -> list[str]:
        lines = lines.copy()
        removed: set[int] = set()
        body: FenceMetadata | None = None
        comment_depth = 0
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
            if not comment_depth and (opening := fence_opening(line)):
                container, delimiter, _ = opening
                body = FenceMetadata(None, "", index, container.continuation, delimiter)
                metadata = fence_metadata(lines, index)
                if metadata is not None and not self.accepts(line):
                    metadata = None
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
                        for context_index in context_lines:
                            container = MarkdownContainer.from_line(lines[context_index])
                            if container.has_list:
                                lines[context_index] = container.prefix
                            else:
                                removed.add(context_index)
                        lines[index] = injected
                continue
            for comment in _COMMENT.finditer(line):
                if comment.group() == "<!--":
                    comment_depth += 1
                else:
                    comment_depth = max(0, comment_depth - 1)
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
