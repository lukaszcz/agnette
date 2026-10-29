# Testing

The suite mirrors the architecture: AgL is tested pass by pass plus whole-program acceptance corpora, commands through their CLI surface, and primitives and domain packages by focused unit tests. Everything lives under `tests/`; `tests/AGENTS.md` holds the test-writing conventions.

## Strategy

- **AgL passes** have their own suites plus end-to-end corpora under `tests/agl/` (programs with multi-scenario sidecars, static rejections, multi-file programs, packages). Scope and typecheck suites build a real `ModuleGraph` (`tests/agl/module_graph.py`); evaluation helpers (`tests/agl/ir_harness.py`) run programs through `PipelineDriver` with fake agents and shells. A scenario's `expect.warnings` is the exact list of `{name, line}` static warnings, each optionally pinned to its `column` and `source` module (absent means none). `warning_levels` (name → level) on a scenario or a rejection simulates `--warning`; a rejection's `diagnostic.warning` pins the denied warning.
- **Corpus harness** (`tests/agl/scenario_harness.py`) runs a program under a scenario and asserts its expectations; `test_agl_e2e.py` and the isolated runner share it.
- **Isolated runner** (`tests/agl/isolated_runner.py`) runs a corpus scenario (`"isolated": true`) or a Python callable in a fresh single-threaded process, since xdist workers carry a second thread state and could not `os.fork`. It applies the suite's in-process guards (`tests/_isolation.py`), captures fd 1 so forked children's output is included, and kills every process left in the process's session when the run ends, including children in groups of their own. A run is a child (own session) of a per-worker *isolated zygote* that imports the harness and compiles the standard library in memory, once; a cold `python -m` subprocess serves a run the zygote cannot: `AGM_TEST_NO_ZYGOTE`, coverage data of its own, a different interpreter-start environment (`PYTHON*`, `LANG`, `LC_*`, `TZ`), an unreachable zygote, or a zygote that failed to start (which also ends zygote use for the session, with a warning quoting its stderr). The zygote never runs a test, so a child inherits its imports, immutable compile caches, hash seed and gc state; a cold interpreter's import order and a scenario's behavior under another hash seed are therefore proven only by the `AGM_TEST_NO_ZYGOTE` suite. A child's CPU is charged back to the test; an abandoned child (timeout, interrupt) is killed with its session and the zygote returns to idle; a crashed zygote fails the run in flight (never rerun cold) and is restarted by the next. The zygote measures nothing: under active coverage each child starts measurement through `tests/isolated_coverage.toml` right after the fork, as a cold subprocess does at startup, so the gate is the same in both modes; pytest-cov combines their data, so lines run only in children count toward the gate.
- **Forked-process scripts.** A corpus scenario's `forks` map (fork path such as `"1.1"` to a section) gives each forked process agent, shell and HTTP scripts and expectations of its own, checked by the child as it exits. The harness implements it as an extra fork participant (`ScriptedForks`, installed by patching `runtime.host_session.build_process_host`) that swaps the scripted boundaries in the child; the `std/ps` programs under `tests/agl/programs/ps/` use it. Process-level expectations (`stderr`, `trace`, `ends`) refer to processes by fork path, resolved to pids the children record.
- **Name resolution** probes (`tests/agl/qualifier_support.py`) run inline, as files, and across REPL entry splits.
- **Commands** are tested at the CLI boundary; multi-command user workflows live in `tests/test_e2e.py`.
- **Domain and primitives** have unit tests using real archives, manifests, git, filesystem, and loopback HTTP; external boundaries (agents, unavailable tools, remote HTTP) are faked. Process-host interruption probes send real signals at deterministic execution boundaries and assert propagation and child reaping.

Tests assert behavior, never exact help, warning, or error text. The e2e harness runs the checkout's CLI with no installation, each invocation in its own process forked from a preloaded per-worker launcher (`tests/_agm_zygote.py`; `AGM_TEST_NO_ZYGOTE` disables it). The launcher framework (socket protocol, fd passing, server loop, client, CPU charging, lifetime) is shared with the isolated runner (`tests/_zygote.py`).

## Gates and Invariants

