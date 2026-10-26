"""Static reachability of declarations over per-module reference summaries."""

from __future__ import annotations

from collections import deque

from agnette.agl.ir.static_keys import Reachable, StaticBindingKey
from agnette.agl.scope.symbols import DeclarationKey
from agnette.agl.typecheck.program import CheckedProgram


def reachable_from(checked: CheckedProgram, root: DeclarationKey) -> Reachable:
    """Breadth-first walk of the reference summaries from *root*.

    Follows function bodies and parameter defaults, and the initializers of
    reached static bindings (``@param``s included).
    """
    functions = {root}
    params: set[StaticBindingKey] = set()
    bindings: set[StaticBindingKey] = set()
    pending = deque([checked.modules[root[0]].declaration_references[root]])
    while pending:
        refs = pending.popleft()
        for function in refs.functions - functions:
            functions.add(function)
            pending.append(checked.modules[function[0]].declaration_references[function])
        for param in refs.params - params:
            params.add(param)
            pending.append(checked.modules[param[0]].binding_references[param])
        for binding in refs.bindings - bindings:
            bindings.add(binding)
            pending.append(checked.modules[binding[0]].binding_references[binding])
    return Reachable(frozenset(functions), frozenset(params))
