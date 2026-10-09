"""Cached match artifacts must belong to the exact prepared source consuming them.

Artifact provenance is an AgL self-check: the host layer that hands a cached
artifact back into the pipeline is the compiler's own caller, so a mismatch is a
wiring bug, not a user error.  The check runs only when self-validation is
enabled (as it is for the whole suite) and raises rather than producing a
diagnostic; with self-validation off — the production path — a cached artifact is
trusted and never re-verified.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agnette.agl.matchcompile import MatchCompiledProgram
from agnette.agl.modules.ids import STD_PRELUDE_ID
from agnette.agl.pipeline import (
    ArtifactProvenanceError,
    PipelineDriver,
    PreparedProgram,
    ProgramDiscovery,
)
from tests._agl_helpers import agl_roots, prepare_inline_code


def _prepare_graph(
    source: str,
    *,
    extra_roots: frozenset[Path] = frozenset(),
) -> PreparedProgram:
    return prepare_inline_code(
        source,
        entry_path=None,
        roots=agl_roots(*extra_roots),
    )


def _compiled(source: str) -> tuple[PreparedProgram, ProgramDiscovery]:
    prepared = _prepare_graph(source)
    discovery = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).discover_programs(
        prepared
    )
    assert discovery.compiled is not None
    return prepared, discovery


@pytest.mark.parametrize("check_only", [False, True])
def test_single_run_rejects_cached_artifact_from_different_prepared_program(
    check_only: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    prepared_a = prepare_inline_code('let a = 1\nprint "stale %{a}"')
    discovery_a = runtime.discover_programs(prepared_a)
    assert discovery_a.compiled is not None
    prepared_b = prepare_inline_code('let b = 2\nprint "fresh %{b}"')

    with pytest.raises(ArtifactProvenanceError):
        runtime.run_prepared(
            prepared_b,
            check_only=check_only,
            compiled=discovery_a.compiled,
        )

    assert capsys.readouterr().out == ""


def test_program_run_rejects_cached_artifact_with_different_entry_identity() -> None:
    prepared, discovery = _compiled("let value = 1\nvalue")
    assert discovery.compiled is not None
    wrong_entry_checked = replace(
        discovery.compiled.checked,
        entry_id=STD_PRELUDE_ID,
    )
    wrong_entry_compiled = MatchCompiledProgram(
        checked=wrong_entry_checked,
        sites_by_module=discovery.compiled.sites_by_module,
    )

    with pytest.raises(ArtifactProvenanceError):
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).run_prepared(
            prepared,
            check_only=True,
            compiled=wrong_entry_compiled,
        )


def test_program_run_rejects_cached_artifact_with_different_module_set(
    tmp_path: Path,
) -> None:
    (tmp_path / "helper.agl").write_text("def answer() -> int = 42\n")
    prepared_with_import = _prepare_graph(
        "import helper\nlet value = 1\nvalue",
        extra_roots=frozenset({tmp_path}),
    )
    discovery = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).discover_programs(
        prepared_with_import
    )
    assert discovery.compiled is not None
    prepared_without_import = _prepare_graph("let value = 2\nvalue")

    with pytest.raises(ArtifactProvenanceError):
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).run_prepared(
            prepared_without_import,
            check_only=True,
            compiled=discovery.compiled,
        )


@pytest.mark.parametrize("check_only", [False, True])
def test_program_run_rejects_cached_artifact_from_different_prepared_program(
    check_only: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepared_a, discovery_a = _compiled('print "stale"')
    prepared_b = _prepare_graph('print "fresh"')

    with pytest.raises(ArtifactProvenanceError):
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).run_prepared(
            prepared_b,
            check_only=check_only,
            compiled=discovery_a.compiled,
        )

    assert capsys.readouterr().out == ""


def test_production_path_reuses_cached_artifacts_without_verifying_provenance(
    self_validation_disabled: None,
) -> None:
    """With the self-checks off, the cached-artifact seam trusts its input."""
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    single_prepared = prepare_inline_code("let value = 1\nvalue")
    single_discovery = runtime.discover_programs(single_prepared)
    assert single_discovery.compiled is not None
    assert runtime.run_prepared(
        single_prepared, compiled=single_discovery.compiled, check_only=True
    ).ok

    graph_prepared = _prepare_graph("let g = 1\ng")
    program_discovery = runtime.discover_programs(graph_prepared)
    assert program_discovery.compiled is not None
    assert runtime.run_prepared(
        graph_prepared, compiled=program_discovery.compiled, check_only=True
    ).ok
