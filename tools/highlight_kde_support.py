"""Shared Pandoc/KDE decoding and reference comparison for development probes."""

import subprocess
from html.parser import HTMLParser
from pathlib import Path

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Grammar
from agnette.agl.highlight.targets.kde import KDE_STYLES
from agnette.util.text import normalize_newlines
from tools.highlight_engine_support import (
    CORPUS,
    CorpusCase,
    EngineResult,
    assert_result,
    spans_from_categories,
)

# Pandoc HTML token classes are KDE default styles, not AgL categories.
HTML_KDE_STYLES = {
    "": "dsNormal",
    "kw": "dsKeyword",
    "dt": "dsDataType",
    "dv": "dsDecVal",
    "bn": "dsBaseN",
    "fl": "dsFloat",
    "cn": "dsConstant",
    "ch": "dsChar",
    "sc": "dsSpecialChar",
    "st": "dsString",
    "vs": "dsVerbatimString",
    "ss": "dsSpecialString",
    "im": "dsImport",
    "co": "dsComment",
    "do": "dsDocumentation",
    "an": "dsAnnotation",
    "cv": "dsCommentVar",
    "ot": "dsOthers",
    "fu": "dsFunction",
    "va": "dsVariable",
    "cf": "dsControlFlow",
    "op": "dsOperator",
    "bu": "dsBuiltIn",
    "ex": "dsExtension",
    "pp": "dsPreprocessor",
    "at": "dsAttribute",
    "re": "dsRegionMarker",
    "in": "dsInformation",
    "wa": "dsWarning",
    "al": "dsAlert",
    "er": "dsError",
}

# Invert through one canonical representative for each emitted KDE style.
# The same quotient is applied to reference and observed categories.
STYLE_CATEGORIES = {
    style: next(category for category in Category if KDE_STYLES[category] == style)
    for style in set(KDE_STYLES.values())
}
KDE_PROJECTION = {category: STYLE_CATEGORIES[KDE_STYLES[category]] for category in Category}


class _CodeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[EngineResult] = []
        self.active = False
        self.styles: list[Category | None] = []
        self.text: list[str] = []
        self.colors: list[Category] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "code":
            self.active = True
            self.text = []
            self.colors = []
            self.styles = []
        elif self.active and tag == "span":
            classes = (dict(attrs).get("class") or "").split()
            category: Category | None = None
            for css_class in classes:
                style = HTML_KDE_STYLES.get(css_class)
                if style is None or style not in STYLE_CATEGORIES:
                    raise ValueError(f"Unexpected Pandoc style: {css_class}")
                category = STYLE_CATEGORIES[style]
            self.styles.append(category)

    def handle_endtag(self, tag: str) -> None:
        if self.active and tag == "span":
            self.styles.pop()
        elif self.active and tag == "code":
            self.results.append(
                EngineResult("".join(self.text), spans_from_categories(self.colors))
            )
            self.active = False

    def handle_data(self, data: str) -> None:
        if self.active:
            self.text.append(data)
            category = next(
                (style for style in reversed(self.styles) if style is not None), Category.TEXT
            )
            self.colors.extend([category] * len(data))


def parse_html(html: str) -> tuple[EngineResult, ...]:
    """Decode code blocks/entities; retain trivia and nested line-wrapper text."""
    parser = _CodeParser()
    parser.feed(html)
    parser.close()
    return tuple(parser.results)


_SENTINEL = "\nAGL_ENGINE_ROUNDTRIP_END"


def run_corpus(
    definition: Path,
    cases: tuple[CorpusCase, ...] = CORPUS,
    *,
    grammar: Grammar = AGL_GRAMMAR,
) -> None:
    """Compare exact LF-normalized text/styles: Pandoc normalizes CRLF and CR.

    Normalize before both Pandoc and reference evaluation, not in the shared
    engine-neutral contract: other engines can retain original delimiters.
    """
    cases = tuple(CorpusCase(case.name, normalize_newlines(case.source)) for case in cases)
    # A final sentinel keeps terminal blank lines (and empty inputs) inside the
    # fence: Pandoc normally drops a fence's terminal LF. Never compare its style.
    markdown = "\n".join(
        "~~~~~~~~~~~~agl\n" + case.source + _SENTINEL + "\n~~~~~~~~~~~~\n" for case in cases
    )
    process = subprocess.run(
        ["pandoc", "--preserve-tabs", "--syntax-definition", str(definition), "-t", "html"],
        input=markdown,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=30,
    )
    results = parse_html(process.stdout)
    assert len(results) == len(cases), ("code block count", len(results), len(cases))
    for case, result in zip(cases, results, strict=True):
        assert result.source == case.source + _SENTINEL, (case.name, "fence roundtrip failed")
        spans = tuple(
            (start, min(end, len(case.source)), category)
            for start, end, category in result.spans
            if start < len(case.source)
        )
        assert_result(case, EngineResult(case.source, spans), KDE_PROJECTION, grammar=grammar)
