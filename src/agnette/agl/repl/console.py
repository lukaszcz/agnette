"""prompt_toolkit front end: category styles, completion, prompts, and loop wiring.

Recognition lives in :mod:`agnette.agl.highlight.lexical`; the loop and themes
remain separate. Enter reads only the precisely typed current Buffer, avoiding
prompt_toolkit's Any-typed Application surface.
"""

from __future__ import annotations

import bisect
import re
import sys
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText, StyleAndTextTuples
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.output import Output

from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.lexical import repl_spans
from agnette.agl.keywords import KEYWORDS, SOFT_KEYWORDS
from agnette.agl.lexer import lex_open_quote_start
from agnette.agl.repl import meta as meta_mod
from agnette.agl.repl.loop import CONTINUATION, PROMPT, is_incomplete, run_repl_loop
from agnette.agl.repl.themes import get_style
from agnette.agl.scope.symbols import BUILTIN_CALL_NAMES

if TYPE_CHECKING:
    from pathlib import Path

    from agnette.agl.repl.session import ReplSession

_KEYWORDS: tuple[str, ...] = tuple(sorted(KEYWORDS | SOFT_KEYWORDS))
_IDENT_PREFIX: re.Pattern[str] = re.compile(r"[A-Za-z0-9_-]+$")

# Finer target-neutral categories preserve the REPL's existing palette.
_CATEGORY_STYLES: dict[Category, str] = {
    Category.TEXT: "",
    Category.ERROR: "",
    Category.COMMENT: "class:agl.comment",
    Category.KEYWORD: "class:agl.keyword",
    Category.KEYWORD_CONSTANT: "class:agl.keyword",
    Category.OPERATOR_WORD: "class:agl.keyword",
    Category.TYPE: "class:agl.type",
    Category.CONSTRUCTOR: "class:agl.constructor",
    Category.FUNCTION: "class:agl.name",
    Category.BUILTIN_CALL: "class:agl.name",
    Category.MODULE: "class:agl.name",
    Category.ATTRIBUTE: "class:agl.attribute",
    Category.PARAMETER: "class:agl.name",
    Category.LOCAL: "class:agl.name",
    Category.NAME: "class:agl.name",
    Category.STRING: "class:agl.string",
    Category.STRING_ESCAPE: "class:agl.string",
    Category.INTERPOLATION: "class:agl.string",
    Category.VERBATIM: "class:agl.string",
    Category.ENV_HOLE: "class:agl.string",
    Category.NUMBER: "class:agl.number",
    Category.OPERATOR: "class:agl.operator",
    Category.PUNCTUATION: "class:agl.operator",
    Category.PLACEHOLDER: "class:agl.operator",
}


class AglPromptLexer(Lexer):
    """Map shared lexical spans to styles, seeding names from the live session."""

    def __init__(self, session: ReplSession | None = None) -> None:
        self._session = session

    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        type_names = self._session.type_names() if self._session is not None else frozenset()
        constructor_names = (
            self._session.constructor_names() if self._session is not None else frozenset()
        )
        styled = self._styled_lines(document.text, type_names, constructor_names)

        def get_line(lineno: int) -> StyleAndTextTuples:
            return styled[lineno]

        return get_line

    @staticmethod
    def _styled_lines(
        text: str,
        type_names: frozenset[str] = frozenset(),
        constructor_names: frozenset[str] = frozenset(),
    ) -> list[StyleAndTextTuples]:
        """Clip legacy spans to their starting line, without redistributing them."""
        lines = text.split("\n")
        line_starts = _line_start_offsets(text)
        per_line: list[list[tuple[int, int, str]]] = [[] for _ in lines]
        for start, end, category in repl_spans(text, type_names, constructor_names):
            style = _CATEGORY_STYLES[category]
            if not style:
                continue
            line_index = bisect.bisect_right(line_starts, start) - 1
            base = line_starts[line_index]
            per_line[line_index].append((start - base, end - base, style))
        return [_fragments_for_line(line, per_line[i]) for i, line in enumerate(lines)]


def _highlighted_agl_fragments(
    text: str,
    session: ReplSession | None = None,
    agl_ranges: tuple[tuple[int, int], ...] = (),
) -> StyleAndTextTuples:
    """Style only the AgL fragments in a mixed ``:info`` display."""
    fragments: StyleAndTextTuples = []
    end = 0
    for start, stop in agl_ranges:
        if end < start:
            fragments.append(("", text[end:start]))
        fragments.extend(_agl_fragments(text[start:stop], session))
        end = stop
    if end < len(text):
        fragments.append(("", text[end:]))
    return fragments


def _agl_fragments(text: str, session: ReplSession | None) -> StyleAndTextTuples:
    """Return syntax-highlighted fragments for one AgL source fragment."""
    get_line = AglPromptLexer(session).lex_document(Document(text))
    lines = text.split("\n")
    fragments: StyleAndTextTuples = []
    for lineno in range(len(lines)):
        fragments.extend(get_line(lineno))
        if lineno < len(lines) - 1:
            fragments.append(("", "\n"))
    return fragments


