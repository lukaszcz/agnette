"""Scope-resolved names over exact lexical spans; never check types or execute code."""

from bisect import bisect_left
from collections.abc import Iterable
from itertools import chain as iter_chain

from agnette.agl.highlight.categories import Category, Span
from agnette.agl.highlight.context import HiddenContext
from agnette.agl.highlight.lexical import _source_offsets, lexical_spans
from agnette.agl.modules.roots import RootSet
from agnette.agl.pipeline import PipelineDriver
from agnette.agl.scope.program import ResolvedProgram
from agnette.agl.scope.symbols import (
    BinderKind,
    BindingRef,
    ModuleResolution,
    QName,
    qname_declaration,
)
from agnette.agl.syntax.nodes import (
    AsPattern,
    AssignStmt,
    BuiltinVarDecl,
    CatchClause,
    Constraint,
    ConstructorPattern,
    EnumDef,
    ExceptionDef,
    ExportDecl,
    ExportItem,
    FieldAccess,
    FieldTarget,
    FuncDef,
    ImportDecl,
    ImportItem,
    InfixDecl,
    IsTest,
    Lambda,
    LetDecl,
    Loop,
    NamedArg,
    NameTarget,
    Param,
    PatternField,
    QualifierChain,
    QualifierSegment,
    RecordDef,
    ScopeRegion,
    ScopeSegment,
    TypeAlias,
    UseDecl,
    VarDecl,
    VariantDef,
    VariantRef,
    VarPattern,
    VarRef,
)
from agnette.agl.syntax.spans import SourceSpan
from agnette.agl.syntax.types import AppliedT, NameT
from agnette.agl.syntax.visitor import walk
from agnette.util.text import normalize_newlines

__all__ = ["semantic_spans"]

_NAME_CATEGORIES = frozenset(
    {Category.NAME, Category.TYPE, Category.CONSTRUCTOR, Category.BUILTIN_CALL, Category.MODULE}
)
_BINDING_CATEGORIES = {
    BinderKind.function_binding: Category.FUNCTION,
    BinderKind.constructor_binding: Category.CONSTRUCTOR,
    BinderKind.param_binding: Category.PARAMETER,
}


def _binding_category(ref: BindingRef, resolved: ModuleResolution) -> Category:
    if ref.kind is BinderKind.pattern_slot:
        slot = resolved.pattern_slots[ref.slot_id] if ref.slot_id is not None else None
        if slot is None or all(candidate.can_match_bare_pattern for candidate in slot.candidates):
            return Category.NAME
    if ref.kind is BinderKind.function_binding and ref.is_builtin:
        return Category.BUILTIN_CALL
    return Category.PARAMETER if ref.is_param else _BINDING_CATEGORIES.get(ref.kind, Category.LOCAL)


class _Names:
    """Locate a spelling after its syntactic prefixes, never inside another name node."""

    def __init__(self, text: str, spans: list[Span]) -> None:
        self.starts: dict[str, list[int]] = {}
        for start, end, category in spans:
            if category in _NAME_CATEGORIES:
                self.starts.setdefault(text[start:end], []).append(start)
        self.overrides: dict[int, Category] = {}

    def mark(
        self,
        span: SourceSpan,
        name: str,
        category: Category,
        *,
        after: Iterable[SourceSpan] = (),
        before: int | None = None,
        start_at: int | None = None,
    ) -> int:
        start = max((s.end_offset for s in after), default=span.start_offset)
        if start_at is not None:
            start = max(start, start_at)
        end = span.end_offset if before is None else before
        starts = self.starts.get(name, [])
        first, stop = bisect_left(starts, start), bisect_left(starts, end)
        if first < stop:
            start = starts[first]
            self.overrides[start] = category
            return start + len(name)
        return start

    def member(
        self, span: SourceSpan, name: str, category: Category, chain: QualifierChain | None
    ) -> None:
        self.mark(
            span, name, category, after=(() if chain is None else (s.span for s in chain.segments))
        )


def _import_category(origins: Iterable[QName], program: ResolvedProgram) -> Category:
    distinct = set(origins)
    if len(distinct) != 1:
        return Category.NAME
    key = qname_declaration(distinct.pop())
    resolved = program.modules[key[0]].resolved
    if resolved.scope_entity_kinds[key] == "type":
        return Category.TYPE
    return _binding_category(resolved.declarations[key], resolved)


