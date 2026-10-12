# AgL Syntax: Lexer, Parser, and AST

A hand-written lexer handles layout (INDENT/DEDENT), string templates (`%{}` holes, `${NAME}` environment holes, `$` verbatim literals), and slash-path/`::` qualifier syntax. A Lark LALR grammar recognizes the token stream; the AST builder constructs frozen nodes with stable ids. Comments expose spans through a side channel, not tokens.

`lexer.tokenize_recover` returns tokens and ordered diagnostics for highlighting malformed input; parser lexing stays strict. Accepted input retains the strict token stream. Error and incomplete-literal tokens preserve source identity; the shared [highlighter](../../highlighting.md) projects overlapping payload spans into a partition. REPL completion's raw-quote compatibility policy uses the same scanner independently of AgL template recognition.

## Shared Lexical Leaves

- **Literal scanning** — escape, number, identifier, and environment-hole rules live in `agl/value_syntax/lexical.py`, shared by the lexer and the host value-syntax reader; it also spells values back as literals so runtime rendering and constant folding cannot diverge. All template forms share one hole scanner and one `template` grammar rule.
- **Keywords** — `keywords.py` is the single inventory of reserved and soft keywords (soft keywords are ordinary names outside their promotion window). The lexer, shared highlighter, portable grammar and generated Emacs inventory import it; the parser grammar's literals and micro mode mirror it by hand.
- **Operator position** — `lexer/operators.py` is the one inventory of operand-closing/opening tokens; layout line-continuation and operator-word promotion both query it, so they cannot disagree.

## Constant Folding

`syntax/constants.py` is the syntactic constant-expression predicate; `syntax/module_constants.py` folds a module's own constants to text from its AST alone, resolving references by the same-module part of whole-path lookup ([scope.md](scope.md#full-path-lookup)). Needing no resolver lets the scope pass and the parse-only package command scan ([packages.md](../../packages.md)) fold attribute arguments identically — and is why a constant reference reaches only its declaring module. The same module supplies the comparison key for the checker's static duplicate dict-key check.

## What the AST Preserves

The AST records source structure faithfully so later passes never reconstruct spellings:

- A builtin-operator chain is grouped at parse; one containing a user operator stays a raw infix chain for scope to group ([scope.md](scope.md#operators)).
- Qualified expressions, types, patterns, and `is` tests share one qualifier-chain node with per-segment spans and type arguments.
- Declarations carry canonical scope paths, method receivers (including applied builtin receivers), constraint blocks (`agl/constraints.py`), and `let`/`var` and assignment-target shapes.
- A partial-application placeholder is a call argument, never an expression; a leading-dot chain becomes a marked unary lambda over a hygienic receiver.
- Attributes are held verbatim; the AST never imports the attribute catalog (`agl/attributes.py`), and meaning is assigned by scope ([scope.md](scope.md#attribute-recognition)).

Inline `agm exec -c` sources parse through `PipelineDriver.parse_entry(inline_code=True)`: unless the source declares a `program def`, a purely syntactic wrapper (`parser/wrap.py`) keeps declarations, path-bearing and `@param` bindings, and the plain bindings they read at the module root, moving everything else into a synthetic entry. REPL entries are not wrapped; their root keeps executable items.

## Code Entry Points

- `src/agnette/agl/keywords.py`, `lexer/` — keyword inventory and lexing; `lexer/operators.py` operator position, `lexer/layout.py` INDENT/DEDENT and continuation.
- `src/agnette/agl/value_syntax/` — shared literal scanning and the host value-syntax reader.
- `src/agnette/agl/grammar/agl.lark`, `parser/` — grammar, AST construction, inline-source wrapping.
- `src/agnette/agl/syntax/` — AST nodes, spans, the generic visitor, constant predicate and module-constant folding, resource-call classification.
- `src/agnette/agl/attributes.py` — the built-in attribute catalog.
