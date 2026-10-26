"""Cross-member declaration validation shared by module and program checking."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Literal, TypeGuard

from agnette.agl.modules.ids import ModuleId, spell_declaration
from agnette.agl.scope.symbols import ModuleResolution
from agnette.agl.semantics.type_table import (
    DeclId,
    ExceptionNesting,
    TypeTable,
    decl_id_sort_key,
    qualified_decl_name,
)
from agnette.agl.syntax.nodes import (
    EnumDef,
    ExceptionDef,
    FuncDef,
    Item,
    RecordDef,
    TypeAlias,
    static_function_items,
    static_items,
    static_type_items,
)
from agnette.agl.syntax.spans import SourceSpan
from agnette.agl.typecheck.env import AglTypeError

SessionBuiltinDeclarations = Mapping[tuple[str, ...], tuple[ModuleId, tuple[str, ...]]]
"""A REPL session's builtin identities from earlier, still-live entries, keyed by scoped name."""


@dataclass(frozen=True, slots=True)
class _MemberDeclaration:
    """A member declaration whose name occupies a nominal member namespace.

    ``origin`` is the declaration's provenance: ``"source"`` for one read
    from a program being compiled here, ``"registered"`` for one read back
    from the shared type table (an imported owner, or a type retained from an
    earlier REPL entry). A source declaration supersedes the registration of
    the same member it produced on a previous pass.

    ``span`` locates a declaration for reporting; it is not provenance.

    ``module_id`` is the declaration's owning module: a method's declaring
    module (the module whose program declares the function, not its owner's
    — orphan methods exist), or a field's owner's own module (a field never
    participates in the same-module pair rule, so its value is unused there).
    """

    kind: Literal["field", "method"]
    span: SourceSpan | None
    module_id: ModuleId
    origin: Literal["source", "registered"]


@dataclass(slots=True)
class _MemberIndex:
    """Every nominal member namespace this validation can reach.

    ``members`` is the single source for the question "what does this owner
    declare?": a program being compiled here contributes ``"source"`` members,
    and an owner outside this compile unit — an imported base, or a type
    retained from an earlier REPL entry — contributes ``"registered"`` ones
    from its registered fields and directly declared methods. ``declared``
    names the owners this compile unit contributes to, which are exactly the
    owners whose members need checking.
    """

    members: dict[DeclId, dict[str, list[_MemberDeclaration]]] = field(default_factory=dict)
    declared: set[DeclId] = field(default_factory=set)

    def members_of(self, owner_id: DeclId) -> Mapping[str, list[_MemberDeclaration]]:
        """Return *owner_id*'s member namespace, empty for an owner never indexed."""
        return self.members.get(owner_id, {})


def _index_registered_owner(index: _MemberIndex, type_table: TypeTable, owner_id: DeclId) -> None:
    """Index an owner outside this compile unit from its registered declaration."""
    if owner_id in index.members:
        return
    typedef = type_table.typedef_of(owner_id)
    members = index.members.setdefault(owner_id, {})
    for field_name, _field_type in typedef.fields:
        members.setdefault(field_name, []).append(
            _MemberDeclaration("field", None, typedef.module_id, "registered")
        )
    for method_name, methods in type_table.declared_methods(owner_id).items():
        same_named = members.setdefault(method_name, [])
        same_named.extend(
            _MemberDeclaration("method", None, method.module_id, "registered") for method in methods
        )