def _overlay_names(text: str, spans: list[Span], program: ResolvedProgram) -> list[Span]:
    """Overlay scope verdicts; candidates awaiting type checking stay neutral."""
    module = program.modules[program.entry_id]
    resolved = module.resolved
    names = _Names(text, spans)
    parameters: set[int] = set()
    regions: dict[SourceSpan, list[ScopeRegion]] = {}
    slots = {
        candidate.pattern_node_id: slot
        for slot in resolved.pattern_slots.values()
        for candidate in slot.candidates
    }

    def reference_category(node_id: int, name: str) -> Category:
        return _import_category(resolved.name_references.get((node_id, name), ()), program)

    def classify(node: object) -> None:
        if isinstance(node, (FuncDef, Lambda)):
            parameters.update(param.node_id for param in node.params)
        if isinstance(
            node,
            (
                FuncDef,
                RecordDef,
                EnumDef,
                ExceptionDef,
                TypeAlias,
                LetDecl,
                VarDecl,
                BuiltinVarDecl,
                VariantDef,
            ),
        ):
            prefixes = [a.span for a in node.attributes]
            if not isinstance(node, VariantDef):
                prefixes.extend(s.span for s in node.scope_path)
            if isinstance(node, FuncDef):
                if node.receiver_type is not None:
                    prefixes.append(node.receiver_type.span)
                    walk(node.receiver_type, classify)
                category = Category.BUILTIN_CALL if node.is_builtin else Category.FUNCTION
            elif isinstance(node, (RecordDef, EnumDef, ExceptionDef, TypeAlias)):
                category = Category.TYPE
            elif isinstance(node, VariantDef):
                category = Category.CONSTRUCTOR
            else:
                category = (
                    Category.PARAMETER
                    if node.node_id in resolved.attributes.params
                    else Category.LOCAL
                )
            end = names.mark(node.span, node.name, category, after=prefixes)
            if isinstance(node, (FuncDef, RecordDef, EnumDef, ExceptionDef, TypeAlias)):
                for param in node.type_params:
                    end = names.mark(node.span, param, Category.TYPE, start_at=end)
            if isinstance(node, ExceptionDef) and node.base is not None:
                names.mark(node.span, node.base, Category.TYPE, start_at=end)
        elif isinstance(node, Param):
            names.mark(
                node.span,
                node.name,
                Category.PARAMETER if node.node_id in parameters else Category.NAME,
                after=(a.span for a in node.attributes),
            )
        elif isinstance(node, (FieldAccess, FieldTarget)):
            names.mark(node.span, node.field, Category.NAME, after=(node.obj.span,))
        elif isinstance(node, (NamedArg, PatternField)):
            names.mark(node.span, node.name, Category.NAME)
        elif isinstance(node, Constraint):
            end = names.mark(node.span, node.kind.value, Category.NAME)
            names.mark(node.span, node.param, Category.TYPE, start_at=end)
        elif isinstance(node, ScopeRegion):
            regions.setdefault(node.span, []).append(node)
        elif isinstance(node, ScopeSegment):
            names.mark(node.span, node.name, reference_category(node.node_id, node.name))
        elif isinstance(node, InfixDecl):
            end = names.mark(node.span, node.name, reference_category(node.node_id, node.name))
            if node.priority_base is not None:
                names.mark(
                    node.span,
                    node.priority_base,
                    reference_category(node.node_id, node.priority_base),
                    start_at=end,
                )
        elif isinstance(node, AsPattern):
            names.mark(node.span, node.name, Category.LOCAL, after=(node.pattern.span,))
        elif isinstance(node, Loop) and node.for_var is not None:
            names.mark(
                node.span,
                node.for_var,
                Category.LOCAL,
                before=node.for_iter.span.start_offset if node.for_iter is not None else None,
            )
        elif isinstance(node, CatchClause):
            end = node.span.start_offset
            if node.exc_type is not None:
                end = names.mark(
                    node.span, node.exc_type, Category.TYPE, before=node.body.span.start_offset
                )
            if node.binding is not None:
                names.mark(
                    node.span,
                    node.binding,
                    Category.LOCAL,
                    start_at=end,
                    before=node.body.span.start_offset,
                )
        elif isinstance(node, AssignStmt) and isinstance(node.target, NameTarget):
            names.member(
                node.target.span,
                node.target.name,
                _binding_category(resolved.resolution[node.node_id], resolved),
                node.target.qualifier,
            )
        elif isinstance(node, (ImportDecl, ExportDecl, UseDecl)):
            items: tuple[ImportItem | ExportItem, ...]
            if isinstance(node, UseDecl):
                items = node.tail or ()
                if node.alias is not None:
                    origins = resolved.name_references.get((node.node_id, node.target[-1].name), ())
                    category = _import_category(origins, program) if origins else Category.MODULE
                    names.mark(node.span, node.alias, category, after=(s.span for s in node.target))
            else:
                items = node.items if isinstance(node, ExportDecl) else node.tail or ()
                if isinstance(node, ImportDecl) and node.alias is not None:
                    names.mark(node.span, node.alias, Category.MODULE)
            hidden: tuple[ImportItem | ExportItem, ...] = node.hidden
            for item in iter_chain(items, hidden):
                category = reference_category(item.node_id, item.name)
                end = names.mark(
                    item.span, item.name, category, after=(s.span for s in item.scope_path)
                )
                if item.rename is not None:
                    names.mark(item.span, item.rename, category, start_at=end)
        elif isinstance(node, VariantRef):
            category = (
                Category.TYPE
                if node.chain.node_id in resolved.owner_declarations
                else Category.NAME
            )
            names.member(node.span, node.chain.member, category, node.chain)
        elif isinstance(node, IsTest):
            category = (
                Category.CONSTRUCTOR if node.node_id in resolved.constructor_refs else Category.NAME
            )
            names.mark(
                node.span,
                node.variant,
                category,
                after=(node.expr.span, *(s.span for s in node.qualifier.segments))
                if node.qualifier is not None
                else (node.expr.span,),
            )
        elif isinstance(node, QualifierSegment) and node.type_args is not None:
            names.mark(node.span, node.name, Category.TYPE)
        elif isinstance(node, (VarRef, ConstructorPattern, VarPattern, NameT, AppliedT)):
            ref = resolved.resolution.get(node.node_id)
            chain = None if isinstance(node, VarPattern) else node.qualifier
            if ref is not None:
                category = _binding_category(ref, resolved)
            elif node.node_id in resolved.constructor_refs:
                category = Category.CONSTRUCTOR
            elif isinstance(node, (NameT, AppliedT)):
                category = Category.TYPE
            elif isinstance(node, VarPattern) and node.node_id in slots:
                slot = slots[node.node_id]
                category = (
                    Category.NAME
                    if any(c.can_match_bare_pattern for c in slot.candidates)
                    else Category.LOCAL
                )
            else:
                category = Category.NAME
            names.member(node.span, node.name, category, chain)

    walk(resolved.program, classify)
    # Multi-segment headers share a normalized region span; closers have no AST nodes.
    for span, group in regions.items():

        def segment_start(segment: ScopeSegment) -> int:
            return segment.span.start_offset

        segments = sorted((region.segment for region in group), key=segment_start)
        end = max(
            (
                *(s.span.end_offset for s in segments),
                *(
                    item.span.end_offset
                    for region in group
                    for item in region.items
                    if not isinstance(item, ScopeRegion) or item.span != span
                ),
            )
        )
        for segment in segments:
            end = names.mark(span, segment.name, Category.NAME, start_at=end)
    return [(a, b, names.overrides.get(a, c)) for a, b, c in spans]


def semantic_spans(
    source: str,
    context: HiddenContext = HiddenContext(),
    *,
    roots: RootSet | None = None,
) -> list[Span]:
    """Return a total body partition, with scope-resolved names when preparation succeeds.

    The pipeline's process-wide parsed-module and scope artifact caches retain
    libraries across calls. No highlighting-specific compiler cache is needed.
    ``roots=None`` uses the driver's default standard-library resolver.
    """
    types, constructors = context.declared_names
    fallback = lexical_spans(source, types, constructors)
    try:
        combined = context.prepend(source)
        text = normalize_newlines(combined)
        parsed = PipelineDriver.parse_entry(text)
        prepared = PipelineDriver.prepare_parsed_entry(parsed, roots=roots)
        if prepared.resolved is None:
            return fallback
        spans = _overlay_names(text, lexical_spans(text, types, constructors), prepared.resolved)
        offsets = _source_offsets(combined)
        return context.body_spans([(offsets[a], offsets[b], c) for a, b, c in spans])
    except Exception:
        # Highlighting is optional presentation, including for arbitrary fragments.
        return fallback
