"""Public façade for AgL (Agnette Language), Agnette's workflow DSL.

The façade exports the pipeline API lazily so lower AgM domains can depend on
AgL leaves such as module identifiers without importing the execution stack.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agnette.agl.diagnostics import (
        AglError,
        Diagnostic,
        RelatedDiagnostic,
        SourceSpan,
        format_diagnostic,
    )
    from agnette.agl.pipeline import (
        ArgumentPreflightFailure,
        PipelineDriver,
        PreparedProgram,
        RunError,
        RunResult,
    )
    from agnette.agl.runtime.agents import AgentFn

__all__ = [
    "AgentFn",
    "AglError",
    "ArgumentPreflightFailure",
    "Diagnostic",
    "format_diagnostic",
    "PipelineDriver",
    "RelatedDiagnostic",
    "PreparedProgram",
    "RunError",
    "RunResult",
    "SourceSpan",
]

# name -> (module path, attribute name). Keeps every export a one-line entry
# and the lazy-loading mechanics (below) independent of the export list, so
# importing ``agnette.agl`` itself never pulls in the execution stack.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentFn": ("agnette.agl.runtime.agents", "AgentFn"),
    "AglError": ("agnette.agl.diagnostics", "AglError"),
    "ArgumentPreflightFailure": ("agnette.agl.pipeline", "ArgumentPreflightFailure"),
    "Diagnostic": ("agnette.agl.diagnostics", "Diagnostic"),
    "format_diagnostic": ("agnette.agl.diagnostics", "format_diagnostic"),
    "PipelineDriver": ("agnette.agl.pipeline", "PipelineDriver"),
    "RelatedDiagnostic": ("agnette.agl.diagnostics", "RelatedDiagnostic"),
    "PreparedProgram": ("agnette.agl.pipeline", "PreparedProgram"),
    "RunError": ("agnette.agl.pipeline", "RunError"),
    "RunResult": ("agnette.agl.pipeline", "RunResult"),
    "SourceSpan": ("agnette.agl.diagnostics", "SourceSpan"),
}


def __getattr__(name: str) -> object:
    """Load one public export only when a caller requests it."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_path, attribute = target
    module = import_module(module_path)
    value: object = getattr(module, attribute)
    return value
