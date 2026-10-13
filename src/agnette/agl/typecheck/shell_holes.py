"""Static analysis of the holes in templates that reach an ``exec`` command.

:func:`checked_templates` finds the template literals whose text becomes the
command; :func:`calls_exempt_function` decides whether a hole is a direct call
of a function declared ``@exempts`` for a warning, which is what every
``%!{…}`` hole expands to. :func:`check_exec_holes` raises ``unquoted-exec-hole``
for the holes that neither carry a scalar nor a constant nor an exempt call.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from agnette.agl.diagnostics import Diagnostic, warning_diagnostic
from agnette.agl.scope.symbols import BinderKind, BindingRef, ConstructorRef
from agnette.agl.semantics.type_table import MethodDef
from agnette.agl.semantics.types import BoolType, DecimalType, IntType, Type
from agnette.agl.syntax.nodes import (
    Block,
    Call,
    Case,
    Expr,
    FieldAccess,
    If,
    InterpSegment,
    Item,
    Template,
    Try,
    VarRef,
)
from agnette.agl.typecheck.env import TypeEnvironment
from agnette.agl.warnings import UNQUOTED_EXEC_HOLE, StaticWarning

__all__ = ["HoleCheckCtx", "calls_exempt_function", "check_exec_holes", "checked_templates"]


class HoleCheckCtx(Protocol):
    """The checker surface the hole analysis reads."""

    _env: TypeEnvironment
    _method_selections: dict[int, MethodDef]

    def _node_type(self, node_id: int) -> Type: ...

    def _is_fixed_constant_expr(self, expr: Expr) -> bool: ...

    def _binding_for(self, node_id: int) -> BindingRef: ...

    def _constructor_ref_for(self, node_id: int) -> ConstructorRef | None: ...

    def _append_warning(self, warning: Diagnostic) -> None: ...


def checked_templates(command: Item) -> Iterator[Template]:
    """Yield the template literals in *command*'s result positions.

    A template is its own result; ``if``/``case`` arms, a block's final item, and
    a ``try`` body and its ``catch`` arms pass their results through. Nothing
    else is followed: bindings, pipes and operators do not make a template reach
    the command.
    """
    match command:
        case Template():
            yield command
        case If(branches=branches):
            for branch in branches:
                yield from checked_templates(branch.body)
        case Case(branches=branches):
            for case_branch in branches:
                yield from checked_templates(case_branch.body)
        case Block(items=items):
            yield from checked_templates(items[-1])
        case Try(body=body, handlers=handlers):
            yield from checked_templates(body)
            for handler in handlers:
                yield from checked_templates(handler.body)


def calls_exempt_function(expr: Expr, warning: StaticWarning, ctx: HoleCheckCtx) -> bool:
    """Whether *expr* is a direct function or method call whose declaration exempts *warning*."""
    if not isinstance(expr, Call):
        return False
    callee = expr.callee
    declaration: int | None = None
    if isinstance(callee, VarRef) and ctx._constructor_ref_for(callee.node_id) is None:
        ref = ctx._binding_for(callee.node_id)
        if ref.kind is BinderKind.function_binding:
            declaration = ref.decl_node_id
    elif isinstance(callee, FieldAccess):
        method = ctx._method_selections.get(callee.node_id)
        declaration = None if method is None else method.decl_node_id
    if declaration is None:
        return False
    signature = ctx._env.get_function_signature_by_node_id(declaration)
    return signature is not None and warning.name in signature.exempts


def check_exec_holes(command: Expr, ctx: HoleCheckCtx) -> None:
    """Warn ``unquoted-exec-hole`` on each plain hole carrying runtime data into *command*.

    A hole is exempt when its type is ``int``, ``decimal`` or ``bool``, when it is a constant
    expression over fixed bindings (never a ``var`` or ``@param``), or when it directly calls a
    function exempting the warning.
    """
    for template in checked_templates(command):
        for segment in template.segments:
            if isinstance(segment, InterpSegment) and not _is_exempt_hole(segment.expr, ctx):
                ctx._append_warning(
                    warning_diagnostic(
                        UNQUOTED_EXEC_HOLE,
                        "This hole inserts its value into the shell command verbatim. Quote it "
                        "with '%!{…}' ('%!${NAME}' for an environment variable), or mark a "
                        "deliberate splice with 'shell-raw'.",
                        segment.span,
                    )
                )


def _is_exempt_hole(expr: Expr, ctx: HoleCheckCtx) -> bool:
    return (
        isinstance(ctx._node_type(expr.node_id), (IntType, DecimalType, BoolType))
        or ctx._is_fixed_constant_expr(expr)
        or calls_exempt_function(expr, UNQUOTED_EXEC_HOLE, ctx)
    )
