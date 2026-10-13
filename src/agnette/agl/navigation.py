"""Go-to-definition: map a source position to the declarations its name denotes.

Reads only pipeline results: scope resolution for names, and type checking, when it
succeeded, for type-directed selections (fields, methods, bare patterns).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.lexical import lexical_spans
from agnette.agl.highlight.semantic import NameOccurrence, SpannedNode, name_occurrences
from agnette.agl.modules.ids import ModuleId
from agnette.agl.scope.program import ResolvedProgram
from agnette.agl.scope.symbols import (
    BindingRef,
    ConstructorRef,
    DeclarationKey,
    DeclarationSelection,
    ModuleResolution,
    OwnerMemberSelection,
    TypeSelection,
    qname_declaration,
)
from agnette.agl.semantics.types import ExceptionType, RecordType
from agnette.agl.syntax.nodes import (
    AsPattern,
    AssignStmt,
    BuiltinVarDecl,
    CatchClause,
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
    LetDecl,
    Loop,
    Param,
    QualifierChain,
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
from agnette.agl.syntax.types import AppliedT, NameT
from agnette.agl.syntax.visitor import walk
from agnette.agl.typecheck.env import CheckedModule
from agnette.agl.typecheck.program import CheckedProgram
from agnette.util.text import normalize_newlines

__all__ = ["Location", "definitions", "source_offset"]

_DECLARATIONS = (
    FuncDef,
    RecordDef,
    EnumDef,
    ExceptionDef,
    TypeAlias,
    LetDecl,
    VarDecl,
    BuiltinVarDecl,
    VariantDef,
    Param,
    AsPattern,
    Loop,
    ScopeRegion,
)
_SCOPED = (FuncDef, RecordDef, EnumDef, ExceptionDef, TypeAlias, LetDecl, VarDecl)
_VALUE_NAMES = (VarRef, ConstructorPattern, NameT, AppliedT, IsTest, VarPattern)


@dataclass(frozen=True, slots=True)
class Location:
    """A 1-based *line* and character *column* in *path*, with that line's *text*."""

    path: Path
    line: int
    column: int
    text: str


@dataclass(frozen=True, slots=True)
class _Target:
    """The declaration node *node_id* in *module_id*, whose declared spelling is *name*."""

    module_id: ModuleId
    node_id: int
    name: str


def source_offset(text: str, line: int, column: int) -> int | None:
    """Return the offset of 1-based *line* and *column* in *text*, or ``None`` past its end."""
    lines = text.split("\n")
    if not (1 <= line <= len(lines) and 1 <= column <= len(lines[line - 1]) + 1):
        return None
    return sum(len(previous) + 1 for previous in lines[: line - 1]) + column - 1


class _ModuleIndex:
    """One module's text, name occurrences, nodes by id, qualifier chains, and module paths."""

    def __init__(self, program: ResolvedProgram, module_id: ModuleId, text: str) -> None:
        self.text = text
        self.spans = lexical_spans(text)
        self.occurrences = name_occurrences(text, self.spans, program, module_id)
        self.starts = [occurrence.start for occurrence in self.occurrences]
        # First start of each spelling within a node.
        self.named = {(o.node.node_id, o.name): o.start for o in reversed(self.occurrences)}
        self.nodes: dict[int, SpannedNode] = {}
        self.chains: list[QualifierChain] = []
        self.module_paths: list[ImportDecl | ExportDecl] = []
        self.headers: set[int] = set()
        walk(program.modules[module_id].resolved.program, self._collect)

    def _collect(self, node: object) -> None:
        if isinstance(node, SpannedNode):
            self.nodes[node.node_id] = node
        if isinstance(node, QualifierChain):
            self.chains.append(node)
        if isinstance(node, (ImportDecl, ExportDecl)):
            self.module_paths.append(node)
        if isinstance(node, ScopeRegion):
            self.headers.add(node.segment.node_id)

    def occurrence_at(self, offset: int) -> NameOccurrence | None:
        """Return the name containing *offset*, else the one ending there."""
        index = bisect_right(self.starts, offset) - 1
        if index < 0:
            return None
        occurrence = self.occurrences[index]
        return occurrence if offset <= occurrence.end else None

    def category_at(self, offset: int) -> Category | None:
        for start, end, category in self.spans:
            if start <= offset < end:
                return category
        return None

    def location(self, path: Path, offset: int) -> Location:
        line_start = self.text.rfind("\n", 0, offset) + 1
        line_end = self.text.find("\n", offset)
        return Location(
            path,
            self.text.count("\n", 0, offset) + 1,
            offset - line_start + 1,
            self.text[line_start : len(self.text) if line_end < 0 else line_end],
        )


