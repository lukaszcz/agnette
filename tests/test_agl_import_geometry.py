"""Immutable import surfaces survive settling; each reader gets current provenance."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from agnette.agl.diagnostics import HiddenMemberError
from agnette.agl.modules.ids import ModuleId
from agnette.agl.scope.imports import (
    ImportEnv,
    ImportWay,
    ItemDeclaration,
    ModuleContribution,
    NameAtom,
    PathAtom,
    QName,
    RouteSurface,
    ScopeOrigins,
    SingleTarget,
    WildcardTarget,
    build_import_env,
    contribution_routes,
    import_env_depth,
    qualifier_member_ways,
)
from agnette.agl.scope.program import resolve_program
from agnette.agl.syntax.nodes import ImportDecl, ImportItem, ScopeSegment
from tests._agl_helpers import dummy_span
from tests.agl.ir_harness import make_file_graph_from_files

_MODULE = ModuleId.from_path("pkg/lib")


def _decl(node_id: int = 1) -> ImportDecl:
    return ImportDecl(_MODULE.segments, False, None, None, (), dummy_span(), node_id)


def _item(*path: str, rename: str | None = None) -> ImportItem:
    return ImportItem(
        path[-1],
        rename,
        dummy_span(),
        10,
        tuple(ScopeSegment(part, dummy_span(), 11 + index) for index, part in enumerate(path[:-1])),
    )


def _build(
    decl: ImportDecl,
    exports: Mapping[NameAtom, QName],
    scopes: dict[NameAtom, ScopeOrigins] | None = None,
    withheld: dict[NameAtom, frozenset[QName]] | None = None,
    aliases: tuple[QName, ...] = (),
) -> ImportEnv:
    return build_import_env(
        (decl,),
        {decl.node_id: SingleTarget(_MODULE)},
        {_MODULE: exports},
        {_MODULE: scopes if scopes is not None else {}},
        aliases,
        {_MODULE: withheld if withheld is not None else {}},
    )


def test_new_environments_share_owned_immutable_members_but_not_import_ways() -> None:
    exports: dict[NameAtom, QName] = {"z": (_MODULE, "z"), "a": (_MODULE, "a")}
    scopes: dict[NameAtom, ScopeOrigins] = {}
    withheld: dict[NameAtom, frozenset[QName]] = {}
    first = _build(_decl(), exports, scopes, withheld)
    second = _build(replace(_decl(2), alias="renamed"), exports, scopes, withheld)
    left, right = first.contributions[_MODULE], second.contributions[_MODULE]
    assert left.members is right.members
    assert left.exports is right.exports
    assert list(right.members) == ["a", "z"]
    assert qualifier_member_ways(first, ("lib",), "a") == {
        (_MODULE, "a"): frozenset({ImportWay(1)})
    }
    assert qualifier_member_ways(second, ("lib",), "a") == {}
    assert qualifier_member_ways(second, ("renamed",), "a") == {
        (_MODULE, "a"): frozenset({ImportWay(2)})
    }
    assert not isinstance(right.members, MutableMapping)
    assert not isinstance(right.exports, MutableMapping)


@pytest.mark.parametrize("changed", ["exports", "scopes", "withheld"])
def test_equal_replacement_values_keep_the_current_export_identity(changed: str) -> None:
    exports: dict[NameAtom, QName] = {"Alias": (_MODULE, "Alias")}
    scopes: dict[NameAtom, ScopeOrigins] = {"S": frozenset({(_MODULE, "S")})}
    withheld: dict[NameAtom, frozenset[QName]] = {"Alias": frozenset({(_MODULE, "Secret")})}
    first = _build(_decl(), exports, scopes, withheld)
    # Copying the map is safe; replacing an equal value still needs a new snapshot.
    shared = _build(_decl(2), dict(exports), dict(scopes), dict(withheld))
    assert shared.contributions[_MODULE].members is first.contributions[_MODULE].members
    if changed == "exports":
        exports["Alias"] = (_MODULE, "".join(["Al", "ias"]))
    elif changed == "scopes":
        scopes["S"] = frozenset(list(scopes["S"]))
    else:
        withheld["Alias"] = frozenset(list(withheld["Alias"]))
    current = _build(_decl(3), exports, scopes, withheld)
    assert current.contributions[_MODULE].members is not first.contributions[_MODULE].members
    assert current.contributions[_MODULE].exports["Alias"] is exports["Alias"]
    assert current.scope_origins_by_route[(_MODULE, ("S",))] is scopes["S"]
    (way,) = current.contributions[_MODULE].routes[None].member_ways["Alias"]
    assert way.withheld is withheld["Alias"]


def test_many_unrelated_surfaces_leave_retained_environments_usable() -> None:
    modules = [ModuleId.from_path(f"lib{index}") for index in range(256)]
    exports: dict[ModuleId, dict[NameAtom, QName]] = {
        module: {"member": (module, "member")} for module in modules
    }
    scopes: dict[ModuleId, dict[NameAtom, ScopeOrigins]] = {module: {} for module in modules}
    decl = replace(_decl(), wildcard=True, tail=())
    env = build_import_env((decl,), {1: WildcardTarget(frozenset(modules))}, exports, scopes)
    assert env.unqualified["member"] == frozenset((module, "member") for module in modules)
    assert import_env_depth(env) == 1
    exports[modules[0]].clear()
    fresh = build_import_env((decl,), {1: SingleTarget(modules[0])}, exports, scopes)
    assert not fresh.unqualified
    assert env.contributions[modules[0]].members == {"member": (modules[0], "member")}


class _PausedSurface(Mapping[NameAtom, QName]):
    """Pause cache validation while another build changes the retained surfaces."""

    def __init__(self, members: Mapping[NameAtom, QName], barrier: Barrier) -> None:
        self.members = members
        self.barrier = barrier

    def __getitem__(self, key: NameAtom) -> QName:
        return self.members[key]

    def __iter__(self) -> Iterator[NameAtom]:
        return iter(self.members)

    def __len__(self) -> int:
        return len(self.members)

    def __eq__(self, other: object) -> bool:
        self.barrier.wait(timeout=30)
        self.barrier.wait(timeout=30)
        return self.members == other


@pytest.mark.parametrize("interference", ["eviction", "replacement"])
def test_concurrent_build_can_change_cache_during_surface_validation(interference: str) -> None:
    exports: dict[NameAtom, QName] = {"member": (_MODULE, "member")}
    first = _build(_decl(), exports)
    barrier = Barrier(2)
    paused = _PausedSurface(exports, barrier)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(_build, _decl(2), paused)
        barrier.wait(timeout=30)
        try:
            if interference == "eviction":
                modules = frozenset(
                    ModuleId.from_path(f"concurrent{index}") for index in range(256)
                )
                build_import_env(
                    (_decl(3),),
                    {3: WildcardTarget(modules)},
                    {module: {} for module in modules},
                    {module: {} for module in modules},
                )
            else:
                _build(_decl(3), {"new": (_MODULE, "new")})
        finally:
            barrier.wait(timeout=30)
        current = pending.result(timeout=30)
    assert current.contributions[_MODULE].members is first.contributions[_MODULE].members
    assert qualifier_member_ways(current, ("lib",), "member") == {
        (_MODULE, "member"): frozenset({ImportWay(2)})
    }
    latest = _build(_decl(4), {"new": (_MODULE, "new")})
    assert set(latest.contributions[_MODULE].members) == {"new"}


@pytest.mark.parametrize("table", ["unqualified", "bare"])
def test_direct_env_owns_nested_way_tables(table: str) -> None:
    qname = (_MODULE, "member")
    ways = frozenset({ImportWay(1)})
    origins: dict[QName, frozenset[ImportWay]] = {qname: ways}
    members: dict[NameAtom, Mapping[QName, frozenset[ImportWay]]] = {"member": origins}
    declarations = {1: members}
    env = ImportEnv(
        {},
        {},
        unqualified_ways=members if table == "unqualified" else {},
        decl_bare_ways=declarations if table == "bare" else {},
    )
    origins.clear()
    members.clear()
    members[("new", "deep")] = {}
    declarations.clear()
    retained = env.unqualified_ways if table == "unqualified" else env.decl_bare_ways[1]
    assert retained == {"member": {qname: ways}}
    assert not isinstance(retained, MutableMapping)
    assert not isinstance(retained["member"], MutableMapping)
    assert import_env_depth(env) == import_env_depth(replace(env)) == 1


def test_direct_env_owns_tail_exposure_tables_and_their_depth() -> None:
    path: PathAtom = ("Alias", "Deep", "member")
    item = ItemDeclaration(_MODULE, path, (_MODULE, "Alias"), path[1:], dummy_span())
    members: dict[NameAtom, frozenset[ItemDeclaration]] = {"short": frozenset({item})}
    declarations = {1: members}
    env = ImportEnv({}, {}, decl_tail_beneath=declarations)
    members.clear()
    declarations.clear()
    assert env.decl_tail_beneath == {1: {"short": frozenset({item})}}
    assert not isinstance(env.decl_tail_beneath, MutableMapping)
    assert not isinstance(env.decl_tail_beneath[1], MutableMapping)
    assert import_env_depth(env) == import_env_depth(replace(env)) == 3


def test_contribution_owns_route_members_and_reader_provenance() -> None:
    qname = (_MODULE, "member")
    members: dict[NameAtom, QName] = {"member": qname}
    ways = frozenset({ImportWay(1)})
    member_ways: dict[NameAtom, frozenset[ImportWay]] = {"member": ways}
    routes: dict[str | None, RouteSurface] = {None: RouteSurface(members, member_ways=member_ways)}
    contribution = ModuleContribution(_MODULE, members, True, frozenset(), routes, members)
    env = ImportEnv({_MODULE: contribution}, {})
    members.clear()
    member_ways.clear()
    routes.clear()
    assert qualifier_member_ways(env, ("lib",), "member") == {qname: ways}
    assert contribution.exports == {"member": qname}
    assert not isinstance(contribution.routes[None].member_ways, MutableMapping)
    assert import_env_depth(env) == import_env_depth(replace(env)) == 1


def test_same_mapping_identity_mutation_and_replacement_leave_old_exports_frozen() -> None:
    exports: dict[NameAtom, QName] = {"old": (_MODULE, "old")}
    scopes: dict[NameAtom, ScopeOrigins] = {}
    withheld: dict[NameAtom, frozenset[QName]] = {}
    first = _build(replace(_decl(), tail=()), exports, scopes, withheld)
    exports.clear()
    exports[("Deep", "New")] = (_MODULE, ("Deep", "New"))
    second = _build(replace(_decl(2), tail=()), exports, scopes, withheld)
    third = _build(replace(_decl(3), tail=()), dict(exports), scopes, withheld)
    assert first.contributions[_MODULE].exports == {"old": (_MODULE, "old")}
    assert set(second.unqualified) == set(third.unqualified) == {("Deep", "New")}
    assert import_env_depth(first) == 1
    assert import_env_depth(second) == import_env_depth(third) == 2


def test_mutable_scope_and_withholding_maps_are_validated_by_content() -> None:
    exports: dict[NameAtom, QName] = {"Alias": (_MODULE, "Alias")}
    scopes: dict[NameAtom, ScopeOrigins] = {"S": frozenset({(_MODULE, "S")})}
    withheld: dict[NameAtom, frozenset[QName]] = {}
    first = _build(replace(_decl(), tail=()), exports, scopes, withheld)
    scopes.clear()
    scopes[("New", "Scope")] = frozenset({(_MODULE, ("Origin", "Deep", "Scope"))})
    removed = (_MODULE, ("Vanished", "Leaf"))
    withheld[("Vanished", "Leaf")] = frozenset({removed})
    withheld["Alias"] = frozenset({(_MODULE, "Secret")})
    second = _build(replace(_decl(2), tail=()), exports, scopes, withheld)
    route = second.contributions[_MODULE].routes[None]
    assert route.scope_paths == frozenset({("New", "Scope")})
    assert first.contributions[_MODULE].routes[None].scope_paths == frozenset({"S"})
    assert route.member_ways[("Vanished", "Leaf")] == frozenset(
        {ImportWay(2, frozenset({removed, (_MODULE, "Vanished")}))}
    )
    assert route.member_ways["Alias"] == frozenset({ImportWay(2, withheld["Alias"])})
    assert import_env_depth(second) == 3
    assert import_env_depth(first) == 1


def test_tail_alias_identity_regions_renames_and_error_spans_are_current() -> None:
    alias = (_MODULE, "Alias")
    exports: dict[NameAtom, QName] = {"Alias": alias}
    scopes: dict[NameAtom, ScopeOrigins] = {}
    withheld: dict[NameAtom, frozenset[QName]] = {}
    item = _item("Alias", "Deep", "Member", rename="short")
    first_decl = replace(_decl(), tail=(item,), hidden=(item,))
    first = _build(first_decl, exports, scopes, withheld, (alias,))
    span = replace(dummy_span(), start_line=23, end_line=23)
    second_decl = replace(
        first_decl,
        node_id=2,
        span=span,
        scope_path=(ScopeSegment("Region", span, 44),),
    )
    second = _build(second_decl, exports, scopes, withheld, (alias,))
    assert set(second.decl_tail_beneath[2]) == {("Alias", "Deep", "Member"), "short"}
    assert next(iter(second.decl_tail_beneath[2]["short"])).span == span
    assert second.decl_hiding[2][0].span == span
    assert first.decl_hiding[1][0].span == first_decl.span
    assert import_env_depth(first) == import_env_depth(second) == 3
    no_alias = _build(second_decl, exports, scopes, withheld)
    assert not no_alias.decl_tail_beneath
    assert not no_alias.decl_hiding
    assert import_env_depth(no_alias) == 1


@pytest.mark.parametrize("path_enabled", [False, True])
def test_contribution_route_paths_agree_with_current_qualifier_reads(path_enabled: bool) -> None:
    decls: tuple[ImportDecl, ...] = (replace(_decl(), alias="local"),)
    if path_enabled:
        decls += (_decl(2),)
    env = build_import_env(
        decls,
        {decl.node_id: SingleTarget(_MODULE) for decl in decls},
        {_MODULE: {"value": (_MODULE, "value")}},
        {_MODULE: {}},
    )
    routes = contribution_routes(env.contributions[_MODULE])
    assert routes == (
        (
            (("local",), False),
            (_MODULE.segments, False),
            (("lib",), False),
            (_MODULE.segments, True),
        )
        if path_enabled
        else ((("local",), False),)
    )
    for path, anchored in routes:
        assert set(qualifier_member_ways(env, path, "value", anchored=anchored)) == {
            (_MODULE, "value")
        }


def test_scoped_tails_and_wildcard_expansion_do_not_reuse_previous_overlay() -> None:
    other = ModuleId.from_path("pkg/other")
    exports: dict[ModuleId, dict[NameAtom, QName]] = {
        _MODULE: {("S", "x"): (_MODULE, "x")},
        other: {"y": (other, "y")},
    }
    scopes: dict[ModuleId, dict[NameAtom, ScopeOrigins]] = {_MODULE: {}, other: {}}
    root = replace(_decl(), tail=(_item("S", rename="Renamed"),))
    region = replace(
        root,
        node_id=2,
        tail=(),
        scope_path=(ScopeSegment("Region", dummy_span(), 4),),
    )
    first = build_import_env((root,), {1: WildcardTarget(frozenset({_MODULE}))}, exports, scopes)
    second = build_import_env(
        (region,), {2: WildcardTarget(frozenset({_MODULE, other}))}, exports, scopes
    )
    assert ("Renamed", "x") in first.unqualified
    assert not second.unqualified
    assert set(second.decl_bare_ways[2]) == {("S", "x"), "y"}
    assert import_env_depth(first) == import_env_depth(second) == 2
    empty = _build(_decl(3), {})
    assert import_env_depth(empty) == 0


@pytest.mark.parametrize("table", ["bare", "routes", "scope", "beneath", "origins", "surface"])
def test_direct_import_env_depth_includes_independent_path_tables(table: str) -> None:
    path: PathAtom = ("a", "b", "c", "d")
    qname = (_MODULE, "Alias")
    item = ItemDeclaration(_MODULE, path, qname, path[1:], dummy_span())
    contribution = ModuleContribution(
        _MODULE, {}, True, frozenset(), {None: RouteSurface(scope_paths=frozenset({path}))}
    )
    env = ImportEnv(
        contributions={_MODULE: contribution} if table == "surface" else {},
        unqualified={},
        decl_bare_ways={1: {path: {qname: frozenset({ImportWay(1)})}}} if table == "bare" else {},
        decl_bare_routes={1: {path: frozenset({(_MODULE, path)})}} if table == "routes" else {},
        decl_scope_routes={1: {path: frozenset({(_MODULE, path)})}} if table == "scope" else {},
        decl_tail_beneath={1: {"short": frozenset({item})}} if table == "beneath" else {},
        scope_origins_by_route={(_MODULE, ("S",)): frozenset({(_MODULE, path)})}
        if table == "origins"
        else {},
    )
    assert import_env_depth(env) == 4
    assert import_env_depth(replace(env)) == 4


def test_scoped_reexport_hiding_and_root_replacement_are_observable(tmp_path: Path) -> None:
    for index, member in enumerate(("left", "right")):
        files = {
            "entry": f"import facade::*\nlet selected = Public::Owner::{member}\n",
            "facade": "scope Public\n  export lib hiding Owner::secret\nend Public\n",
            "lib": f"enum Owner\n  | {member}\n  | secret\n",
        }
        root = tmp_path / str(index)
        resolved = resolve_program(make_file_graph_from_files(root, files))
        assert resolved.modules
        files["entry"] = "import facade::*\nlet selected = Public::Owner::secret\n"
        with pytest.raises(HiddenMemberError):
            resolve_program(make_file_graph_from_files(root, files))
