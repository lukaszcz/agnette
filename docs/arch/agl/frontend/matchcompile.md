# AgL Match Compilation

The match compiler, the last static pass, turns every checked `case` into an immutable decision DAG that lowering executes. It consumes checked pattern metadata only.

## Compilation Model

Patterns are normalized into typed pattern matrices and decomposed into a decision DAG that preserves source priority, chooses tests with Maranget's column heuristic, and shares nodes. Enum, record, and boolean domains are closed (signatures from the type table); scalar and type-variable domains are open. Single-constructor signatures decompose without a runtime test.

## Diagnostics Match Execution

Reachability and missing-pattern witnesses are derived from the DAG that will execute, so exhaustiveness and redundancy diagnostics cannot disagree with runtime behavior. Witnesses carry declaration identities and are spelled at render time by scope's constructor speller ([scope.md](scope.md)). Refutable `let` reports the same witness form. Any issue yields diagnostics and no artifact.

The result wraps the checked program plus a total site-to-DAG mapping over every reachable module; artifact validation is a self-check under the self-validation toggle ([testing.md](../../testing.md)).

## References

- Luc Maranget, *Compiling Pattern Matching to Good Decision Trees* (ML Workshop 2008).
- Luc Maranget, *Warnings for Pattern Matching* (JFP 17(3), 2007) — witnesses here are reconstructed from the compiled DAG.

## Code Entry Points

- `src/agnette/agl/matchcompile/model.py`, `normalize.py` — constructor identities, checked patterns, closed signatures.
- `src/agnette/agl/matchcompile/matrix.py`, `compiler.py`, `diagnostics.py` — decomposition, DAGs, issues and witnesses.
- `src/agnette/agl/matchcompile/stage.py` — whole-program artifacts.
- Consumer: [execution/lowering.md](../execution/lowering.md).
