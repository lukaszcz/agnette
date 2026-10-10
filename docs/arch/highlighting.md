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
- Function, parameter, and local categories are reserved for scope-resolved highlighting;
  the lexical tier uses name categories without invoking the pipeline.
- `repl/console.py` maps categories to prompt_toolkit styles. `repl_spans` shares
  classification but preserves legacy diagnostic clipping and plain-on-error fallback;
  the adapter presents each span only on its starting line. Completion uses the scanner's
  raw-quote compatibility policy, independent of AgL interpolation/verbatim recognition.
  The exact lexical API retains recovered code and distinguishes escapes.

## Layering

Categories are a vocabulary leaf; lexical recognition depends on the lexer and
language vocabulary, never the REPL or UI libraries. Lower language layers do not
import highlighting. Optional Pygments/Markdown imports belong only in their adapter
modules. `tests/test_agl_dependencies.py` enforces these fences;
`tests/test_agl_highlight_lexical.py` covers categories, partitions, and totality;
`tests/test_agl_repl_highlight_compatibility.py` checks parent-corpus character/style parity.
