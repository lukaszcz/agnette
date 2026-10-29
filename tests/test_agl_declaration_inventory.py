"""Source inventory reuse must not retain contextual validation results."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import replace

import pytest

from agnette.agl.modules.ids import ENTRY_ID, ModuleId
from agnette.agl.semantics.type_table import TypeTable
from agnette.agl.semantics.types import RecordType
from agnette.agl.syntax import nodes
from agnette.agl.syntax.nodes import Item
from agnette.agl.typecheck import declaration_validation as validation
from agnette.agl.typecheck.env import AglTypeError
from tests.agl.module_graph import resolve_and_check_entry, resolve_entry


@pytest.fixture
def source_walks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Item, ...]]:
    walks: list[tuple[Item, ...]] = []
    original = nodes.static_items

    def counted(items: tuple[Item, ...]) -> Iterator[Item]:
        walks.append(items)
        yield from original(items)

    monkeypatch.setattr(nodes, "static_items", counted)
    monkeypatch.setattr(validation, "static_items", counted)
    return walks


def test_repeated_validations_do_not_walk_unchanged_source(
    source_walks: list[tuple[Item, ...]],
) -> None:
    checked = resolve_and_check_entry(
        "record Point\n    x: int\n\nscope Point\n  def measure(self) -> int = self.x\nend Point\n",
        default_stdlib=False,
    )
    resolved = checked.resolved
    modules = {ENTRY_ID: resolved}
    source_walks.clear()
    for _ in range(3):
        validation.validate_builtin_declaration_uniqueness(modules, ENTRY_ID)
        validation.validate_method_declaration_collisions(modules, checked.type_env.type_table)
    assert not source_walks


def test_builtin_and_entry_names_share_one_source_walk(
    source_walks: list[tuple[Item, ...]],
) -> None:
    resolved = resolve_entry(
        "scope nested\n  builtin def host() -> unit\nend nested\n", default_stdlib=False
    )
    source_walks.clear()
    for _ in range(2):
        validation.validate_builtin_declaration_uniqueness(
            {ENTRY_ID: resolved}, ENTRY_ID, {("nested", "host"): (ModuleId(("old",)), ())}
        )
    assert sum(items is resolved.program.body.items for items in source_walks) == 1


def test_cached_members_still_check_current_enum_membership() -> None:
    checked = resolve_and_check_entry(
        "record Point\n    x: int\n"
        "record Other\n    y: int\n"
        "enum Shape = Other\n"
        "def Point::measure(self) -> int = 1\n"
        "def Shape::measure(self) -> int = 2\n",
        default_stdlib=False,
    )
    table = checked.type_env.type_table
    modules = {ENTRY_ID: checked.resolved}
    validation.validate_method_declaration_collisions(modules, table)
    shape = table.named(ENTRY_ID, "Shape")
    point = RecordType("Point", decl_id=table.named(ENTRY_ID, "Point").decl_node_id)
    changed = TypeTable()
    for typedef in table.defs.values():
        changed.register(replace(typedef, members=(point,)) if typedef is shape else typedef)
    with pytest.raises(AglTypeError):
        validation.validate_method_declaration_collisions(modules, changed)


def test_cached_builtin_inventory_still_checks_current_session() -> None:
    imported_id = ModuleId(("library",))
    imported = resolve_entry("builtin def host() -> unit\n", default_stdlib=False)
    entry = resolve_entry("", default_stdlib=False)
    modules = {ENTRY_ID: entry, imported_id: imported}
    validation.validate_builtin_declaration_uniqueness(modules, ENTRY_ID)
    with pytest.raises(AglTypeError) as error:
        validation.validate_builtin_declaration_uniqueness(
            modules, ENTRY_ID, {("host",): (ModuleId(("earlier",)), ())}
        )
    assert error.value.span is imported.program.body.items[0].span


def test_equal_syntax_does_not_reuse_another_sources_spans() -> None:
    first = resolve_entry("builtin def host() -> unit\n", default_stdlib=False)
    second = resolve_entry("\n\nbuiltin def host() -> unit\n", default_stdlib=False)
    assert first.program == second.program
    library_id = ModuleId(("library",))
    entry = resolve_entry("", default_stdlib=False)
    session: validation.SessionBuiltinDeclarations = {("host",): (ModuleId(("earlier",)), ())}
    for resolved in (first, second):
        with pytest.raises(AglTypeError) as error:
            validation.validate_builtin_declaration_uniqueness(
                {ENTRY_ID: entry, library_id: resolved}, ENTRY_ID, session
            )
        assert error.value.span is resolved.program.body.items[0].span


def test_cached_inventory_uses_current_receiver_classification() -> None:
    checked = resolve_and_check_entry(
        "record Point\n    x: int\n"
        "record Other\n    measure: int\n"
        "def Point::measure(self) -> int = 1\n",
        default_stdlib=False,
    )
    resolved = checked.resolved
    key = (ENTRY_ID, ("Point",), "measure")
    changed = replace(
        resolved,
        method_declarations={
            key: replace(resolved.method_declarations[key], scope_path=("Other",))
        },
    )
    with pytest.raises(AglTypeError):
        validation.validate_method_declaration_collisions(
            {ENTRY_ID: changed}, checked.type_env.type_table
        )


def test_inventory_eviction_only_repeats_source_collection(
    monkeypatch: pytest.MonkeyPatch, source_walks: list[tuple[Item, ...]]
) -> None:
    modules = [
        resolve_entry(f"builtin def host{index}() -> unit\n", default_stdlib=False)
        for index in range(3)
    ]
    monkeypatch.setattr(validation, "_INVENTORIES", OrderedDict())
    monkeypatch.setattr(validation, "_INVENTORY_CAPACITY", 2)
    source_walks.clear()
    for index in (0, 1, 0, 2, 1):
        validation.validate_builtin_declaration_uniqueness({ENTRY_ID: modules[index]}, ENTRY_ID)
    assert len(source_walks) == 4


def test_cached_inventory_does_not_fix_builtin_module_visit_order() -> None:
    library_id = ModuleId(("library",))
    first = resolve_entry("builtin def host() -> unit\n", default_stdlib=False)
    second = resolve_entry("\nbuiltin def host() -> unit\n", default_stdlib=False)
    modules = {ENTRY_ID: first, library_id: second}
    for entry_id, expected in ((ENTRY_ID, first), (library_id, second)):
        with pytest.raises(AglTypeError) as error:
            validation.validate_builtin_declaration_uniqueness(modules, entry_id)
        assert error.value.span is expected.program.body.items[0].span
