# AgL Frontend

The frontend turns source text into a resolved, type-checked, match-compiled program in five static passes — lexer, parser, scope, typecheck, match compilation — over one shared AST. Nothing here evaluates.

## Pass Structure

Each pass wraps its input artifact rather than mutating it: parse → AST (raw user-operator chains retained); scope → resolved program (chains grouped); typecheck → checked program; match compilation → the artifact lowering consumes. AST nodes stay frozen and shared; pass conclusions live in side tables keyed by node id.

All passes report through one diagnostic channel (`agl/diagnostics.py`). `agl/recursion.py` turns stack exhaustion on over-deep source into an ordinary diagnostic ([hosting.md](../hosting.md)).

## What To Read Next

- [syntax.md](syntax.md) — the lexer, parser, and the AST firewall.
- [scope.md](scope.md) — name resolution.
- [types.md](types.md) — the semantic type model and the typecheck pass.
- [matchcompile.md](matchcompile.md) — the pattern-match compiler.
