# AgL Highlighting

`agnette.agl.highlight` is UI-free. `categories.py` defines the shared `Category`
vocabulary and half-open `Span` ranges; `TEXT` covers trivia. Front ends own
category-to-style mappings.

## Tiers and Consumers

- `lexical.py` classifies recovery-mode lexer tokens, declaration sites, attributes,
  and optionally known type/constructor names. Spans partition the original source,
  including malformed input and normalized-newline offset mapping. Lexer delimiters
  define raw literal backgrounds; code, holes, and escapes overlay them. Cooked/dedented
  fragment widths remain diagnostic-only. Projection handles overlapping, non-source-ordered
  tokens without changing strict lexing.
- `semantic.py` overlays scope-resolved names through `PipelineDriver` load/scope only,
  reusing its module/artifact caches; no typecheck or evaluation. AST spelling boundaries
  and resolved side tables classify declarations, generic parameters, member selections,
  aliases, scope labels, and references; type-directed alternatives remain neutral.
  Scope records non-expression name candidates in `ModuleResolution.name_references`:
  uses retain declaration-time visibility, import/export items read public surfaces,
  and infix headers read root bindings. Highlighting never re-resolves these names.
  Failure returns seeded lexical spans. `context.py` prepends hidden declarations/imports, seeds fallback names,
  and projects original offsets onto the displayed body.
- `repl/console.py` maps categories to prompt_toolkit styles. `repl_spans` shares
  classification but preserves legacy diagnostic clipping and plain-on-error fallback;
  the adapter presents each span only on its starting line. Completion uses the scanner's
  raw-quote compatibility policy, independent of AgL interpolation/verbatim recognition.
  The exact lexical API retains recovered code and distinguishes escapes.

## Documentation Adapter

`pygments_lexer.py` maps exact spans to semantic Pygments tokens, preserving source
text and filling gaps with `Text`. `AglLexer` accepts hidden `context` source and a
`semantic` toggle (default on). The `pygments.lexers` entry point `agl` discovers it;
the optional `highlight` extra supplies dependencies for the docs and dev groups.
MkDocs highlights `agl` fences through superfences; `docs/stylesheets/agl.css`
distinguishes type, constant, interpolation, and error tokens using Material's palette.

## Document Context

`context.fence_metadata` is the shared parser for adjacent `agl-check`/`agl-context`
comments, including list indentation and blockquotes, for backtick/tilde fences
with optional whitespace before `agl`. `FenceMetadata.body_line` normalizes
container prefixes, including blank/nested quotes, for both consumers. The snippet
compile guard prepends hidden context; `tools/agl_style.py` checks only displayed
fence bodies.

## Portable Grammars

`grammar.py` defines immutable named states, ordered regex/capture rules, and
push/pop/switch actions. Construction validates an explicit-character-set portable
regex subset, capture mappings, and state references; pushed states put pop first.
Braced quantifiers accept only `{n}`, `{n,}` and `{n,m}` with ASCII bounds ≤100000;
literal braces require escaping outside classes. Lookbehind excludes lookahead;
positive lookbehind excludes negative lookbehind, and negative lookbehind excludes
captures. Assertions cannot be repeated, including through noncapturing groups.
Keyword helpers derive boundaries from `IDENT_STOP`. `grammar_eval.py` interprets
the generic model into partitioning spans.

Rules match each line without its LF, CRLF or CR delimiter: captures, anchors and
lookarounds cannot see adjacent lines. Delimiters always receive `TEXT`; other
Unicode separators remain content. Original offsets and the state stack survive
line transitions. Empty lines and line ends (including EOF) run zero-width actions.
Each zero-width `(state, rule)` runs once per offset; revisits and nontransitioning
empty matches are skipped. Pop removes one frame; Switch replaces only the top.
Chained actions can pop distinct frames, but the revisit guard also applies to
recursive states. Target generators must faithfully lower stack operations and
zero-width progress or reject unsupported grammars, not silently approximate them.

## Layering

Categories are a vocabulary leaf; lexical recognition depends on the lexer and
language vocabulary, never the REPL or UI libraries. Lower language layers do not
import highlighting. Optional Pygments/Markdown imports belong only in their adapter
modules. `tests/test_agl_dependencies.py` enforces these fences;
`tests/test_agl_highlight_lexical.py` covers categories, partitions, and totality;
`tests/test_agl_repl_highlight_compatibility.py` checks parent-corpus character/style parity.