def _member_declarations(
    modules: Mapping[ModuleId, ModuleResolution], type_table: TypeTable
) -> _MemberIndex:
    """Collect source and retained-owner fields plus scope-classified methods."""
    index = _MemberIndex()
    owner_ids: dict[tuple[ModuleId, tuple[str, ...]], DeclId] = {}
    for module_id, resolved in modules.items():
        for item in static_type_items(resolved.program.body.items):
            if isinstance(item, TypeAlias):
                continue
            declared_path = tuple(segment.name for segment in item.scope_path)
            decl_owner_id = type_table.named(module_id, item.name, declared_path).decl_node_id
            owner_ids[module_id, (*declared_path, item.name)] = decl_owner_id
            index.declared.add(decl_owner_id)
            members = index.members.setdefault(decl_owner_id, {})
            if isinstance(item, (RecordDef, ExceptionDef)):
                for source_field in item.fields:
                    same_named = members.setdefault(source_field.name, [])
                    same_named[:] = [
                        member
                        for member in same_named
                        if not (member.kind == "field" and member.origin == "registered")
                    ]
                    same_named.append(
                        _MemberDeclaration("field", source_field.span, module_id, "source")
                    )
        for function in static_function_items(resolved.program.body.items):
            owner_path = resolved.receiver_owner_for(module_id, function)
            if owner_path is None or owner_path.builtin is not None:
                continue
            method_owner_id = owner_ids.get((owner_path.module_id, owner_path.scope_path))
            if method_owner_id is None:
                # A method on an owner retained from an earlier REPL entry or
                # another module: its members come from the shared type table.
                method_owner_id = type_table.named(
                    owner_path.module_id, owner_path.scope_path[-1], owner_path.scope_path[:-1]
                ).decl_node_id
                owner_ids[owner_path.module_id, owner_path.scope_path] = method_owner_id
                _index_registered_owner(index, type_table, method_owner_id)
            index.declared.add(method_owner_id)
            same_named = index.members.setdefault(method_owner_id, {}).setdefault(function.name, [])
            # This declaration supersedes its own earlier registration on a
            # retained owner; the owner's fields stay, since only a
            # redeclaration of the type itself can replace those.
            same_named[:] = [
                member
                for member in same_named
                if not (member.kind == "method" and member.origin == "registered")
            ]
            same_named.append(_MemberDeclaration("method", function.span, module_id, "source"))
    return index


class _ExceptionOwners:
    """Exception owners declaring one kind of member under one key, nested by ancestry.

    Sorted by pre-order number, with each entry's nearest enclosing entry
    (the nearest listed ancestor), so the nearest listed ancestor of any
    exception is found without walking its base chain.
    """

    def __init__(self, nesting: ExceptionNesting, owners: list[DeclId]) -> None:
        def by_pre(owner: DeclId) -> int:
            return nesting.pre[owner]

        owners.sort(key=by_pre)
        self._nesting = nesting
        self._owners = owners
        self._pres = [nesting.pre[owner] for owner in owners]
        self._enclosing: list[int | None] = []
        open_entries: list[int] = []
        for position, owner in enumerate(owners):
            while open_entries and nesting.end[owners[open_entries[-1]]] <= self._pres[position]:
                open_entries.pop()
            self._enclosing.append(open_entries[-1] if open_entries else None)
            open_entries.append(position)

    def nearest_ancestor(self, owner_id: DeclId) -> DeclId | None:
        """The nearest strict ancestor of *owner_id* among the listed owners."""
        point = self._nesting.pre[owner_id]
        last_before = bisect_left(self._pres, point) - 1
        entry = last_before if last_before >= 0 else None
        while entry is not None and self._nesting.end[self._owners[entry]] <= point:
            entry = self._enclosing[entry]
        return None if entry is None else self._owners[entry]

    def first_descendant(self, owner_id: DeclId) -> DeclId | None:
        """The listed descendant of *owner_id* a breadth-first descent reaches first."""
        nesting = self._nesting
        start = bisect_left(self._pres, nesting.pre[owner_id] + 1)
        stop = bisect_left(self._pres, nesting.end[owner_id])

        def breadth_first(descendant: DeclId) -> tuple[int, int]:
            return nesting.depth[descendant], nesting.pre[descendant]

        return min(self._owners[start:stop], key=breadth_first, default=None)


