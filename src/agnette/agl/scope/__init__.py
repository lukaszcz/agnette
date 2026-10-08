"""AgL scope/name-resolution pass.

Public API
----------
- :func:`resolve_program` — whole-program static name-resolution pass over a
  :class:`~agnette.agl.modules.loader.ModuleGraph`: one ``ModuleResolution`` per
  module.
- :class:`ModuleResolution` — frozen dataclass carrying one module's
  ``Program`` plus side tables keyed by ``node_id``; its
  ``param_bindings()`` yields every ``@param``-marked static binding.
- :class:`ParamBinding` — one such binding, paired with its ``@param``
  attribute payload.
- :class:`BindingRef` — resolved reference to a scope binding.
- :class:`BuiltinKind` — enum classifying contextual built-in Call nodes.
- :class:`AglScopeError` — fatal scope error (span-aware ``AglError``
  subclass).
- :func:`recognize_program_registration` — parse-only recognition of one
  ``program def``'s documentation and registered command, for callers that
  scan source without a full resolve (package command discovery).
"""

from __future__ import annotations

from agnette.agl.scope.attributes import recognize_program_registration
from agnette.agl.scope.program import ResolvedModule, ResolvedProgram, resolve_program
from agnette.agl.scope.symbols import (
    AglScopeError,
    BindingRef,
    BuiltinKind,
    ModuleResolution,
    ParamBinding,
    ScopeNode,
)

__all__ = [
    "AglScopeError",
    "BindingRef",
    "BuiltinKind",
    "ModuleResolution",
    "ParamBinding",
    "ResolvedModule",
    "ResolvedProgram",
    "ScopeNode",
    "recognize_program_registration",
    "resolve_program",
]
