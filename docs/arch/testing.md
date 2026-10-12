# Testing

The suite mirrors the architecture: AgL is tested pass by pass plus whole-program acceptance corpora, commands through their CLI surface, and primitives and domain packages by focused unit tests. Everything lives under `tests/`; `tests/AGENTS.md` holds the test-writing conventions.

## Strategy

- **AgL passes** have their own suites plus end-to-end corpora under `tests/agl/` (programs with multi-scenario sidecars, static rejections, multi-file programs, packages). Scope and typecheck suites build a real `ModuleGraph` (`tests/agl/module_graph.py`); evaluation helpers (`tests/agl/ir_harness.py`) run programs through `PipelineDriver` with fake agents and shells.
- **Name resolution** probes (`tests/agl/qualifier_support.py`) run inline, as files, and across REPL entry splits.
- **Commands** are tested at the CLI boundary; multi-command user workflows live in `tests/test_e2e.py`.
- **Domain and primitives** have unit tests using real archives, manifests, git, filesystem, and loopback HTTP; external boundaries (agents, unavailable tools, remote HTTP) are faked.

Tests assert behavior, never exact help, warning, or error text. The e2e harness runs the checkout's CLI with no installation, each invocation in its own process forked from a preloaded per-worker launcher (`tests/_agm_zygote.py`; `AGM_TEST_NO_ZYGOTE` disables it).

## Gates and Invariants

- **Static typing** — strict mypy, configured in `pyproject.toml`.
- **Package layering** — `tests/test_agl_dependencies.py` asserts the AgL import contract ([agl/index.md](agl/index.md)) and keeps `core`/`util` below both subsystems.
- **Coverage** — 100% line and branch coverage of `src/` and `packages/stdlib/src/` via `sys.monitoring` (hence Python 3.14); `packages/sysone` is outside the gates.
- **Command coverage** — `tests/_command_coverage.py` records which leaf commands the e2e suite ran through the real binary.
- **Documented examples compile** — every `agl` fence under `docs/agl/reference/` runs through the static pipeline with adjacent hidden context (`tests/test_agl_doc_snippets.py`); shared metadata parsing is covered by `tests/test_agl_highlight_context.py`.
- **Grammar conformance** — `tests/test_agl_highlight_conformance.py` compares projected exact lexical spans with the portable grammar across programs, stdlib sources and doc fences. Symmetric contextual projection is paired with independent local-contract tests; named, witnessed core divergences are source-bounded and guarded by mutations. This verifies the [approximation contract](highlighting.md#portable-grammar-and-targets), not semantic equality.
- **AgL layout style** — `tools/agl_style.py` formats and checks every `.agl` source, doc fence, and embedded snippet.
- **Self-validation** — compiler invariant self-checks and completion's error re-raising are gated by `agnette.agl.self_validation`, enabled for in-process tests by `tests/conftest.py`; e2e subprocesses and production run without it.
- **Hermeticity** — autouse fixtures give each test an empty `HOME`, strip inherited `AGM_*` and related variables, pin `AGM_STDLIB` to the repository stdlib, and bound git discovery (`tests/test_environment_isolation.py`).
- **No real agents or network** — spawning an external agent or sandbox CLI (`tests/_external_agent_clis.py`) and unscripted HTTP fail the test; HTTP is scripted through `tests/_http_helpers.py` and `tests/_jev_helpers.py`.
- **Dead code** — `tools/vulture_check.py` (`just vulture`); test-only use does not count.
- **Vacuous-assertion guard** — `just test-neutral-tmp` reruns with neutrally named temp directories.
- **Optional mutation gate** — `tools/mutation_check.py` replays the mutation sample in `tools/mutations.json`, outside `just check`.

## Test Cost

Per-test cost is measured in CPU seconds (`tests/_durations.py`); `just test` enforces a per-test ceiling and `just test-budget` ranks tests to recalibrate it. Pure lexer and grammar matrices batch small input axes under the same isolation and CPU gates; each case constructs fresh results. Compiling modules dominates AgL cost, so fixtures reuse immutable compilation where roots agree and keep runtime state fresh. Periodic young and full GC bounds cyclic compiler retention without per-allocation scans; only session metadata is frozen. `just check` runs the static gates concurrently with the suite.

## Native Highlighting Engines

Outside `just check`, `just test-kde` uses Pandoc 3.x/skylighting and
`just test-textmate` uses pinned TextMate/Oniguruma npm dependencies. Missing tools
skip clearly. Both compare CLI-generated definitions with `grammar_eval` over the
shared corpus/partition contract in `tools/highlight_engine_support.py`, including
native recovery frames. KDE compares projected HTML styles and normalizes newlines;
TextMate compares published scopes and rejects unknown scopes. Harnesses live in
`tools/highlight_kde*.py`, `tools/highlight_textmate.py` and
`config/{kde,textmate}/tests/`; generated definitions stay under ignored `build/`.

## Editor Modes

The Emacs (`config/emacs/`) and micro (`config/micro/`) modes have their own suites, `just test-emacs` and `just test-micro`, outside `just check`. Emacs uses a generated inventory in `build/emacs/`; micro still mirrors the canonical keyword/builtin inventories by hand.

## Code Entry Points

- `tests/test_agl_*.py` — AgL pass suites; `tests/test_agl_e2e.py`, `test_agl_multifile.py` — acceptance suites over `tests/agl/`.
- `tests/test_e2e.py` — command e2e suite (`run_agm`).
- `tests/conftest.py` and `tests/_*_helpers.py` — shared fixtures and fakes.
- `tests/_command_coverage.py`, `_durations.py`, `_agm_zygote.py` — command coverage, CPU cost, e2e launcher.
- `tests/test_agl_highlight_*.py` — exact tiers, Markdown context rendering/isolation, portable conformance and target contracts.
- `justfile` — standard gates; optional `test-kde`, `test-textmate`, `test-emacs`, `test-micro`.
