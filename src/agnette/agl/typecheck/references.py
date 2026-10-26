"""Static reference summaries: what each function and static binding refers to.

A summary maps a ``def`` (its body and parameter defaults) or a static
``let``/``var`` (its initializer), lambdas included, to the :class:`References`
it makes directly. It is derived from scope's ``resolution`` (pattern slots
dereferenced as the checker resolved them) and the checker's
``method_selections``. :mod:`agnette.agl.typecheck.reachability` walks them.
Host-backed (``builtin``) declarations are summarized too, since their parameter
defaults are AgL expressions, and edges to them are kept. Record field defaults
fold declared initializers, so they need no edges.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import NamedTuple

from agnette.agl.ir.static_keys import StaticBindingKey, static_binding_key
from agnette.agl.modules.ids import ModuleId
from agnette.agl.scope.bindings import ModuleBindingReferences
from agnette.agl.scope.symbols import BinderKind, BindingRef, DeclarationKey, ModuleResolution
from agnette.agl.semantics.type_table import MethodDef
from agnette.agl.syntax.nodes import (
    AssignStmt,
    FieldAccess,
    FuncDef,
    VarRef,
    scope_names,
    static_function_items,
)
from agnette.agl.syntax.visitor import SyntaxNode, walk
from agnette.agl.typecheck.env import dereference_slot_binding

_STATIC_BINDING_KINDS = frozenset({BinderKind.let_binding, BinderKind.var_binding})


@dataclass(frozen=True, slots=True)
class References:
    """The declarations one function or static binding directly references.

    ``functions``
        Functions, methods and ``builtin`` declarations included.
    ``params``
        Static ``@param`` bindings.
    ``bindings``
        Other static ``let``/``var`` bindings.
    """

    functions: frozenset[DeclarationKey] = frozenset()
    params: frozenset[StaticBindingKey] = frozenset()
    bindings: frozenset[StaticBindingKey] = frozenset()


class ReferenceSummaries(NamedTuple):
    """A module's summaries: per ``def``, and per static ``let``/``var``."""

    declarations: dict[DeclarationKey, References]
    bindings: dict[StaticBindingKey, References]


class _Collector:
    def __init__(
        self,
        resolved: ModuleResolution,
        method_selections: Mapping[int, MethodDef],
        slot_resolution: Mapping[int, BindingRef],
        module_id: ModuleId,
    ) -> None:
        self._resolved = resolved
        self._method_selections = method_selections
        self._slot_resolution = slot_resolution
        self._module_id = module_id
        self.static_bindings = ModuleBindingReferences(resolved, module_id).declarations

    def collect(self, *roots: SyntaxNode) -> References:
        """Collect the references made anywhere inside *roots*."""
        functions: set[DeclarationKey] = set()
        params: set[StaticBindingKey] = set()
        bindings: set[StaticBindingKey] = set()

        def visit(node: object) -> None:
            if isinstance(node, (VarRef, AssignStmt)):
                ref = dereference_slot_binding(
                    node.node_id,
                    resolution=self._resolved.resolution,
                    slot_resolution=self._slot_resolution,
                )
                if ref is None:
                    return
                key = (ref.module_id, ref.scope_path, ref.name)
                if ref.kind is BinderKind.function_binding:
                    functions.add(key)
                elif ref.kind in _STATIC_BINDING_KINDS and self._is_static(ref):
                    (params if ref.is_param else bindings).add(key)
            elif isinstance(node, FieldAccess):
                method = self._method_selections.get(node.node_id)
                if method is not None:
                    functions.add(method.declaration_key)

        for root in roots:
            walk(root, visit)
        return References(frozenset(functions), frozenset(params), frozenset(bindings))

    def _is_static(self, ref: BindingRef) -> bool:
        """Whether a ``let``/``var`` reference names a module-level binding, not a local."""
        return ref.module_id != self._module_id or ref.decl_node_id in self.static_bindings


def compute_references(
    resolved: ModuleResolution,
    method_selections: Mapping[int, MethodDef],
    slot_resolution: Mapping[int, BindingRef],
    module_id: ModuleId,
) -> ReferenceSummaries:
    """Summarize every ``def`` and static binding of a checked module."""
    collector = _Collector(resolved, method_selections, slot_resolution, module_id)
    declarations: dict[DeclarationKey, References] = {}
    for funcdef in static_function_items(resolved.program.body.items):
        key = (module_id, scope_names(funcdef.scope_path), funcdef.name)
        declarations[key] = collector.collect(*_function_roots(funcdef))
    bindings = {
        static_binding_key(module_id, scope_names(item.scope_path), item.name): collector.collect(
            item.value
        )
        for item in collector.static_bindings.values()
    }
    return ReferenceSummaries(declarations, bindings)


def _function_roots(funcdef: FuncDef) -> list[SyntaxNode]:
    """The expressions that run when *funcdef* does: parameter defaults and body."""
    roots: list[SyntaxNode] = [
        param.default for param in funcdef.params if param.default is not None
    ]
    if funcdef.body is not None:
        roots.append(funcdef.body)
    return roots
