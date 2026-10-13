"""Tests for module-local constant classification and folding."""

from __future__ import annotations

from agnette.agl.modules.ids import ENTRY_ID
from agnette.agl.scope.symbols import ModuleResolution
from agnette.agl.syntax.nodes import Expr, LetDecl
from agnette.agl.syntax.types import TypeExpr
from agnette.agl.typecheck.constant_bindings import ModuleConstantBindings
from tests.agl.module_graph import resolve_entry

_DEPTH = 20


class _CountingBindings(ModuleConstantBindings):
    """Count initializer lookups while exercising the public binding behavior."""

    def __init__(self, resolved: ModuleResolution) -> None:
        self.initializer_lookups = 0
        super().__init__(resolved, ENTRY_ID, is_constructor=lambda _node_id: False)

    def initializer_for(self, node_id: int) -> tuple[Expr, TypeExpr | None] | None:
        self.initializer_lookups += 1
        return super().initializer_for(node_id)


def _chain() -> tuple[_CountingBindings, Expr]:
    source = 'let a0 = ""\n' + "".join(
        f'let a{index} = "%{{a{index - 1}}}%{{a{index - 1}}}"\n' for index in range(1, _DEPTH + 1)
    )
    resolved = resolve_entry(source, default_stdlib=False)
    declaration = next(
        item
        for item in resolved.program.body.items
        if isinstance(item, LetDecl) and item.name == f"a{_DEPTH}"
    )
    return _CountingBindings(resolved), declaration.value


def test_fixed_classification_memoizes_shared_initializers() -> None:
    bindings, initializer = _chain()

    assert bindings.is_fixed(initializer)
    assert bindings.initializer_lookups <= 3 * _DEPTH


def test_hole_folding_memoizes_shared_initializers() -> None:
    from agnette.agl.syntax.module_constants import fold_hole_text

    bindings, initializer = _chain()

    assert fold_hole_text(initializer, initializer_for=bindings.initializer_for) == ""
    assert bindings.initializer_lookups <= 3 * _DEPTH
