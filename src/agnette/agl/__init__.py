"""Public façade for AgL (Agnette Language), Agnette's workflow DSL.

The façade exports the pipeline API lazily so lower AgM domains can depend on
AgL leaves such as module identifiers without importing the execution stack.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agnette.agl.pipeline import ArgumentPreflightFailure, PipelineDriver

__all__ = ["ArgumentPreflightFailure", "PipelineDriver"]

# name -> (module path, attribute name). Keeps every export a one-line entry
# and the lazy-loading mechanics (below) independent of the export list, so
# importing ``agnette.agl`` itself never pulls in the execution stack.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ArgumentPreflightFailure": ("agnette.agl.pipeline", "ArgumentPreflightFailure"),
    "PipelineDriver": ("agnette.agl.pipeline", "PipelineDriver"),
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
