"""Host-side agent session protocols and lifecycle management.

The façade exports lazily so the AgL runtime can use the protocol leaf
(``agnette.agent.session.protocol``) without loading the session service.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agnette.agent.session.service import create_agl_session_host

__all__ = ["create_agl_session_host"]

# name -> (module path, attribute name).
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "create_agl_session_host": ("agnette.agent.session.service", "create_agl_session_host"),
}


def __getattr__(name: str) -> object:
    """Load one public export only when a caller requests it."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_path, attribute = target
    value: object = getattr(import_module(module_path), attribute)
    return value
