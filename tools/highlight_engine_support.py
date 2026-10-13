"""Engine-neutral corpus and source-position contract for optional native suites."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category, Span
from agnette.agl.highlight.context import fence_metadata
from agnette.agl.highlight.grammar import Grammar
from agnette.agl.highlight.grammar_eval import evaluate

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class CorpusCase:
    """Stable diagnostic label and untouched displayed source (not hidden context)."""

    name: str
    source: str


@dataclass(frozen=True, slots=True)
class EngineResult:
    """Round-tripped text and a complete, ordered half-open Category partition."""

    source: str
    spans: tuple[Span, ...]


# Deliberately lexical inputs: they need not parse or satisfy exact lexer semantics.
LEXICAL_CASES = (
    CorpusCase("empty", ""),
    CorpusCase("trivia-roundtrip", "\n\t # <&> Ω\n\nlet x = 1\n\n"),
    CorpusCase(
        "core", "# comment\nlet n = 12.34 + 56\ntrue false null () [] {} : , . ; := :: ? ?12 _"
    ),
    CorpusCase(
        "escapes-env", r'"hello\n\u0041\uD83D\uDE00\%${HOME} %${HOME} ${HOME} %{ {1: "nested"} }"'
    ),
    CorpusCase("single-quote", "'hello\\' %{ {1: 'nested'} }'"),
    CorpusCase("triple-double", '"""one\ntwo %{ {1: "nested"} }\nthree""" let x = 1'),
    CorpusCase("triple-single", "'''one\ntwo %{ {1: 'nested'} }\nthree''' let x = 1"),
    CorpusCase(
        "verbatim-inline",
        r'$ hello \%{literal} \%${HOME} %${HOME} ${HOME} %{ {1: "nested"} } # literal   '
        + "\nnext",
    ),
    CorpusCase("verbatim-block", '$\n  hello %{ {1: "nested"} }\n\n  %${HOME} ${HOME}\nnext(1)'),
    CorpusCase("nested-interpolation", '"%{ {1: {2: {3: "%{4}"}}} }" let after = 1'),
    CorpusCase(
        "attributes-declarations",
        "@ field record Thing | Some\nexception Problem\ntype Alias enum Choice",
    ),
    CorpusCase("capture-boundaries", "@\trecord @true record true | enum @\nrecord\nThing"),
    CorpusCase("context-in-hole", '"a%{ record Thing @ field | Some }b"'),
    CorpusCase("paths", "import std/text std/* std/text::split foo::[int](1) a/b/*::"),
    CorpusCase(
        "softwords", "scope x\nend\nx and y or not z\nlet and = 1\nf(with = 2)\ninfixl + at 5"
    ),
    CorpusCase("builtins", "int decimal str bool print(1) print::[int](1) print-value print"),
    CorpusCase(
        "identifier-boundaries", 'resource-path record-id αname foo"bar a+b ++ <=> 123abc １２'
    ),
    CorpusCase("keyword-regression", "1if 1true 1int 1print(2) +if resource+if"),
    CorpusCase(
        "builtin-regression", "resource-path resource-dir( resource( as? a+b xrecord record-id"
    ),
    CorpusCase(
        "unicode", "𐐀name + 😀 ++ ≤ λname _name name+tail ${HOME}\nrecord Ωmega @ attr | Variant"
    ),
    CorpusCase("recursive-brackets", "(let x = [{1: [2, (3)]}]\nprint(x)) let after = 4"),
    CorpusCase("single-line-recovery", '"unfinished\nlet x = 2'),
    CorpusCase("escape-recovery", '"bad\\q\nlet x = 3'),
    CorpusCase("triple-hole-recovery", '"""%{1\ntail""" let x = 1'),
    CorpusCase(
        "triple-escape-recovery", '"""bad\\q consumed"""\ntail\\n %{2} %${HOME}\n""" let x = 1'
    ),
    CorpusCase("deep-hole-recovery", '"""%{ {{{x\nlet y = 2'),
    CorpusCase("verbatim-hole-recovery", "$\n  hi %{1\n  tail\nlet x = 1"),
    CorpusCase("successive-triple-holes", '"""%{1\ntail %{2\nmore\nend""" let after = 3'),
    CorpusCase(
        "successive-single-triple-holes",
        "'''%{1\ntail %{let n=2 + '%{3}'} %{4\nmore\nend''' let after=3",
    ),
    CorpusCase("successive-escape-holes", '"""bad\\q\ntail %{2\nmore %{3\nend""" let after = 3'),
    CorpusCase("successive-verbatim-holes", "$ \n  hi %{1\n  tail %{2\n  more\nlet after = 3"),
    CorpusCase("tab-verbatim-recovery", "$\t\n\thi %{1\n\ttail %{2\n\tmore\nlet after = 3"),
    CorpusCase("bracketed-verbatim-marker", "[$\n  x]\nlet after = 1"),
    CorpusCase("deferred-verbatim-marker", "[$\n  x\nlet after = 1"),
)


