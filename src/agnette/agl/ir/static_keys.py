"""Structured identities for host-addressable static bindings, and sets of them."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypeAlias

from agnette.agl.modules.ids import ModuleId

StaticBindingKey: TypeAlias = tuple[ModuleId, tuple[str, ...], str]
"""A static binding identity: its owning module, scope path, and name."""


@dataclass(frozen=True, slots=True)
class FunctionParamKey:
    """A function parameter's identity: its function's declaration key and its name.

    A ``program def``'s own parameters are keyed alike.
    """

    module: ModuleId
    scope_path: tuple[str, ...]
    function: str
    param: str


HostParamKey: TypeAlias = StaticBindingKey | FunctionParamKey
"""Any host parameter's identity, told apart by type.

Engine settings are root bindings of ``std/config``, so they are binding keys.
"""


def static_binding_key(
    module_id: ModuleId, scope_path: Iterable[str], name: str
) -> StaticBindingKey:
    return module_id, tuple(scope_path), name


def host_param_module(key: HostParamKey) -> ModuleId:
    """Return the module declaring the host parameter *key*."""
    return key.module if isinstance(key, FunctionParamKey) else key[0]


def split_host_param_values[V](
    values: Mapping[HostParamKey, V],
) -> tuple[dict[StaticBindingKey, V], dict[FunctionParamKey, V]]:
    """Split host parameter *values* into binding-keyed and function-parameter-keyed ones."""
    bindings: dict[StaticBindingKey, V] = {}
    functions: dict[FunctionParamKey, V] = {}
    for key, value in values.items():
        if isinstance(key, FunctionParamKey):
            functions[key] = value
        else:
            bindings[key] = value
    return bindings, functions


def host_param_label(key: HostParamKey) -> str:
    """Return the stable qualified spelling of a host parameter."""
    if isinstance(key, FunctionParamKey):
        return "::".join((key.module.display(), *key.scope_path, key.function, key.param))
    module_id, scope_path, name = key
    return "::".join((module_id.display(), *scope_path, name))


@dataclass(frozen=True, slots=True)
class Reachable:
    """The functions and ``@param`` bindings transitively referenced from a root.

    ``functions`` are declaration keys (module, scope path, name) and include the
    root itself.
    """

    functions: frozenset[tuple[ModuleId, tuple[str, ...], str]]
    params: frozenset[StaticBindingKey]
