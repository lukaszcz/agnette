"""Cached pipeline artifacts retain prepared-source provenance.

Source provenance of frontend artifacts and of lowered executables is an
internal invariant, re-verified only under AgL's self-validation (enabled
suite-wide).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agnette.agl.lower import lower_program
from agnette.agl.pipeline import (
    ArgumentPreflight,
    ArtifactProvenanceError,
    PipelineDriver,
    PreparedProgram,
)
from agnette.agl.runtime.arguments import ProgramArguments
from tests._agl_helpers import agl_roots, prepare_inline_code


def _prepare_graph(source: str) -> PreparedProgram:
    return prepare_inline_code(
        source,
        entry_path=None,
        roots=agl_roots(),
    )


def _preflight(runtime: PipelineDriver, prepared: PreparedProgram) -> ArgumentPreflight:
    """Preflight the wrapped entry program's (empty) argument list.

    Every source in this file is a bare inline statement, wrapped into a
    zero-parameter synthetic ``program def main``. Only the resulting
    lowered executable matters here — this file tests artifact provenance,
    not argument binding.
    """
    discovery = runtime.discover_programs(prepared)
    (program,) = discovery.programs
    return runtime.preflight_arguments(
        prepared, program, ProgramArguments(positional=(), named={}), compiled=discovery.compiled
    )


def test_run_rejects_executable_from_different_prepared_program(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    prepared_a = prepare_inline_code('print "stale"')
    preflight_a = _preflight(runtime, prepared_a)
    assert preflight_a.result.ok
    assert preflight_a.executable is not None
    prepared_b = prepare_inline_code('print "fresh"')

    with pytest.raises(ArtifactProvenanceError):
        runtime.run_prepared(
            prepared_b,
            executable=preflight_a.executable,
            select_default_program=True,
        )

    assert capsys.readouterr().out == ""


def test_run_rejects_executable_issued_by_another_pipeline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    issuer = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    prepared = prepare_inline_code('print "stale"')
    preflight = _preflight(issuer, prepared)
    assert preflight.result.ok
    assert preflight.executable is not None

    with pytest.raises(ArtifactProvenanceError):
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None).run_prepared(
            prepared,
            executable=preflight.executable,
            select_default_program=True,
        )

    assert capsys.readouterr().out == ""


def test_run_resumes_the_executable_from_its_preflight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None)
    prepared = prepare_inline_code('print "fresh"')

    with patch("agnette.agl.lower.lower_program", wraps=lower_program) as lower:
        preflight = _preflight(runtime, prepared)
        assert preflight.result.ok
        assert preflight.executable is not None

        result = runtime.run_prepared(
            prepared,
            executable=preflight.executable,
            select_default_program=True,
        )

    assert result.ok
    assert lower.call_count == 1
    assert capsys.readouterr().out == "fresh\n"
