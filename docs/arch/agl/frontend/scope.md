# AgL Name Resolution

The scope pass resolves every name over the whole module graph and publishes immutable side tables, including each module's reachable declaration identities. Typecheck, match compilation, lowering, and the REPL only read these tables; nothing downstream re-resolves. Scope never guesses: a module's own declaration wins, and two distinct contributed declarations where a spelling is decided are an ambiguity reported at that use.

## Full-Path Lookup

A scope path is part of a declaration's name; a `scope` region is not an entity, and regions of one path combine across modules. Lexical binders (parameters, block `let`/`var`, loop and pattern binders) are nearest; past them, one lookup (`scope/lookup.py`) decides every spelling in every position; the position picks the kind (type, value, constructor).

- **Steps** — a spelling `p` in `scope S1::S2` is tried at `S1::S2::p`, `S1::p`, then `p`. Each step reads own declarations at that path plus contributions anchored at or above it (imports, uses, prelude, injected enum members); the first step selecting anything decides.
- **Identity** — distinctness is by declaration identity, so an alias and its target, or two routes to one declaration, count once. Constructor references resolve to a canonical `ConstructorRef`; spellings are never identity.
- **Hiding** — `hiding` removes by declaration identity (`scope/hiding.py`); each candidate carries what every way reaching it removes, and the walk alone issues the hidden verdict.
- **Sources** — `ModuleSources` (`scope/sources.py`) is what one module's lookups read: own declarations, import contributions, injected enum members, reads beneath aliases. `UseReader` (`scope/uses.py`) re-reads `use` targets live, so nothing is snapshotted.

Every verdict is recorded once: bindings in `resolution`, qualified and named-type selections in `owner_declarations` by node id.

## Declarations and Aliases

Resolution runs in three phases over all modules: prepare (placement, declaration collection, headers), collect (constructor candidates and root bindings, once every module is prepared), and resolve (bodies in graph order). Header diagnostics of every module therefore precede body diagnostics of any.

- Module-level `let`/`var` of a static-root module (any but a REPL entry or a wrapped `-c` entry) are pre-populated like `def`s; those two keep textual visibility. Either way the name claims its scope path in the shared declaration registry, so collisions are order-independent.
- An alias declares no scope: declaring beneath an own alias name, or a method receiver through an alias, is an error. A path beneath an alias reads literally through its target, at the alias's site, under one re-entrancy guard.
- `TypeOwnerIndex` (`scope/type_owners.py`) holds one owner per type path: an enum owns its inline members, an alias the member table its target selects (minus its `hiding`). Inline enum members are nominal record declarations beneath the enum's scope.
- Enum members are injected as bare names by one rule wherever a spelling of their enum reaches a step; in value positions a record or exception of the same name at that step wins.
- Scope owns constructor spelling: `ModuleSources.spell_constructor` finds the shortest verified spelling at a site, used for ambiguity repairs and match witnesses (`ResolvedProgram.speller`, [matchcompile.md](matchcompile.md)).
- Patterns and `is` tests keep every visible candidate; typecheck selects from the matched nominal type.

## Imports and `use`

An import contributes a module's exports by declaration identity, unfiltered; its `hiding` and the exporter's travel with the ways reaching each declaration. Import tails and `use` add bare names without narrowing qualified access; `use` reads only already-nameable paths and never loads a module. Exports and re-exports settle per import SCC to a fixpoint before any importer reads them (`scope/program.py`). Loading-level rules are in [modules.md](../modules.md).

## Classification

Scope classifies what typecheck will type:

- A called name is a built-in only when it resolves to a `builtin def`; a method-call spelling records a provisional kind that typecheck confirms after member selection. Most builtin call names are reserved in bare position. Binding references retain builtin and method provenance across aliases.
- A `self`-receiver `def` attaches to the type its full path selects; scope classifies builtin receivers.
- `builtin var` bindings are host-backed; only root `std/config` bindings are engine settings.
- Simple static `let`/`var` bindings are exported like declarations; `scope/bindings.py` answers which own static bindings an expression reads (constant classification and initializer ordering).
- Declaration `{…}` constraint blocks must name the declaration's own (or receiver's) type parameters.

## Operators

A user operator is a name. The body walk resolves each operator in a raw infix chain ([syntax.md](syntax.md)) and groups the chain by the fixity the selected declaration's module declares; `resolve` rewrites every chain once (`agl/infix.py`), so `ModuleResolution.program` is the grouped program. `ModuleResolution.fixities` publishes a module's declared fixities for importers reusing it and for later REPL entries.

## Attribute Recognition

The parser keeps attributes verbatim; scope gives them meaning. One walk checks each attribute against the catalog in `agl/attributes.py` (name, target, argument schema, repetition, conflicts) and hands survivors to per-attribute fact builders that write typed side tables (`AttributeFacts`). Adding an attribute adds a fact builder, never a traversal. Text arguments are folded as constants first ([syntax.md](syntax.md#constant-folding)).

| Fact | From | Consumer |
| --- | --- | --- |
| `param_zones` | `@arg-*` | typecheck parameter and field lists (the AST carries no zone) |
| `extern_names` | `@extern-name` | lowering and companion resolution ([execution/ffi.md](../execution/ffi.md)) |
| `external_names` | `@name`, `@json-name` | type building; collisions checked by typecheck |
| `params`, `program_options`, `docs` | `@param`, `@opt-*`, `@doc` | program discovery and CLI projection ([hosting.md](../hosting.md)); `docs` also type building |
| `program_configs` | `@config` | typecheck (keys resolved, values checked as constants) and lowering ([hosting.md](../hosting.md)) |

`@command` writes no fact: scope only validates its path, and the package command scan reuses the recognizer without a scope pass ([packages.md](../../packages.md)).

## Code Entry Points

- `src/agnette/agl/scope/lookup.py`, `hiding.py` — full-path lookup, verdicts, identity-based hiding.
- `src/agnette/agl/scope/sources.py`, `uses.py` — per-module lookup sources, alias reads, constructor speller; `use` reads.
- `src/agnette/agl/scope/resolver.py` — declaration collection, the ordered walk, classification.
- `src/agnette/agl/scope/imports.py`, `program.py` — import contributions; export settling, cross-module resolution, `ResolvedProgram`.
- `src/agnette/agl/scope/type_owners.py`, `type_names.py` — type owners, alias targets, member selection.
- `src/agnette/agl/scope/attributes.py`, `bindings.py` — attribute recognition; static-binding reads.
- `src/agnette/agl/scope/symbols.py` — symbol kinds, `ConstructorRef`, selections, `ModuleResolution` and `AttributeFacts`, scope errors.
- `src/agnette/agl/infix.py` — operator fixity and chain grouping.