class _ExceptionRelatives:
    """The ancestor-field and related-method rules of exceptions, indexed by member name.

    An exception's mates are its ancestors, nearest first, then its
    descendants, breadth-first. Every exception of the table is indexed once,
    so each rule is answered from the numbering of the exception hierarchy
    rather than by walking an owner's chain.
    """

    def __init__(self, index: _MemberIndex, type_table: TypeTable) -> None:
        self._index = index
        nesting = type_table.exception_nesting()
        fields: dict[str, list[DeclId]] = {}
        methods: dict[tuple[str, ModuleId], list[DeclId]] = {}
        for exception_id in nesting.pre:
            _index_registered_owner(index, type_table, exception_id)
            for name, members in index.members_of(exception_id).items():
                if any(member.kind == "field" for member in members):
                    fields.setdefault(name, []).append(exception_id)
                for module_id in {m.module_id for m in members if m.kind == "method"}:
                    methods.setdefault((name, module_id), []).append(exception_id)
        self._fields = {name: _ExceptionOwners(nesting, owners) for name, owners in fields.items()}
        self._methods = {key: _ExceptionOwners(nesting, owners) for key, owners in methods.items()}

    def ancestor_field(
        self, owner_id: DeclId, name: str
    ) -> tuple[DeclId, _MemberDeclaration] | None:
        """Find the nearest ancestor field named *name*."""
        owners = self._fields.get(name)
        ancestor = None if owners is None else owners.nearest_ancestor(owner_id)
        if ancestor is None:
            return None
        return ancestor, next(
            m for m in self._index.members_of(ancestor)[name] if m.kind == "field"
        )

    def related_method(
        self, owner_id: DeclId, name: str, module_id: ModuleId
    ) -> tuple[DeclId, _MemberDeclaration] | None:
        """Find a same-named method on an ancestor, else a descendant, declared by *module_id*."""
        owners = self._methods[name, module_id]  # the queried method itself is listed
        mate = owners.nearest_ancestor(owner_id)
        if mate is None:
            mate = owners.first_descendant(owner_id)
        if mate is None:
            return None
        member = next(
            m
            for m in self._index.members_of(mate)[name]
            if m.kind == "method" and m.module_id == module_id
        )
        return mate, member


class _Relatives:
    """The owners whose members an owner's methods may collide with, by selection level.

    An exception's relatives are its ancestors and descendants
    (:class:`_ExceptionRelatives`, built once on first need); a record's are
    its counted owning enums; an enum's, the member records it counts for
    (:meth:`TypeTable.owning_enum_defs_for_selection`, read in reverse).
    Only an exception has ancestors, hence inherited fields.
    """

    def __init__(self, index: _MemberIndex, type_table: TypeTable) -> None:
        self._index = index
        self._type_table = type_table
        self._exceptions: _ExceptionRelatives | None = None
        self._mates: dict[DeclId, tuple[DeclId, ...]] = {}

    def _exception_relatives(self) -> _ExceptionRelatives:
        if self._exceptions is None:
            self._exceptions = _ExceptionRelatives(self._index, self._type_table)
        return self._exceptions

    def _mates_of(self, owner_id: DeclId) -> tuple[DeclId, ...]:
        """The record or enum *owner_id*'s mates, indexed on first use."""
        mates = self._mates.get(owner_id)
        if mates is None:
            type_table = self._type_table
            typedef = type_table.typedef_of(owner_id)
            if typedef.kind == "record":
                mates = tuple(
                    enum_def.decl_node_id
                    for enum_def in type_table.owning_enum_defs_for_selection(owner_id)
                )
            else:
                mates = tuple(
                    member.decl_id
                    for member in typedef.members
                    if any(
                        enum_def.decl_node_id == owner_id
                        for enum_def in type_table.owning_enum_defs_for_selection(member.decl_id)
                    )
                )
            for mate in mates:
                _index_registered_owner(self._index, type_table, mate)
            self._mates[owner_id] = mates
        return mates

    def _is_exception(self, owner_id: DeclId) -> bool:
        return self._type_table.typedef_of(owner_id).kind == "exception"

    def ancestor_field(
        self, owner_id: DeclId, name: str
    ) -> tuple[DeclId, _MemberDeclaration] | None:
        """Find the nearest ancestor field named *name*."""
        if not self._is_exception(owner_id):
            return None
        return self._exception_relatives().ancestor_field(owner_id, name)

    def related_method(
        self, owner_id: DeclId, name: str, module_id: ModuleId
    ) -> tuple[DeclId, _MemberDeclaration] | None:
        """Find a same-named method declared by *module_id* on a relative of *owner_id*."""
        if self._is_exception(owner_id):
            return self._exception_relatives().related_method(owner_id, name, module_id)
        for mate in self._mates_of(owner_id):
            for member in self._index.members_of(mate).get(name, ()):
                if member.kind == "method" and member.module_id == module_id:
                    return mate, member
        return None