- **Static typing** — strict mypy, configured in `pyproject.toml`.
- **Package layering** — `tests/test_agl_dependencies.py` asserts the AgL import contract ([agl/index.md](agl/index.md)) and keeps `core`/`util` below both subsystems.
- **Coverage** — 100% line and branch coverage of `src/` and `packages/stdlib/src/` via `sys.monitoring` (hence Python 3.14); `packages/sysone` is outside the gates.
- **Command coverage** — `tests/_command_coverage.py` observes real command dispatch through a private child descriptor; help and parser rejections do not count. No application instrumentation is required.
- **Documented examples compile** — every `agl` fence under `docs/agl/reference/` runs through the static pipeline with adjacent hidden context (`tests/test_agl_doc_snippets.py`); shared metadata parsing is covered by `tests/test_agl_highlight_context.py`.
- **Grammar conformance** — `tests/test_agl_highlight_conformance.py` compares projected exact lexical spans with the portable grammar across programs, stdlib sources and doc fences. Symmetric contextual projection is paired with independent local-contract tests; named, witnessed core divergences are source-bounded and guarded by mutations. This verifies the [approximation contract](highlighting.md#portable-grammar-and-targets), not semantic equality.
- **AgL layout style** — `tools/agl_style.py` formats and checks every `.agl` source, doc fence, and embedded snippet.
- **Self-validation** — compiler invariant self-checks and completion's error re-raising are gated by `agnette.agl.self_validation`, enabled for in-process tests by `tests/conftest.py`; e2e subprocesses and production run without it.
- **Hermeticity** — autouse fixtures give each test an empty `HOME`, strip inherited AgM context and shell startup overrides, pin `AGM_STDLIB` to the repository stdlib, and bound git discovery (`tests/test_environment_isolation.py`).
- **No real agents or network** — spawning an external agent or sandbox CLI (`tests/_external_agent_clis.py`) and unscripted HTTP fail the test (guards in `tests/_isolation.py`, applied by `tests/conftest.py` fixtures); HTTP is scripted through `tests/_http_helpers.py` and `tests/_jev_helpers.py`. A session-wide guard (`tests/conftest.py`) puts blocking `uv`/`pip` stubs first on `PATH`, sets offline installer variables, and fails any test that reaches one; tests fake installers at the subprocess boundary.
- **Dead code** — `tools/vulture_check.py` (`just vulture`); test-only use does not count.
- **Vacuous-assertion guard** — `just test-neutral-tmp` reruns with neutrally named temp directories.
- **Optional mutation gate** — `uv run python tools/mutation_check.py` replays pinned semantic mutants in `tools/mutations.json`, outside `just check`. It intercepts ordinary and companion imports in memory, verifies unchanged baselines and per-test reachability, and distinguishes survivors from invalid or unreached mutants. Reports are written to `mutation-report/` by default.

## Test Cost

Per-test cost is measured in CPU seconds (`tests/_durations.py`); `just test` enforces a per-test ceiling and `just test-budget` ranks tests to recalibrate it. Pure lexer and grammar matrices batch small input axes under the same isolation and CPU gates; each case constructs fresh results. Fixtures and launchers reuse immutable compilation where roots agree, keeping runtime state fresh. The xdist controller shares a private, per-invocation snapshot of static coverage analysis with workers and isolated zygotes (`tests/_coverage_metadata.py`); changed bytecode and temporary companions use ordinary analysis. Collectors and measured arcs remain child-local. Allocation-pressure-triggered young and periodic full GC bounds cyclic compiler retention at test boundaries; session metadata and preloaded stdlib artifacts are frozen. E2e commands have a generous hang deadline and descendant cleanup, separate from the CPU budget. `just check` runs the static gates concurrently with the suite.

## Native Highlighting Engines

Outside `just check`, `just test-kde` uses Pandoc 3.x/skylighting,
`just test-textmate` uses pinned TextMate/Oniguruma npm dependencies, and
`just test-sublime` uses bat/syntect. Missing tools skip clearly; with bat present,
`just check` also runs the native Sublime pytest probes. `just setup` installs those dependencies when npm is available; with node
present, `just check` also runs the native TextMate pytest probes and fails if they are missing. All compare CLI-generated definitions with `grammar_eval` over the
shared corpus/partition contract in `tools/highlight_engine_support.py`, including
native recovery frames. KDE compares projected HTML styles and normalizes newlines;
TextMate compares published scopes and rejects unknown scopes; Sublime decodes a
probe theme's per-category colors and normalizes newlines. Harnesses live in
`tools/highlight_{kde,textmate,sublime}*.py` and
`config/{kde,textmate}/tests/`; generated definitions stay under ignored `build/`.

## Editor Modes

The Emacs (`config/emacs/`) and micro (`config/micro/`) modes have their own suites, `just test-emacs` and `just test-micro`, outside `just check`. Emacs uses a generated inventory in `build/emacs/`; micro still mirrors the canonical keyword/builtin inventories by hand.

## Code Entry Points

- `tests/agl/scenario_harness.py` — corpus scenario harness; `tests/agl/isolated_runner.py`, `tests/isolated_coverage.toml`, `tests/test_agl_isolated_runner.py` — isolated runner (client, zygote, cold subprocess), its coverage config, and its tests.
- `tests/test_agl_*.py` — AgL pass suites; `tests/test_agl_e2e.py`, `test_agl_multifile.py` — acceptance suites over `tests/agl/`.
- `tests/test_e2e.py` — command e2e suite (`run_agm`).
- `tests/conftest.py`, `tests/_isolation.py`, and `tests/_*_helpers.py` — shared fixtures, in-process guards, and fakes.
- `tests/_command_coverage.py`, `_durations.py`, `_zygote.py`, `_agm_zygote.py` — command coverage, CPU cost, shared preforking launcher, e2e launcher.
- `tests/test_agl_highlight_*.py` — exact tiers, Markdown context rendering/isolation, portable conformance and target contracts.
- `justfile` — standard gates; optional `test-kde`, `test-textmate`, `test-sublime`, `test-emacs`, `test-micro`.
