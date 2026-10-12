# Native KDE highlighting checks

Run `just test-kde` with **Pandoc 3.x** on `PATH`. Missing Pandoc produces a skip;
other versions fail. No additional Python dependencies are required beyond the
repository environment. The recipe generates `build/kde/agl.xml` through
`uv run agm dev highlight kde`, runs the manual harness regressions, then compares
Pandoc/skylighting HTML with the portable `grammar_eval` reference. Generated
files are ignored, not committed. This suite is outside `just check`.

The HTML decoder preserves entities, Unicode character offsets, tabs, nested
line wrappers, and unstyled trivia. A terminal sentinel preserves trailing blank
lines and empty sources; its style is excluded from comparison. Pandoc/skylighting
normalizes CRLF and bare CR to LF. The KDE coordinator explicitly normalizes input
before both Pandoc and reference evaluation, then requires an exact normalized-source
round trip and gap-free, nonoverlapping partitions; it never strips arbitrary text.
Other engines retain the engine-neutral original-source contract.

`HTML_KDE_STYLES` in `tools/highlight_kde_support.py` maps Pandoc token classes to KDE
styles. `KDE_STYLES` in the generator supplies the inverse category quotient:
normal text/names/punctuation/placeholders, keywords/operator words,
constructors/functions, parameters/locals, and escapes/interpolation/env holes
are indistinguishable within each group. Both actual and reference categories
receive this same projection; no exact lexer or semantic comparison is involved.
Unknown or non-emitted styles fail rather than becoming plain text.

## Shared native-harness API

`tools/highlight_engine_support.py` has no engine dependencies. Import it as
`tools.highlight_engine_support` with the repository root on `sys.path`.
`tools/highlight_kde_support.py` shares style projection, HTML decoding, Pandoc
invocation and reference comparison between the optional CLI harness and normal
native generator tests. Run the CLI with `uv run python -m tools.highlight_kde XML`.

- `CORPUS: tuple[CorpusCase, ...]` contains three existing AgL programs, the first
  three AgL fences from each of two reference documents, and named lexical inputs.
  `NEWLINE_CASES` covers original LF, CRLF and bare CR in strings, nested interpolation,
  attributes and Unicode, with leading/trailing trivia retained.
  `collect_corpus(root)` recollects this small subset; hidden context is not rendered.
- `CorpusCase(name, source)` names an untouched displayed source.
- `expected_spans(source, grammar=AGL_GRAMMAR)` returns reference `Category` spans.
- `EngineResult(source, spans)` uses a tuple of half-open Unicode character spans,
  including trivia. Empty source has an empty span tuple.
- `spans_from_categories(categories)` coalesces per-character categories.
- `assert_result(case, result, projection=None, *, grammar=AGL_GRAMMAR)` checks text,
  both partitions, and per-character equality. A projection must map every
  `Category`; the default is identity (appropriate for distinct TextMate scopes).

The shared lexical table covers strings, escapes, environment holes, nested
interpolation/brackets, attributes, declaration/capture boundaries, paths,
softwords, builtin calls, numbers, Unicode identifiers, and multiline recovery
in the built-in grammar's shared 11-state native framing. It intentionally
includes fragments and rejected input: the reference is the grammar, not the
exact scanner's recovery policy.
