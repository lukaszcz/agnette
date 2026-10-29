"""Independently lowered module bodies and their linkable tables."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock

from agnette.agl import artifact_serialization
from agnette.agl.artifact_cache import retain_lowered_module, retained_lowered_module
from agnette.agl.ir.builtin_vars import BuiltinVarKey
from agnette.agl.ir.contracts import ContractRequest, TargetContractRequest
from agnette.agl.ir.ids import ContractId, FunctionId, NominalId, SymbolId
from agnette.agl.ir.nodes import IrExpr
from agnette.agl.ir.program import ExecutableModule, FunctionDescriptor, SymbolDescriptor
from agnette.agl.ir.static_keys import HostParamKey
from agnette.agl.lower.lowerer import _LinkState, _ModuleAllocation
from agnette.agl.syntax.resources import ResourceError, resolve_resource
from agnette.agl.typecheck.env import CheckedModule


@dataclass(frozen=True, slots=True)
class LoweredModule:
    """A module's IR, with stable handles for local and imported declarations."""

    module: ExecutableModule
    symbols: dict[SymbolId, SymbolDescriptor]
    functions: dict[FunctionId, FunctionDescriptor]
    contracts: dict[ContractId, ContractRequest]
    target_contracts: dict[ContractId, TargetContractRequest]
    declarations: dict[int, SymbolId]
    function_symbols: dict[int, SymbolId]
    function_ids: dict[int, FunctionId]
    defaults: dict[BuiltinVarKey | str, IrExpr]
    program_configs: dict[SymbolId, tuple[tuple[HostParamKey, IrExpr], ...]]
    resources: tuple[tuple[Path | None, str | None, Path], ...]
    field_defaults: dict[NominalId, tuple[IrExpr | None, ...]]

    def link_into(self, link: _LinkState) -> None:
        """Publish this module's declarations and bodies to the program linker."""
        link.symbols.update(self.symbols)
        link.functions.update(self.functions)
        link.contracts.update(self.contracts)
        link.target_contracts.update(self.target_contracts)
        link.decl_to_sym.update(self.declarations)
        link.fn_node_to_sym.update(self.function_symbols)
        link.fn_node_to_id.update(self.function_ids)
        link.field_defaults.update(self.field_defaults)


def capture(
    module: ExecutableModule,
    link: _LinkState,
    defaults: dict[BuiltinVarKey | str, IrExpr],
    program_configs: dict[SymbolId, tuple[tuple[HostParamKey, IrExpr], ...]],
    resources: tuple[tuple[Path | None, str | None, Path], ...],
    allocation_start: int,
    allocation_end: int,
    field_defaults: dict[NominalId, tuple[IrExpr | None, ...]],
) -> LoweredModule:
    """Extract only this checked source version's allocation namespace.

    Module ownership spans source versions in a live REPL image. Keep its old
    descriptors there for retained values, but never copy them into a new
    version's artifact. Allocation identity also includes synthetic descriptors
    whose semantic owner may be a declaration from another module.
    """
    functions = {
        fid: fn
        for fid, fn in link.functions.items()
        if allocation_start <= fid.value < allocation_end
    }
    symbols = {
        sid: sym
        for sid, sym in link.symbols.items()
        if allocation_start <= sid.value < allocation_end
    }
    return LoweredModule(
        module,
        symbols,
        functions,
        {
            cid: value
            for cid, value in link.contracts.items()
            if allocation_start <= cid.value < allocation_end
        },
        {
            cid: value
            for cid, value in link.target_contracts.items()
            if allocation_start <= cid.value < allocation_end
        },
        {nid: sid for nid, sid in link.decl_to_sym.items() if sid in symbols},
        {nid: sid for nid, sid in link.fn_node_to_sym.items() if sid in symbols},
        {nid: fid for nid, fid in link.fn_node_to_id.items() if fid in functions},
        defaults,
        program_configs,
        resources,
        field_defaults,
    )


def load(key: bytes) -> LoweredModule | None:
    """Read reusable IR, revalidating filesystem-dependent resource paths."""
    module = retained_lowered_module(key)
    if module is None:
        restored = artifact_serialization.load(key, "ir")
        if not isinstance(restored, LoweredModule):
            return None
        module = restored
        retain_lowered_module(key, module)
    return module if _resources_intact(module) else None


def _resources_intact(module: LoweredModule) -> bool:
    try:
        return all(
            resolve_resource(root, relative) == expected
            for root, relative, expected in module.resources
        )
    except ResourceError:
        return False


def save(key: bytes, module: LoweredModule) -> None:
    """Publish a successfully lowered module for later CLI processes."""
    retain_lowered_module(key, module)
    artifact_serialization.save(key, "ir", module)


@dataclass(frozen=True, slots=True)
class _SessionModule:
    # Keep the actual owner alive: an integer object id alone is not an identity.
    owner: CheckedModule
    module: LoweredModule
    allocation: _ModuleAllocation


_SESSION_CAPACITY = 512
_SESSION_MODULES: OrderedDict[tuple[int, bytes], _SessionModule] = OrderedDict()
_SESSION_LOCK = Lock()


def load_session(owner: CheckedModule, context: bytes, link: _LinkState) -> LoweredModule | None:
    """Reuse only this exact checked artifact, never persisted session code."""
    key = (id(owner), context)
    with _SESSION_LOCK:
        cached = _SESSION_MODULES.get(key)
        if cached is None or not _resources_intact(cached.module):
            return None
        _SESSION_MODULES.move_to_end(key)
        link.module_allocations[owner.resolved.program.node_id] = replace(cached.allocation)
        return cached.module


def save_session(
    owner: CheckedModule, context: bytes, module: LoweredModule, link: _LinkState
) -> None:
    """Retain immutable IR and a copy of its allocation frontier, not runtime values."""
    key = (id(owner), context)
    with _SESSION_LOCK:
        _SESSION_MODULES[key] = _SessionModule(
            owner, module, replace(link.module_allocations[owner.resolved.program.node_id])
        )
        _SESSION_MODULES.move_to_end(key)
        while len(_SESSION_MODULES) > _SESSION_CAPACITY:
            _SESSION_MODULES.popitem(last=False)
