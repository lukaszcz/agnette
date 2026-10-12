# AgL Highlighting

`agnette.agl.highlight` is UI-free. `categories.py` defines the shared `Category`
vocabulary and half-open `Span` ranges; `TEXT` covers trivia. Front ends own
category-to-style mappings.

## Tiers and Consumers

- `lexical.py` classifies recovery-mode lexer tokens, declaration sites, attributes,
  and optionally known type/constructor names. Recovery supplies tokens, diagnostics,
  and skipped comments in one scan while retaining the public token API. Spans
  partition the original source, including malformed input and normalized-newline
  offset mapping. Lexer delimiters define raw literal backgrounds; code, holes, and
  escapes overlay them. Cooked/dedented
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
Keyword helpers derive boundaries from `IDENT_STOP` and retain canonical inventories
in `Rule.words`. `grammar_eval.py` interprets the generic model into partitioning spans.

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

### Generated Targets

`targets.TARGETS` maps names to pure `Grammar → str` generators; extending it needs
one module and one registry entry. `targets/kde.py` emits deterministic XML with
canonical keyword lists, category item data, integer release versions (major ×
1000000 + minor × 1000 + patch), human release comments, and native push/pop/switch
contexts. Only exact token-start keyword-helper patterns without captures use
native keyword matching; other `Rule.words` remain inventory metadata, with matching
lowered as regex. Identifier-stop guards and regex fallbacks preserve punctuation-bearing
and Unicode identifiers without `weakDeliminator`. `targets/_regex.py` supplies structural lowering facts.

KDE cannot style regex captures directly. Shared finite stages partition linear,
top-level captures and background gaps; suffix lookaheads retain ordered matching,
and only the final stage applies the rule action. Optional captures are supported;
nullable whole capture rules, repeated/nested selected captures and top-level
alternatives are rejected. All built-in contextual captures fit this subset.
Native line-end/empty-line actions implement unconditional ends, chained pops and
empty-line transitions; nullable context-dependent end actions are rejected.
Nullable transitions pair consuming matches with explicit lookahead fallbacks.
The target accepts `^` only as the leading atom with no top-level alternatives;
other anchor forms and all lookbehind raise `ValueError`: native engines may lose
preceding consumed text. Generic keyword helpers with `token_start=False` therefore
require a caller-owned token-start alternative for export. `targets/_kde_contract.py` rejects
base-frame pops and potential zero-width cycles involving push/switch, including
returns through pops. Reachability conservatively ignores pattern feasibility;
pop-only recursion remains supported. The contract also rejects XML 1.0-forbidden
literal characters in regexes, state names and keyword items before serialization.
It reuses `grammar.validate_pattern` with PCRE2's repeat-bound limit of 65535;
the generic model retains 100000. Braced group repetitions with any bound >1
are rejected: PCRE2 expands group bodies and can exceed compiled-regex size limits
even within the numeric cap. Atom/class bounds retain the cap; group bounds 0/1
and unbounded `*`/`+` remain supported. These lowering restrictions prevent known
unsupported repeat expansion without runtime engine dependencies or size heuristics.
Serialized text encodes CR as a character reference to preserve keyword metadata
through XML newline normalization. Regexes remain line-local; defaults style unmatched characters.
`tests/test_agl_highlight_targets.py` and `test_agl_highlight_kde_contract.py` check
structure, inventories, staged transitions and target rejection cheaply, without
launching external engines.

### Contextual Approximation Contract

The portable grammar adds three capture rules and one path rule, no contextual
states or second classifier. They apply in CODE, brackets and holes, never literal text/comments.
All names use canonical identifier boundaries; hard keywords are excluded.

| Construct | Guaranteed local behavior | Deliberate limitation |
| --- | --- | --- |
| Attributes | `@` plus same-line horizontal whitespace and an identifier → `ATTRIBUTE`. | No cross-line lookahead or argument transparency: arguments remain ordinary CODE, including declaration captures. |
| Modules | Contiguous identifier paths separated by `/`, optionally ending in `/*` and/or `::`; a single identifier followed by `::` → `MODULE`. | Lexical anywhere, not restricted to import/use/export headers; whitespace/comments interrupt paths; same-line `:: [` type arguments retain core call/name categories. |
| Type declarations | Identifier immediately after `record`, `enum`, `type`, or `exception`, separated by same-line horizontal whitespace → `TYPE`. | No intervening attributes/comments, symbolic names, or later reference seeding. |
| Enum variants | Identifier immediately after standalone `\|` with optional same-line horizontal whitespace → `CONSTRUCTOR`. | No enum-owner tracking: the same local shape outside an enum is also promoted; symbolic/hardword variants are not. |
| Softwords | Canonical operator softwords → `OPERATOR_WORD`; other canonical softwords → `KEYWORD`, except where earlier contextual captures apply. | Inventory coloring even in fields, named arguments and member positions; no layout/header/recovery history. Infix `at` remains `NAME`. |

`tests/test_agl_highlight_portable_context.py` independently checks these features
and named boundaries. The corpus comparison applies the **same** coarse projection
to both outputs: contextual/semantic name roles become plain names while canonical
builtin types/calls retain their categories; attribute/module punctuation (including
wildcard `/*`, even when the scanner treats a local path suffix as `NAME`) is
source-normalized to operators in styled code. Softword role differences and
contextual infix `at` are coarsened by spelling; wildcard `_` becomes a name.
Projection rescans styled code spans only, never replays promotion history or reads
generated rules. It does not collapse core keyword, literal, escape, interpolation,
environment-hole, number, placeholder, error, or other punctuation categories.
Mutation tests ensure unrelated core mistakes still fail comparison. Fine contextual
equality is intentionally not a corpus requirement; the independent examples own
that contract.

`tests/test_agl_highlight_conformance.py` compares each corpus item once with
original-source offsets. Named, witnessed core divergences cover
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
match. Buffered-hole exemptions require per-position recovery-token evidence from
nonbuffering quoted fragments, using the same coarse name projection. Candidates
come only from source spelling, never compared categories. Lexer probes validate
literal ownership; parent BODY tokens retain nested delimiter/code facts, and lexer
closers and owner/tail bounds exclude recovered code. Rejected verbatim exemptions
require a real opener/diagnostic extent and independent per-position recovery evidence.
Uniform-margin probes expose layout-rejected bodies while preserving payload spelling,
interpolation, and nested errors; only evidence-matching categories or literal
line-delimiter/blank-indentation `TEXT` qualify. Projection reuses each recovery
scan's skipped comments.
Exemptions never cover whole files.

## Layering

Categories are a vocabulary leaf; lexical recognition depends on the lexer and
language vocabulary, never the REPL or UI libraries. Lower language layers do not
import highlighting. Optional Pygments/Markdown imports belong only in their adapter
modules. `tests/test_agl_dependencies.py` enforces these fences;
`tests/test_agl_highlight_lexical.py` covers categories, partitions, and totality;
`tests/test_agl_repl_highlight_compatibility.py` checks parent-corpus character/style parity.