def _line_start_offsets(text: str) -> list[int]:
    """Return the absolute offset at which each line begins."""
    offsets = [0]
    for line in text.split("\n")[:-1]:
        offsets.append(offsets[-1] + len(line) + 1)
    return offsets


def _fragments_for_line(line: str, spans: list[tuple[int, int, str]]) -> StyleAndTextTuples:
    """Turn a line and its styled spans into ordered fragments."""
    if not spans:
        return [("", line)]
    fragments: StyleAndTextTuples = []
    cursor = 0
    for start, end, style in sorted(spans):
        if start > cursor:
            fragments.append(("", line[cursor:start]))
        fragments.append((style, line[start:end]))
        cursor = end
    if cursor < len(line):
        fragments.append(("", line[cursor:]))
    return fragments


class AglCompleter(Completer):
    """Complete meta-commands or words from keywords and live session bindings."""

    def __init__(self, session: ReplSession) -> None:
        self._session = session

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        del complete_event
        if document.text_before_cursor.lstrip().startswith(":"):
            yield from self._meta_completions(document)
            return
        yield from self._word_completions(document)

    def _meta_completions(self, document: Document) -> Iterable[Completion]:
        word = document.text_before_cursor.lstrip()
        for name in meta_mod.meta_command_names():
            if name.startswith(word):
                yield Completion(name, start_position=-len(word))

    def _word_completions(self, document: Document) -> Iterable[Completion]:
        if lex_open_quote_start(document.text_before_cursor) is not None:
            return
        word = _completion_word_before_cursor(document.text_before_cursor)
        if word is None:
            return
        for candidate in self._candidates():
            if candidate.startswith(word) and candidate != word:
                yield Completion(candidate, start_position=-len(word))

    def _candidates(self) -> list[str]:
        names = list(_KEYWORDS)
        names.extend(BUILTIN_CALL_NAMES)
        names.extend(name for name, _type, _value in self._session.bindings())
        seen: set[str] = set()
        unique: list[str] = []
        for name in names:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique


def _completion_word_before_cursor(text_before_cursor: str) -> str | None:
    """Return an identifier prefix, or None when the cursor is off-word."""
    if text_before_cursor == "":
        return ""
    match = _IDENT_PREFIX.search(text_before_cursor)
    return None if match is None else match.group(0)


def _make_key_bindings() -> KeyBindings:
    """Bind Enter to AgL-aware continuation without using the Any-typed app."""
    bindings = KeyBindings()

    @bindings.add("enter")
    def _on_enter(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if is_incomplete(buffer.text):
            buffer.insert_text("\n")
        else:
            buffer.validate_and_handle()

    return bindings


def _make_history(history_path: Path | None) -> History:
    """Use file history when configured, otherwise in-memory history."""
    if history_path is None:
        return InMemoryHistory()
    return FileHistory(str(history_path))


def build_prompt_session(
    session: ReplSession,
    *,
    theme: str = "auto",
    history_path: Path | None = None,
    input: Input | None = None,
    output: Output | None = None,
) -> PromptSession[str]:
    """Construct the styled, multiline REPL prompt with injectable terminal I/O."""
    return PromptSession(
        message=[("class:agl.prompt", PROMPT)],
        lexer=AglPromptLexer(session),
        completer=AglCompleter(session),
        history=_make_history(history_path),
        style=get_style(theme),
        multiline=True,
        key_bindings=_make_key_bindings(),
        prompt_continuation=_prompt_continuation,
        input=input,
        output=output,
    )


def _prompt_continuation(width: int, line_number: int, wrap_count: int) -> StyleAndTextTuples:
    """Render the continuation prompt."""
    del width, line_number, wrap_count
    return [("class:agl.prompt", CONTINUATION)]


def run_console(
    session: ReplSession,
    *,
    echo: bool = True,
    echo_unit: bool = False,
    history_path: Path | None = None,
    theme: str = "auto",
    on_setting_save: Callable[[str, str | bool], None] | None = None,
    input: Input | None = None,
    output: Output | None = None,
) -> None:
    """Wire prompt_toolkit to the shared loop, including live theme persistence."""
    prompt_session = build_prompt_session(
        session, theme=theme, history_path=history_path, input=input, output=output
    )

    def on_setting_change(key: str, value: str | bool) -> None:
        if key == "theme" and isinstance(value, str):
            prompt_session.style = get_style(value)
        if on_setting_save is not None:
            on_setting_save(key, value)

    def highlighted_writer(text: str, agl_ranges: tuple[tuple[int, int], ...]) -> None:
        # Avoid prompt_toolkit's process-global cached output under redirection.
        print_formatted_text(
            FormattedText(_highlighted_agl_fragments(text, session, agl_ranges)),
            style=prompt_session.style,
            file=sys.stdout,
        )

    run_repl_loop(
        session,
        reader=prompt_session.prompt,
        writer=print,
        echo=echo,
        echo_unit=echo_unit,
        theme=theme,
        on_setting_change=on_setting_change,
        highlighted_writer=highlighted_writer,
    )