def _raise_collision(
    type_table: TypeTable,
    owner_id: DeclId,
    name: str,
    declared: _MemberDeclaration,
    conflicting_id: DeclId,
    conflicting: _MemberDeclaration,
    *,
    prefer_later: bool = False,
) -> None:
    """Report a member collision, preferring the later same-source declaration.

    A field/method clash on one owner (``owner_id == conflicting_id``) always
    prefers whichever of the two came later. A method pair between two
    related owners (``prefer_later``) does too, since either side may be the
    later declaration; there, a span-less side is also never preferred over
    a spanned one, so the report always lands on a locatable declaration. An
    ancestor field clash never swaps: the method stays ``declared``
    regardless of source order, since the field's owner is a different,
    unrelated type.
    """
    swap = (
        (prefer_later or owner_id == conflicting_id)
        and declared.span is not None
        and conflicting.span is not None
        and declared.span.source == conflicting.span.source
        and declared.span.start_offset < conflicting.span.start_offset
    ) or (prefer_later and declared.span is None and conflicting.span is not None)
    if swap:
        owner_id, conflicting_id = conflicting_id, owner_id
        declared, conflicting = conflicting, declared

    owner_typedef = type_table.typedef_of(owner_id)
    conflicting_typedef = type_table.typedef_of(conflicting_id)
    related = (
        ()
        if conflicting.span is None
        else ((f"{conflicting.kind} '{name}' is declared here", conflicting.span),)
    )
    raise AglTypeError(
        f"{declared.kind.capitalize()} '{qualified_decl_name(owner_typedef)}::{name}' conflicts "
        f"with {conflicting.kind} '{name}' of '{qualified_decl_name(conflicting_typedef)}'.",
        span=declared.span,
        related=related,
    )


def _module_visit_order(module_id: ModuleId, entry_id: ModuleId) -> tuple[bool, tuple[str, ...]]:
    """Sort key placing the program's entry module after every other module.

    A duplicate between a library declaration and the program's own is then
    reported at the program's own, more actionable, declaration.
    """
    return (module_id == entry_id, module_id.segments)


def is_bare_declaration(
    item: Item,
) -> TypeGuard[RecordDef | EnumDef | ExceptionDef | TypeAlias | FuncDef]:
    """Whether *item* is one of the AST kinds sharing the bare-declaration name space.

    A record/enum/exception/alias type and a def share one name space within
    the declaration's scope path (see :func:`is_builtin_bare_declaration`).
    """
    return isinstance(item, (RecordDef, EnumDef, ExceptionDef, TypeAlias, FuncDef))


def is_builtin_bare_declaration(
    item: Item,
) -> TypeGuard[RecordDef | EnumDef | ExceptionDef | TypeAlias | FuncDef]:
    """Whether *item* is a builtin type or builtin def sharing the bare-declaration namespace.

    A ``builtin`` type (record/enum/exception/alias) and a ``builtin def``
    share one name space within the declaration's scope path. A ``builtin
    var`` is neither, and is excluded. This lets a builtin method coexist with
    the root builtin it dispatches to while retaining one declaration per
    scoped host identity.
    """
    return is_bare_declaration(item) and item.is_builtin


def bare_declaration_scoped_name(item: Item) -> tuple[str, ...] | None:
    """Return *item*'s full scoped name (its scope path plus its own name).

    ``None`` when *item* is not one of the bare-declaration kinds
    (:func:`is_bare_declaration`).
    """
    if is_bare_declaration(item):
        return (*(segment.name for segment in item.scope_path), item.name)
    return None


def _builtin_bare_declarations(
    modules: Mapping[ModuleId, ModuleResolution],
    entry_id: ModuleId,
) -> list[tuple[ModuleId, tuple[str, ...], str, SourceSpan]]:
    """List every builtin type and builtin def declaration by scoped name.

    Modules are visited in :func:`_module_visit_order`, and each module's
    items in source order (named scope regions inlined), so a program with
    several duplicate declarations always reports the same one.
    """
    found: list[tuple[ModuleId, tuple[str, ...], str, SourceSpan]] = []
    for module_id in sorted(modules, key=partial(_module_visit_order, entry_id=entry_id)):
        resolved = modules[module_id]
        for item in static_items(resolved.program.body.items):
            if is_builtin_bare_declaration(item):
                path = tuple(segment.name for segment in item.scope_path)
                found.append((module_id, path, item.name, item.span))
    return found


