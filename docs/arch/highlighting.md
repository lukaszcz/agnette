# AgL Highlighting

`agnette.agl.highlight` owns UI-free classification and portable grammar generation.
`categories.py` defines half-open source `Span`s and shared `Category` roles: trivia,
comments, keywords/constants, names (types, constructors, functions, builtins, modules,
attributes, parameters, locals), literals/escapes/holes, numbers, operators,
punctuation, placeholders, and errors. Each front end owns its style mapping.

## Exact Tiers and Front Ends

- `lexical.py` partitions original source using recovery-mode lexer tokens and
  comments, declaration/attribute rules, and optional known type/constructor names.
  Malformed input remains highlightable; strict compiler lexing is unchanged.
- `semantic.py` overlays scope-resolved names through `PipelineDriver` load/scope,
  reusing library artifacts without typechecking or evaluation. Scope side tables
  supply name identities; type-directed alternatives stay neutral. Failure falls
  back to lexical spans seeded from hidden context. `name_occurrences` exposes each
  name with its owning AST node, which editor navigation (`agl/navigation.py`) reuses.
- `context.py` prepends hidden source and projects spans back onto the displayed body.
- `repl/console.py` supplies session names and maps lexical categories to
  prompt_toolkit styles. Its compatibility adapter retains legacy error fallback
  and line clipping; the REPL does not run the semantic tier.
- `pygments_lexer.py` preserves source text and maps categories to semantic Pygments
  tokens. `AglLexer` accepts `context` and `semantic` (default on). Types map to
  `Keyword.Type`, constructors to `Name.Class`, modules to `Name.Namespace`, and
  locals/parameters to `Name.Variable`. `docs/stylesheets/agl.css` distinguishes
  type, constant, interpolation, and error classes using Material palette variables,
  never fixed colors.

`pyproject.toml` registers `agl` under `pygments.lexers` and `agl_highlight` under
`markdown.extensions`. The optional `highlight` extra supplies adapter dependencies;
`docs` and `dev` groups include it. Only these adapters import UI libraries;
`tests/test_agl_dependencies.py` enforces the layering.

## Document Context

`context.fence_metadata` shares adjacent `agl-context`/`agl-check` parsing across
Markdown rendering, snippet compilation, and `tools/agl_style.py`. It preserves AgL
indentation and Markdown list/blockquote boundaries. Hidden context participates in
compilation; layout-style checks apply only to displayed bodies. Authoring rules
are in `docs/agl/AGENTS.md`.

`markdown_ext.py` passes encoded hidden context through SuperFences validation and
its default formatter, preserving normal options/HTML and isolating each fence's
context. Rejected or uninjectable headers retain authored markers. `mkdocs.yml`
enables the extension and custom AgL fence; `tests/test_agl_highlight_markdown.py`
covers real rendering, containers, options, and context isolation.

## Portable Grammar and Targets

`grammar.py` defines immutable states, ordered regex/capture rules and push/pop/switch
transitions, validating portable patterns and state references. `grammar_eval.py` is
its line-local reference interpreter. `agl_grammar.py` derives keywords, builtins,
escapes and identifier boundaries from canonical language inventories. Its 11 states
use shared native push/pop frames for strings, brackets, interpolation and recovery.
Generated highlighting is **lexical approximation**, not scope-resolved semantics.

Local captures apply only in code, including brackets and interpolation:

| Construct | Approximation |
| --- | --- |
| Attributes | `@` and an identifier on the same line; arguments remain ordinary code. |
| Declarations | Identifier immediately after `record`, `enum`, `type`, or `exception`; no intervening attributes/comments or reference seeding. |
| Variants | Identifier immediately after standalone `\|`, without enum-owner tracking. |
| Modules | Contiguous slash paths, wildcard suffixes and qualifiers anywhere in code, not just module headers; qualifier type arguments remain call syntax. |
| Softwords | Inventory coloring regardless of layout/member/argument position; infix `at` remains a name. |

Verbatim dedent is approximated at column zero. Line delimiters are trivia.
Malformed interpolation or escapes consume the rejected child's remaining line;
line-end recovery pops that child, but a multiline literal host survives until its
own closer/dedent or EOF. The exact scanner instead abandons the rejected construct.
Single-line hosts unwind at line end. This shared native error-frame approximation
does not change the exact Pygments/REPL path.

`targets.TARGETS` registers pure `Grammar → str` generators: KDE XML (also Pandoc),
TextMate JSON (`source.agl`), Sublime YAML (TextMate scopes; also bat), and Emacs
vocabulary (`agl-inventory`). CLI help and
completion use registry keys; grammar construction is deferred until export.
Generic target restrictions are narrower than the model: KDE requires lowerable
linear captures, safe native transitions and PCRE2/XML-compatible patterns;
TextMate requires consuming push/match rules, pop-first frames, no switch/base pop,
and no LF-sensitive patterns; Sublime keeps rule order with a fallback per context
and requires consuming match/push/switch rules, no base-frame pop/switch, and no
LF-sensitive patterns. Contracts live in `targets/_kde_contract.py`,
`targets/_regex.py`, `targets/kde.py`, `targets/textmate.py`, and `targets/sublime.py`.
Emacs generates inventory only; its font-lock and micro's entire mode remain
hand-written. Generated files are not committed.

## Installation and Verification

`agm/commands/dev/highlight.py` exports definitions to stdout or a UTF-8 file.
`just install` runs `tools/install_highlighting.py` with the **installed** CLI:
refresh AgM-home/TextMate/Sublime and KDE definitions, register the Sublime syntax
with bat, preserve Pandoc defaults unless forced, and isolate prefix installs from
user home/XDG data and bat.
`tools/install_emacs_mode.py` generates inventory with the repository CLI before
packaging. Locations and Pandoc usage: [development tooling](../commands/dev.md).

`tests/test_agl_highlight_conformance.py` compares the grammar and exact lexical
tier under a symmetric contextual projection; independent local-context tests own
the finer approximation contract. Named, witnessed, source-bounded core divergences
and mutation probes keep exemptions narrow. Lexical, semantic, adapter and target
suites verify the remaining seams. Optional `just test-kde` (Pandoc/skylighting),
`just test-textmate` (TextMate/Oniguruma) and `just test-sublime` (bat/syntect)
compare real engines with the reference: `tools/highlight_engine_support.py`,
`tools/highlight_{kde,textmate,sublime}*.py`, and `config/{kde,textmate}/tests/`.
See [testing](testing.md) for quality gates and editor suites.