class _Navigator:
    def __init__(
        self, program: ResolvedProgram, checked: CheckedProgram | None, source: str
    ) -> None:
        self.program = program
        self.checked = checked
        self.indices = {program.entry_id: _ModuleIndex(program, program.entry_id, source)}

    def index(self, module_id: ModuleId) -> _ModuleIndex:
        if module_id not in self.indices:
            path = self.path(module_id)
            assert path is not None, "only an inline entry has no file"
            text = normalize_newlines(path.read_text(encoding="utf-8"))
            self.indices[module_id] = _ModuleIndex(self.program, module_id, text)
        return self.indices[module_id]

    def path(self, module_id: ModuleId) -> Path | None:
        return self.resolution(module_id).origin_path

    def node(self, target: _Target) -> SpannedNode:
        return self.index(target.module_id).nodes[target.node_id]

    def resolution(self, module_id: ModuleId) -> ModuleResolution:
        return self.program.modules[module_id].resolved

    def checked_module(self, module_id: ModuleId) -> CheckedModule | None:
        return None if self.checked is None else self.checked.modules.get(module_id)

    # -- locations -------------------------------------------------------

    def locate(self, target: _Target | ModuleId) -> Location | None:
        """Locate a declaration's name, or a module's start."""
        module_id = target if isinstance(target, ModuleId) else target.module_id
        path = self.path(module_id)
        if path is None:
            return None
        index = self.index(module_id)
        if isinstance(target, ModuleId):
            return index.location(path, 0)
        node = self.node(target)
        if isinstance(node, ScopeRegion):
            node = node.segment
        # An implicitly declared scope's node is the first declaration spelling it.
        prefixes = node.scope_path if isinstance(node, _SCOPED) else ()
        start = next(
            index.named[key]
            for candidate in (node, *prefixes)
            if (key := (candidate.node_id, target.name)) in index.named
        )
        return index.location(path, start)

    # -- targets ---------------------------------------------------------

    def binding(self, ref: BindingRef) -> _Target:
        return _Target(ref.module_id, ref.decl_node_id, ref.name)

    def constructor(self, ref: ConstructorRef) -> _Target:
        return _Target(ref.owner_module_id, ref.owner_decl_node_id, ref.owner_name)

    def key(self, key: DeclarationKey) -> _Target:
        """The declaration or scope *key* names; types are declarations."""
        module_id, path, name = key
        resolved = self.resolution(module_id)
        if key in resolved.declarations:
            return self.binding(resolved.declarations[key])
        return _Target(module_id, resolved.scope_nodes[(*path, name)].node_id, name)

    def selection(self, selection: TypeSelection | None) -> Iterator[_Target]:
        if isinstance(selection, DeclarationSelection):
            yield self.key(selection.key)
        elif isinstance(selection, OwnerMemberSelection):
            owner = self.key(selection.owner)
            enum = self.node(owner)
            members = enum.members if isinstance(enum, EnumDef) else ()
            for member in members:
                if isinstance(member, VariantDef) and member.name == selection.member:
                    yield _Target(owner.module_id, member.node_id, member.name)

    def references(self, resolved: ModuleResolution, node_id: int, name: str) -> Iterator[_Target]:
        for qname in sorted(resolved.name_references.get((node_id, name), ()), key=str):
            yield self.key(qname_declaration(qname))

    def reference(
        self, module_id: ModuleId, node: SpannedNode
    ) -> BindingRef | ConstructorRef | None:
        """The binding or constructor a value or pattern *node* refers to."""
        checked = self.checked_module(module_id)
        node_id = node.node_id
        if checked is None:
            resolved = self.resolution(module_id)
            return resolved.resolution.get(node_id) or resolved.constructor_refs.get(node_id)
        return (
            checked.pattern_binding_refs.get(node_id)
            or checked.binding_for(node_id)
            or checked.pattern_constructor_refs.get(node_id)
            or checked.constructor_ref_for(node_id)
            or checked.pattern_classifications.get(node_id)
        )

    def value(self, module_id: ModuleId, node: SpannedNode) -> Iterator[_Target]:
        """Targets of a value, pattern, or type name node."""
        resolved = self.resolution(module_id)
        reference = self.reference(module_id, node)
        if isinstance(reference, BindingRef):
            yield self.binding(reference)
        elif isinstance(reference, ConstructorRef):
            yield self.constructor(reference)
        elif isinstance(node, (NameT, AppliedT)):
            chain = node.qualifier
            selection = resolved.owner_declarations.get(node.node_id)
            if selection is None and chain is not None:
                selection = resolved.owner_declarations.get(chain.node_id)
            yield from self.selection(selection)
        elif isinstance(node, VarPattern) and (
            self.checked_module(module_id) is not None
            or not resolved.pattern_constructor_candidates.get(node.node_id)
        ):
            yield _Target(module_id, node.node_id, node.name)

    def member(self, module_id: ModuleId, node: FieldAccess | FieldTarget) -> Iterator[_Target]:
        checked = self.checked_module(module_id)
        if checked is None:
            return
        method = checked.method_selections.get(node.node_id)
        if method is not None:
            yield _Target(method.module_id, method.decl_node_id, method.name)
            return
        owner = checked.node_types[node.obj.node_id]
        assert isinstance(owner, (RecordType, ExceptionType)), "only nominal types have fields"
        yield from self.field(_Target(owner.module_id, owner.decl_id, owner.name), node.field)

    def field(self, owner: _Target, name: str) -> Iterator[_Target]:
        """Field *name* of a record or exception, else of an exception's base."""
        declaration = self.node(owner)
        assert isinstance(declaration, (RecordDef, VariantDef, ExceptionDef))
        for field in declaration.fields:
            if field.name == name:
                yield _Target(owner.module_id, field.node_id, name)
                return
        base = self.resolution(owner.module_id).owner_declarations.get(declaration.node_id)
        for selected in self.selection(base):
            yield from self.field(selected, name)

    def occurrence(self, module_id: ModuleId, occurrence: NameOccurrence) -> Iterator[_Target]:
        node, name = occurrence.node, occurrence.name
        resolved = self.resolution(module_id)
        if isinstance(node, ExceptionDef) and name == node.base:
            yield from self.selection(resolved.owner_declarations.get(node.node_id))
        elif isinstance(node, CatchClause):
            if name == node.binding:
                yield _Target(module_id, node.node_id, name)
            else:
                yield from self.selection(resolved.owner_declarations.get(node.node_id))
        elif isinstance(node, _DECLARATIONS):
            yield _Target(module_id, node.node_id, name)
        elif isinstance(node, (FieldAccess, FieldTarget)):
            yield from self.member(module_id, node)
        elif isinstance(node, AssignStmt):
            yield self.binding(resolved.resolution[node.node_id])
        elif isinstance(node, (ImportItem, ExportItem)):
            yield from self.references(resolved, node.node_id, node.name)
        elif isinstance(node, UseDecl):
            yield from self.references(resolved, node.node_id, node.target[-1].name)
        elif isinstance(node, ScopeSegment) and node.node_id in self.index(module_id).headers:
            yield _Target(module_id, node.node_id, name)
        elif isinstance(node, (ScopeSegment, InfixDecl)):
            yield from self.references(resolved, node.node_id, name)
        elif isinstance(node, VariantRef):
            yield from self.selection(resolved.owner_declarations.get(node.chain.node_id))
        elif isinstance(node, _VALUE_NAMES):
            yield from self.value(module_id, node)

    def qualifier(
        self, module_id: ModuleId, chain: QualifierChain, segment: int
    ) -> Iterator[_Target | ModuleId]:
        """Targets of qualifier *segment*, aligned with the path its whole *chain* selects."""
        selection = self.resolution(module_id).owner_declarations[chain.node_id]
        if isinstance(selection, OwnerMemberSelection):
            target_module, owner_path, owner = selection.owner
            path = (*owner_path, owner)
        else:
            target_module, path, _ = selection.key
        names = tuple(s.name for s in chain.segments)
        if path[len(path) - len(names) :] == names:
            depth = len(path) - len(names) + segment
        elif names[len(names) - len(path) :] == path:
            # Leading segments beyond the declaration's scope path spell its module.
            depth = segment - (len(names) - len(path))
            if depth < 0:
                yield target_module
                return
        else:
            return
        yield self.key((target_module, path[:depth], path[depth]))

    def modules(self, declaration: ImportDecl | ExportDecl) -> list[ModuleId]:
        path = declaration.module_path
        return sorted(
            (
                module_id
                for module_id in self.program.graph.modules
                if module_id.segments == path
                or (declaration.wildcard and module_id.segments[: len(path)] == path)
            ),
            key=str,
        )

    def at(self, offset: int) -> Iterator[_Target | ModuleId]:
        entry = self.program.entry_id
        index = self.indices[entry]
        for chain in index.chains:
            for position, segment in enumerate(chain.segments):
                start = segment.span.start_offset
                if start <= offset <= start + len(segment.name):
                    yield from self.qualifier(entry, chain, position)
                    return
        occurrence = index.occurrence_at(offset)
        if occurrence is not None:
            if isinstance(occurrence.node, ImportDecl) and occurrence.name == occurrence.node.alias:
                yield from self.modules(occurrence.node)
            else:
                yield from self.occurrence(entry, occurrence)
            return
        if index.category_at(offset) is Category.MODULE:
            for declaration in index.module_paths:
                if declaration.span.start_offset <= offset < declaration.span.end_offset:
                    yield from self.modules(declaration)


def definitions(
    program: ResolvedProgram, checked: CheckedProgram | None, source: str, offset: int
) -> list[Location]:
    """Return the declarations the entry name at *offset* denotes, in a stable order.

    *source* is the entry's normalized text and *offset* a character offset into it.
    *checked* is ``None`` when type checking failed: type-directed names then resolve
    only where scope alone decides them.
    """
    navigator = _Navigator(program, checked, source)
    locations: list[Location] = []
    for target in navigator.at(offset):
        location = navigator.locate(target)
        if location is not None:
            locations.append(location)
    return locations
