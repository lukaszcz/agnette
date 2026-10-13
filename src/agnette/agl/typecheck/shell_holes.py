"""Static analysis of the holes in templates that reach an ``exec`` command.

:func:`checked_templates` finds the template literals whose text becomes the
command; :func:`calls_exempt_function` decides whether a hole is a direct call
of a function declared ``@exempts`` for a warning, which is what every
``%!{…}`` hole expands to. :func:`check_exec_holes` raises ``unquoted-exec-hole``
for the holes that neither carry a scalar nor a constant nor an exempt call, and
``shell-quoted-hole`` for standard quoting calls (:func:`is_std_quoting_call`) inside the
author's own shell quotes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from agnette.agl.diagnostics import Diagnostic, warning_diagnostic
from agnette.agl.modules.ids import ModuleId
from agnette.agl.scope.symbols import BinderKind, BindingRef, ConstructorRef
from agnette.agl.semantics.type_table import MethodDef
from agnette.agl.semantics.types import BoolType, DecimalType, IntType, Type
from agnette.agl.syntax.module_constants import FoldFailure, fold_hole_text
from agnette.agl.syntax.nodes import (
    Block,
    Call,
    Case,
    Expr,
    If,
    InterpSegment,
    Item,
    Template,
    TextSegment,
    Try,
    VarRef,
)
from agnette.agl.typecheck.constant_bindings import ModuleConstantBindings
from agnette.agl.typecheck.env import TypeEnvironment
from agnette.agl.warnings import SHELL_QUOTED_HOLE, UNQUOTED_EXEC_HOLE, StaticWarning
from agnette.util.posix_shell import HoleKind, QuoteState, hole_quote_states

__all__ = [
    "HoleCheckCtx",
    "calls_exempt_function",
    "check_exec_holes",
    "checked_templates",
    "is_std_quoting_call",
]

#: The functions that quote their argument into shell words, by module and name.
_STD_EXEC = ModuleId.from_path("std/exec")
_QUOTING_FUNCTIONS = frozenset({"shell-quote", "shell-join"})

_UNQUOTED = (
    "This hole inserts its value into the shell command verbatim. Quote it "
    "with '%!{…}' ('%!${NAME}' for an environment variable), or mark a "
    "deliberate splice with 'shell-raw'."
)
_HAND_QUOTED = " Quotes written around the hole do not protect the value."
_QUOTED = (
    "This hole quotes its value itself; inside quotes you wrote, its quotes become literal "
    "text or cancel the surrounding ones. Remove the surrounding quotes."
)


class HoleCheckCtx(Protocol):
    """The checker surface the hole analysis reads."""

    _env: TypeEnvironment
    _method_selections: dict[int, MethodDef]

    @property
    def _constant_bindings(self) -> ModuleConstantBindings: ...

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
    if isinstance(callee, VarRef):
        ref = _function_binding(callee, ctx)
        declaration = None if ref is None else ref.decl_node_id
    else:  # method selections are keyed by the callee `FieldAccess` node
        method = ctx._method_selections.get(callee.node_id)
        declaration = None if method is None else method.decl_node_id
    if declaration is None:
        return False
    signature = ctx._env.get_function_signature_by_node_id(declaration)
    return signature is not None and warning.name in signature.exempts


def is_std_quoting_call(expr: Expr, ctx: HoleCheckCtx) -> bool:
    """Whether *expr* directly calls ``std/exec``'s ``shell-quote`` or ``shell-join``.

    Identified by the resolved declaration, so every ``%!{…}`` qualifies and a same-named
    local function or ``shell-raw`` (which also exempts holes) does not.
    """
    if not (isinstance(expr, Call) and isinstance(expr.callee, VarRef)):
        return False
    ref = _function_binding(expr.callee, ctx)
    return ref is not None and ref.module_id == _STD_EXEC and ref.name in _QUOTING_FUNCTIONS


def _function_binding(callee: VarRef, ctx: HoleCheckCtx) -> BindingRef | None:
    """The function declaration *callee* names, or ``None`` for a constructor or other binding."""
    if ctx._constructor_ref_for(callee.node_id) is not None:
        return None
    ref = ctx._binding_for(callee.node_id)
    return ref if ref.kind is BinderKind.function_binding else None


def check_exec_holes(command: Expr, ctx: HoleCheckCtx) -> None:
    """Warn on the holes of the templates reaching *command*.

    ``unquoted-exec-hole`` marks each plain hole carrying runtime data: a hole is exempt when
    its type is ``int``, ``decimal`` or ``bool``, when it is a constant expression over fixed
    bindings (never a ``var`` or ``@param``), or when it directly calls a function exempting
    the warning. ``shell-quoted-hole`` marks a call of a standard quoting function (every
    ``%!{…}``) that sits inside shell quotes the author wrote, as judged by
    :func:`hole_quote_states`.
    """
    for template in checked_templates(command):
        states = _hole_states(template, ctx)
        for segment in template.segments:
            if not isinstance(segment, InterpSegment):
                continue
            quoted = states.get(segment.node_id) in (QuoteState.SINGLE, QuoteState.DOUBLE)
            if is_std_quoting_call(segment.expr, ctx):
                if quoted:
                    ctx._append_warning(
                        warning_diagnostic(SHELL_QUOTED_HOLE, _QUOTED, segment.span)
                    )
            elif not _is_exempt_hole(segment.expr, ctx):
                message = _UNQUOTED + (_HAND_QUOTED if quoted else "")
                ctx._append_warning(warning_diagnostic(UNQUOTED_EXEC_HOLE, message, segment.span))


def _hole_states(template: Template, ctx: HoleCheckCtx) -> dict[int, QuoteState]:
    """The quote state of each hole of *template* that stays in the scan, by node id.

    A constant hole is spliced into the text as its folded value and has no state. Any
    other hole is a word when it is a standard quoting call or a scalar, else opaque.
    """
    fragments = [""]
    kinds: list[HoleKind] = []
    holes: list[int] = []
    for segment in template.segments:
        if isinstance(segment, TextSegment):
            fragments[-1] += segment.text
        elif (constant := _constant_text(segment.expr, ctx)) is not None:
            fragments[-1] += constant
        else:
            holes.append(segment.node_id)
            kinds.append(
                HoleKind.WORD
                if is_std_quoting_call(segment.expr, ctx) or _is_scalar(segment.expr, ctx)
                else HoleKind.OPAQUE
            )
            fragments.append("")
    return dict(zip(holes, hole_quote_states(fragments, kinds), strict=True))


def _constant_text(expr: Expr, ctx: HoleCheckCtx) -> str | None:
    """The text a hole of fixed constant *expr* renders as, or ``None`` if it is not foldable."""
    if not ctx._is_fixed_constant_expr(expr):
        return None
    folded = fold_hole_text(expr, initializer_for=ctx._constant_bindings.initializer_for)
    return None if isinstance(folded, FoldFailure) else folded


def _is_scalar(expr: Expr, ctx: HoleCheckCtx) -> bool:
    return isinstance(ctx._node_type(expr.node_id), (IntType, DecimalType, BoolType))


def _is_exempt_hole(expr: Expr, ctx: HoleCheckCtx) -> bool:
    return (
        _is_scalar(expr, ctx)
        or ctx._is_fixed_constant_expr(expr)
        or calls_exempt_function(expr, UNQUOTED_EXEC_HOLE, ctx)
    )
