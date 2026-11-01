"""Shared runner for AgL tests of the ``devel`` package, with mocked agents."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agent.session.service import create_agl_session_host
from agnette.agl.pipeline import PipelineDriver
from agnette.agl.runtime.agents import value_driven_agent_factory
from tests._agl_git_helpers import execute_git_program
from tests._agl_helpers import hermetic_get_sandbox_context, package_roots
from tests._package_helpers import package_info

_DEVEL_ROOT = Path(__file__).resolve().parents[1] / "packages" / "devel"


def run_devel_program(
    body: str,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    imports: tuple[str, ...],
) -> None:
    """Run *body* in *repo* with the ``devel`` package mounted."""
    get_sandbox_context = hermetic_get_sandbox_context()
    runtime = PipelineDriver(
        resolve_agent_spec=None,
        agent_dispatcher=value_driven_agent_factory(
            idle_timeout=None, get_sandbox_context=get_sandbox_context
        ),
        session_host=create_agl_session_host(
            idle_timeout=None, get_sandbox_context=get_sandbox_context
        ),
        get_sandbox_context=None,
    )
    execute_git_program(
        body,
        repo,
        monkeypatch,
        imports=imports,
        runtime=runtime,
        roots=package_roots(package_info(_DEVEL_ROOT), cwd=_DEVEL_ROOT),
    )