def _entry_declared_scoped_names(resolved: ModuleResolution) -> frozenset[tuple[str, ...]]:
    """Return every scoped name the entry module itself declares, builtin or not.

    A REPL entry's own fresh declaration at a scoped name supersedes
    whatever a still-live earlier entry recorded there (see
    ``session_builtins`` below), whether or not either declaration is a
    ``builtin`` one.
    """
    return frozenset(
        name
        for item in static_items(resolved.program.body.items)
        if (name := bare_declaration_scoped_name(item)) is not None
    )


def validate_builtin_declaration_uniqueness(
    modules: Mapping[ModuleId, ModuleResolution],
    entry_id: ModuleId,
    session_builtins: SessionBuiltinDeclarations | None = None,
) -> None:
    """Reject a builtin name declared more than once at the same scope path.

    A ``builtin`` declaration marks what a declaration denotes at the host
    boundary. Its scoped name, rather than its bare spelling, is the identity:
    a root builtin and a method under a receiver type intentionally coexist.
    Types and defs still share one namespace at any one scoped name.

    ``session_builtins`` carries builtin identities a REPL session already
    holds from an earlier, still-live entry -- outside *modules*, this call's
    own module graph -- keyed by scoped name, so a later entry importing the
    same scoped builtin is rejected exactly as it would be declared in one
    module. Their declaring source is gone by the time a later entry runs, so
    the conflict is reported at the new declaration alone, without a
    "declared here" note. A name the entry module itself declares is excluded:
    that redeclaration supersedes the session's prior identity there rather
    than clashing with it.
    """
    entry_declared = (
        _entry_declared_scoped_names(modules[entry_id]) if session_builtins else frozenset()
    )
    first_seen: dict[tuple[str, ...], tuple[ModuleId, tuple[str, ...], SourceSpan | None]] = {
        key: (module_id, path, None)
        for key, (module_id, path) in (session_builtins or {}).items()
        if key not in entry_declared
    }
    for module_id, path, name, span in _builtin_bare_declarations(modules, entry_id):
        key = (*path, name)
        prior = first_seen.get(key)
        if prior is None:
            first_seen[key] = (module_id, path, span)
            continue
        prior_module_id, prior_path, prior_span = prior
        first_spelling = spell_declaration(prior_module_id, (*prior_path, name))
        duplicate_spelling = spell_declaration(module_id, (*path, name))
        related = (
            () if prior_span is None else ((f"'{first_spelling}' is declared here", prior_span),)
        )
        raise AglTypeError(
            f"Builtin '{name}' is declared more than once: as '{first_spelling}' and as "
            f"'{duplicate_spelling}'.",
            span=span,
            related=related,
        )


def validate_method_declaration_collisions(
    modules: Mapping[ModuleId, ModuleResolution], type_table: TypeTable
) -> None:
    """Reject field collisions and same-module, same-level method pairs.

    A method collides with a field of its owner or an ancestor regardless of
    module. A method also collides with a same-named, same-module method
    declared on a record and a current enum that declares or references it,
    or on an exception and one of its ancestors or descendants — the only
    pairs one selection level (see :meth:`TypeTable.method_candidates`) can
    join. Cross-module pairs stay legal; they are a call-site ambiguity
    instead.
    """
    index = _member_declarations(modules, type_table)
    relatives = _Relatives(index, type_table)

    # Order owners deterministically so a program reports one stable collision.
    sort_key = partial(decl_id_sort_key, type_table.defs)
    for owner_id in sorted(index.declared, key=sort_key):
        same_named_by_name = index.members[owner_id]
        for name, same_named_members in same_named_by_name.items():
            for method in (member for member in same_named_members if member.kind == "method"):
                field = next(
                    (member for member in same_named_members if member.kind == "field"), None
                )
                if field is not None:
                    _raise_collision(type_table, owner_id, name, method, owner_id, field)
                conflict = relatives.ancestor_field(owner_id, name)
                if conflict is not None:
                    _raise_collision(type_table, owner_id, name, method, *conflict)
                if method.origin == "registered":
                    # A retained declaration from an earlier compile unit:
                    # the pair check only starts from the new declaration's
                    # own owner, which the symmetric level-mate relation
                    # always reaches in turn.
                    continue
                pair = relatives.related_method(owner_id, name, method.module_id)
                if pair is not None:
                    _raise_collision(type_table, owner_id, name, method, *pair, prefer_later=True)
