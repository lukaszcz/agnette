"""Structured identities for host-addressable static bindings, and sets of them."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeAlias

from agnette.agl.modules.ids import ModuleId

StaticBindingKey: TypeAlias = tuple[ModuleId, tuple[str, ...], str]
"""A static binding identity: its owning module, scope path, and name."""


def static_binding_key(
    module_id: ModuleId, scope_path: Iterable[str], name: str
) -> StaticBindingKey:
    return module_id, tuple(scope_path), name


@dataclass(frozen=True, slots=True)
class Reachable:
    """The functions and ``@param`` bindings transitively referenced from a root.

    ``functions`` are declaration keys (module, scope path, name) and include the
    root itself.
    """

    functions: frozenset[tuple[ModuleId, tuple[str, ...], str]]
    params: frozenset[StaticBindingKey]