# Original delimiters belong to the engine-neutral contract. KDE alone converts
# them before execution; TextMate adapters can compare these sources unchanged.
NEWLINE_CASES = tuple(
    CorpusCase(f"{name}-{label}", source.replace("\n", newline))
    for name, source in (
        (
            "newline-strings",
            '\n\t# <&> 𐐀\n"""one\ntwo Ω\nthree"""\n'
            "'''four\nfive 𐐀\nsix'''\n\"unfinished\nlet after = 1\n\n",
        ),
        (
            "newline-nested-interpolation",
            '\n"""one %{ {1: "%{ {2: \'Ω\'} }"} }\n%{ {{{x\ntail %{2\nend"""\nlet after = 3\n\n',
        ),
        (
            "newline-attributes-unicode",
            '\n@\tattr record Ωmega | Variant\n"%{ @ field record 𐐀name }"\n'
            "@\nrecord\nThing\nimport std/text\n\t# <&> 😀\n\n",
        ),
    )
    for label, newline in (("LF", "\n"), ("CRLF", "\r\n"), ("CR", "\r"))
)


def _doc_cases(root: Path, relative: str, limit: int) -> list[CorpusCase]:
    lines = (root / relative).read_text(encoding="utf-8").splitlines()
    cases: list[CorpusCase] = []
    index = 0
    while index < len(lines) and len(cases) < limit:
        metadata = fence_metadata(lines, index)
        if metadata is None:
            index += 1
            continue
        opening = index
        index += 1
        body: list[str] = []
        while index < len(lines):
            content = metadata.container.content(lines[index])
            if content is None or metadata.closes(content):
                break
            body.append(content)
            index += 1
        cases.append(CorpusCase(f"{relative}:{opening + 1}", "\n".join(body)))
        index += 1
    return cases


def collect_corpus(root: Path = REPO_ROOT) -> tuple[CorpusCase, ...]:
    """Small real-source subset plus explicit lexical/native-frame regressions."""
    files = (
        "tests/agl/multi_file/entry_qualified.agl",
        "tests/agl/multi_file/utils/strings.agl",
        "packages/stdlib/src/option.agl",
    )
    cases = [CorpusCase(name, (root / name).read_text(encoding="utf-8")) for name in files]
    for relative in (
        "docs/agl/reference/functions.md",
        "docs/agl/reference/lexical-structure.md",
    ):
        cases.extend(_doc_cases(root, relative, 3))
    return (*cases, *LEXICAL_CASES, *NEWLINE_CASES)


CORPUS = collect_corpus()


def expected_spans(source: str, grammar: Grammar = AGL_GRAMMAR) -> tuple[Span, ...]:
    """Reference grammar categories, never exact lexer or semantic classifications."""
    return tuple(evaluate(grammar, source))


def spans_from_categories(categories: Sequence[Category]) -> tuple[Span, ...]:
    """Coalesce per-character categories without discarding unstyled text."""
    spans: list[Span] = []
    offset = 0
    for category, group in groupby(categories):
        end = offset + sum(1 for _ in group)
        spans.append((offset, end, category))
        offset = end
    return tuple(spans)


def _partition(source: str, spans: Sequence[Span], label: str) -> list[Category]:
    colors: list[Category] = []
    for start, end, category in spans:
        assert start == len(colors) and start < end <= len(source), (label, start, end)
        colors.extend([category] * (end - start))
    assert len(colors) == len(source), (label, "incomplete partition")
    return colors


def assert_result(
    case: CorpusCase,
    result: EngineResult,
    projection: Mapping[Category, Category] | None = None,
    *,
    grammar: Grammar = AGL_GRAMMAR,
) -> None:
    """Require exact text/partitions, then symmetric target-observable equality.

    Projection must cover every Category; omitted projection means identity.
    Native output adapters use Unicode character offsets, not byte offsets.
    """
    assert result.source == case.source, (case.name, "source roundtrip failed")
    actual = _partition(result.source, result.spans, case.name)
    expected = _partition(case.source, expected_spans(case.source, grammar), case.name)
    if projection is not None:
        actual = [projection[category] for category in actual]
        expected = [projection[category] for category in expected]
    for offset, (observed, wanted) in enumerate(zip(actual, expected, strict=True)):
        assert observed == wanted, (
            case.name,
            offset,
            repr(case.source[max(0, offset - 25) : offset + 25]),
            observed,
            wanted,
        )
