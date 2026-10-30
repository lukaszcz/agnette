"""Reuse immutable source facts only during the controller's coverage summary.

Private parser fields reviewed against coverage 7.13; summary against pytest-cov 7.1.
Recheck these interfaces when upgrading, independently of measurement metadata.
"""

from __future__ import annotations

import ast
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from threading import Lock
from typing import TYPE_CHECKING, Protocol, TextIO, cast

if TYPE_CHECKING:
    import pytest
    from coverage.parser import PythonParser

type _Key = tuple[str, str, str | None]
type _Arc = tuple[int, int]
_LAZY_AST = "_agm_report_lazy_ast"


@dataclass(frozen=True, slots=True)
class _Branches:
    arcs: frozenset[_Arc]
    fragments: tuple[tuple[_Arc, tuple[tuple[str | None, str | None], ...]], ...]
    fixers: tuple[tuple[_Arc, tuple[_Arc, _Arc]], ...]

    @classmethod
    def freeze(cls, parser: PythonParser) -> _Branches:
        assert parser._all_arcs is not None and parser._missing_arc_fragments is not None
        return cls(
            frozenset(parser._all_arcs),
            tuple((arc, tuple(parts)) for arc, parts in parser._missing_arc_fragments.items()),
            tuple(parser._with_jump_fixers.items()),
        )

    def restore(self, parser: PythonParser) -> None:
        parser._all_arcs = set(self.arcs)
        parser._missing_arc_fragments = defaultdict(
            list, ((arc, list(parts)) for arc, parts in self.fragments)
        )
        parser._with_jump_fixers = dict(self.fixers)


@dataclass(frozen=True, slots=True)
class _Source:
    statements: frozenset[int]
    excluded: frozenset[int]
    raw_statements: frozenset[int]
    raw_excluded: frozenset[int]
    raw_docstrings: frozenset[int]
    multiline: tuple[tuple[int, int], ...]
    branches: _Branches | None = None

    @classmethod
    def freeze(cls, parser: PythonParser) -> _Source:
        return cls(
            frozenset(parser.statements),
            frozenset(parser.excluded),
            frozenset(parser.raw_statements),
            frozenset(parser.raw_excluded),
            frozenset(parser.raw_docstrings),
            tuple(parser.multiline_map.items()),
        )

    def restore(self, parser: PythonParser) -> None:
        parser.statements = set(self.statements)
        parser.excluded = set(self.excluded)
        parser.raw_statements = set(self.raw_statements)
        parser.raw_excluded = set(self.raw_excluded)
        parser.raw_docstrings = set(self.raw_docstrings)
        parser.multiline_map = dict(self.multiline)
        # Arcs remain lazy, including on line-only report hits.
        parser._all_arcs = None
        parser._missing_arc_fragments = None
        parser._with_jump_fixers = {}
        parser._ast_root = None
        setattr(parser, _LAZY_AST, True)


class Cache:
    """Bound source retention; source character budget is not an RSS bound."""

    def __init__(self, limit: int = 512, source_limit: int = 8 * 1024 * 1024) -> None:
        self.limit = limit
        self.source_limit = source_limit
        self.entries: OrderedDict[_Key, _Source] = OrderedDict()
        self.source_chars = 0
        self.lock = Lock()
        self.closed = False

    def get(self, key: _Key) -> _Source | None:
        with self.lock:
            result = self.entries.get(key)
            if result is not None:
                self.entries.move_to_end(key)
            return result

    def put(self, key: _Key, value: _Source) -> None:
        if len(key[1]) > self.source_limit or not self.limit:
            return
        with self.lock:
            if self.closed:
                return
            old = self.entries.get(key)
            if old is not None:
                # Duplicate concurrent parsing must not overwrite completed arcs.
                if value.branches is None:
                    return
                self.source_chars -= len(key[1])
            self.entries[key] = value
            self.entries.move_to_end(key)
            self.source_chars += len(key[1])
            while len(self.entries) > self.limit or self.source_chars > self.source_limit:
                removed, _ = self.entries.popitem(last=False)
                self.source_chars -= len(removed[1])

    def close(self) -> None:
        """Release snapshots, including in explicitly inherited thread contexts."""
        with self.lock:
            self.closed = True
            self.entries.clear()
            self.source_chars = 0


active: ContextVar[Cache | None] = ContextVar("agm-coverage-report-sources", default=None)
_installed = False


@contextmanager
def scope(cache: Cache) -> Generator[Cache, None, None]:
    token = active.set(cache)
    try:
        yield cache
    finally:
        active.reset(token)


def install() -> None:
    """Install once on the controller; inactive calls retain coverage's behavior."""
    global _installed
    if _installed:
        return
    from coverage.parser import PythonParser

    original_parse = PythonParser.parse_source
    original_analyze = PythonParser._analyze_ast

    def parse_source(parser: PythonParser) -> None:
        cache = active.get()
        if cache is None or parser.show_tokens:
            original_parse(parser)
            return
        # PythonParser.__init__ still reads/decodes source before every lookup.
        key = (parser.filename, parser.text, parser.exclude)
        saved = cache.get(key)
        if saved is None:
            original_parse(parser)
            cache.put(key, _Source.freeze(parser))
        else:
            saved.restore(parser)

    def analyze_ast(parser: PythonParser) -> None:
        cache = active.get()
        key = (parser.filename, parser.text, parser.exclude)
        deferred = cast(bool, getattr(parser, _LAZY_AST, False))
        saved = (
            cache.get(key)
            if cache is not None
            and not parser.show_tokens
            and (parser._ast_root is not None or deferred)
            else None
        )
        if saved is not None and saved.branches is not None:
            saved.branches.restore(parser)
            return
        # This wrapper must stay installed: a line-only parser can escape summary.
        # Only cache hits carry the marker; ordinary unparsed parsers still fail normally.
        if deferred:
            parser._ast_root = ast.parse(parser.text)
            delattr(parser, _LAZY_AST)
        original_analyze(parser)
        if cache is not None and not parser.show_tokens:
            cache.put(
                key, replace(saved or _Source.freeze(parser), branches=_Branches.freeze(parser))
            )

    setattr(PythonParser, "parse_source", parse_source)
    setattr(PythonParser, "_analyze_ast", analyze_ast)
    _installed = True


class _Controller(Protocol):
    summary: Callable[[TextIO], float]


def configure(config: pytest.Config) -> None:
    """Wrap only this controller's summary, not measurement or worker reporting."""
    if hasattr(config, "workerinput"):
        return
    plugin = cast(object, config.pluginmanager.getplugin("_cov"))
    controller = cast(_Controller | None, getattr(plugin, "cov_controller", None))
    if controller is None:
        return
    original = controller.summary

    def summary(stream: TextIO) -> float:
        install()
        cache = Cache()
        try:
            with scope(cache):
                return original(stream)
        finally:
            cache.close()

    controller.summary = summary
    config.add_cleanup(lambda: setattr(controller, "summary", original))
