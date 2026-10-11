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
  tokens without changing strict lexing; synthetic loop terminators own no text.
  Rejected opener extents terminate literal backgrounds before recovered code;
  deferred bracketed `$` markers with no payload create no literal background.
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
`markdown_ext.py` registers `agl_highlight` via `markdown.extensions`. Its early
preprocessor consumes adjacent context markers using `fence_metadata`, preserving
check markers and list anchors, and skipping fenced bodies/HTML comments. It probes
SuperFences' configured header-validation pipeline before modifying text; rejected
or uninjectable headers retain authored markers. A superfences validator passes
decoded context to a delegating formatter; the adapter's `ContextVar` supplies
the lexer default, with explicit options taking precedence and fence-local reset.
`mkdocs.yml` enables the extension and routes every `agl` fence through these custom
callbacks, retaining default HTML, highlighting options, and container handling.
`docs/stylesheets/agl.css` distinguishes type, constant, interpolation, and error
tokens using Material's palette; `tests/test_agl_highlight_markdown.py` covers rendering.

## Document Context

`context.fence_metadata` is the shared parser for adjacent `agl-check`/`agl-context`
comments for backtick/tilde fences with plain or attribute-style AgL headers.
Attribute-language recognition follows Markdown's permissive lexical semantics
without importing UI dependencies; option validation belongs to the adapter.
`MarkdownContainer` shares list/blockquote detection, continuation prefixes, and
body boundaries; consumers never extend an unclosed fence beyond its container.
`FenceMetadata` shares closing-fence recognition and option injection. The snippet
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

`agl_grammar.py` supplies the lexical grammar from canonical keyword, builtin,
escape and identifier inventories. Explicit Unicode ranges cover identifiers and
operator names. Recursive strings and brackets use ordered single-frame push/pop
rules. Each literal has a finite hole state: entering a hole switches the literal
frame, closing it switches back, and line-end recovery pops unfinished holes and
nested braces. This also abandons a multiline host without a stack reset.
Invalid escapes switch to a line-end rejected state. CODE tracks all brackets across
lines; holes track only curly nesting, ignoring mismatched parentheses/squares.
Bracketed code and holes reject `$` without opening a payload; holes also exclude
root comments. Verbatim blocks
approximate dedent at column zero and render blank-line indentation as trivia. Token-start keyword
rules need only the following identifier boundary because names are consumed whole.

Rules match each line without its LF, CRLF or CR delimiter: captures, anchors and
lookarounds cannot see adjacent lines. Delimiters always receive `TEXT`; other
Unicode separators remain content. Original offsets and the state stack survive
line transitions. Empty lines and line ends (including EOF) run zero-width actions.
Zero-width push/switch and base-frame pop rules run once per `(state, rule)` at
an offset. Other pops decrease depth and can unwind repeated recursive frames;
nontransitioning empty matches are skipped. Pop removes one frame; Switch replaces
only the top. The built-in grammar is directly exportable to native TextMate/KDE
states: finite transitions and chained line-local ends require neither arbitrary
stack reset nor target rejection. Exact scanner recovery of arbitrarily nested
rejected literals is not expressible by these online states.

`tests/test_agl_highlight_conformance.py` compares each corpus item once, projecting
contextual roles with original-source offsets. Named, witnessed divergences cover
literal newlines, relative verbatim dedent, interior blank indentation, rejected
verbatim layout, retroactive template diagnostics, buffered holes in incomplete
triple strings, and builtin-call names whose lookahead crosses lines.
`invalid-interpolation-escape-recovery` covers the scanner's overlapping nested-quote
background after its closer versus the grammar's `ERROR` suffix. Bounds are the
invalid escape diagnostic through the enclosing rejected literal's end, and only
`STRING`/`ERROR` differences qualify. Witnesses: `"%{ "bad\q" }"\nlet x=1` and
`$ hi %{ "bad\q" }\nlet x=1`. `deferred-verbatim-bracket-diagnostic` covers only the
`$` marker in an unfinished bracket (witness: `[$\n  x\nlet x=1`); the scanner delays
that diagnostic until brackets close. Payload and later standalone statements must
match. Exemptions never cover recovered code or whole files.

## Layering

Categories are a vocabulary leaf; lexical recognition depends on the lexer and
language vocabulary, never the REPL or UI libraries. Lower language layers do not
import highlighting. Optional Pygments/Markdown imports belong only in their adapter
modules. `tests/test_agl_dependencies.py` enforces these fences;
`tests/test_agl_highlight_lexical.py` covers categories, partitions, and totality;
`tests/test_agl_repl_highlight_compatibility.py` checks parent-corpus character/style parity.
