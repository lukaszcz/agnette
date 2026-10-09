"""AgL type-checking pass.

Public API
----------
- :class:`UnappliedGenericTypeError` — an ``AglTypeError`` for a bare generic
  type reference missing its type arguments; carries the resolved
  ``GenericTypeDef`` and display name.

The pass itself is reached through its submodules (``env``, ``program``, ...).
"""

from agnette.agl.typecheck.env import UnappliedGenericTypeError

__all__ = ["UnappliedGenericTypeError"]
