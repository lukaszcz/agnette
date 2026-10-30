"""Completed modules own stable interfaces; builders and new entries stay live."""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agnette.agl import artifact_serialization
from agnette.agl.artifact_cache import clear_retained_artifacts
from agnette.agl.modules.ids import ModuleId
from agnette.agl.repl import ReplSession
from agnette.agl.scope.program import ResolvedProgram, resolve_program
from agnette.agl.semantics.type_table import DeclKey
from agnette.agl.semantics.types import IntType, TextType, Type
from agnette.agl.semantics.values import IntValue, TextValue
from agnette.agl.typecheck.env import (
    CheckedModule,
    CheckedModuleImage,
    GenericAliasDef,
    GenericTypeDef,
    ModuleTypeInterface,
    TypeEnvironment,
)
from agnette.agl.typecheck.program import _prepare_program, check_program
from tests.agl.ir_harness import make_file_graph_from_files, write_module_file

_LIB_ID = ModuleId.from_path("lib")
_SOURCE = """\
record Box[T]
  value: T

type Erased[T] = int
type Items[T] = array[T]
type Count = int

enum Choice[T]
  | empty
  | erased(value: Erased[T])
  | boxed(value: Box[T])

scope Region
  record Marker
    value: Count
end Region

def Box::get[T](self) = self.value
"""


@dataclass(frozen=True)
class _Compiled:
    resolved: ResolvedProgram
    module: CheckedModule


@pytest.fixture
def compiled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Compiled:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    clear_retained_artifacts()
    graph = make_file_graph_from_files(
        tmp_path,
        {"entry": "import lib::*\n", "lib": _SOURCE},
        default_stdlib=True,
    )
    resolved = resolve_program(graph)
    return _Compiled(resolved, check_program(resolved).modules[_LIB_ID])


def _assert_immutable(interface: ModuleTypeInterface) -> None:
    for table in (interface.types, interface.generics, interface.aliases):
        assert table
        assert not isinstance(table, MutableMapping)
        assert getattr(table, "__setitem__", None) is None


def _clear_declaration_tables(env: TypeEnvironment) -> None:
    for table in (env._program_type_table, env._program_generic_table, env._program_alias_table):
        assert isinstance(table, dict)
        table.clear()


def test_completed_module_materializes_before_any_consumer_read(compiled: _Compiled) -> None:
    module = compiled.module
    _clear_declaration_tables(module.type_env)
    interface = module.interface
    assert interface.types[(_LIB_ID, (), "Count")] == IntType()
    assert interface.generics[(_LIB_ID, (), "Box")].type_params == ("T",)
    assert interface.aliases[(_LIB_ID, (), "Items")].type_params == ("T",)
    assert module.interface is interface
    assert replace(module, source_text="presentation only").interface is interface
    assert module.image().interface is interface


def test_completed_interface_owns_immutable_export_containers(compiled: _Compiled) -> None:
    interface = compiled.module.interface
    _assert_immutable(interface)
    assert {key[0] for key in interface.types} == {_LIB_ID}
    assert {definition.module_id for definition in interface.definitions} == {_LIB_ID}


def test_snapshot_includes_final_aliases_inline_arities_and_scoped_definitions(
    compiled: _Compiled,
) -> None:
    module = compiled.module
    interface = module.interface
    assert interface == module.type_env.module_interface()
    assert interface.types[(_LIB_ID, (), "Count")] == IntType()
    assert interface.aliases[(_LIB_ID, (), "Items")].type_params == ("T",)
    assert (_LIB_ID, ("Choice",), "erased") not in interface.generics
    definitions = {(td.scope_path, td.name): td for td in interface.definitions}
    assert definitions[(("Choice",), "erased")].type_params == ()
    assert definitions[(("Choice",), "boxed")].type_params == ("T",)
    assert definitions[(("Region",), "Marker")].fields == (("value", IntType()),)
    box = interface.generics[(_LIB_ID, (), "Box")].template
    assert definitions[((), "Box")].decl_node_id == box.decl_id


def test_mutating_an_invalid_checked_environment_cannot_rewrite_its_snapshot(
    compiled: _Compiled,
) -> None:
    """Sealed facts are a contract, not a transitive freeze of shared tables."""
    module = compiled.module
    interface = module.interface
    expected_types = dict(interface.types)
    expected_generics = dict(interface.generics)
    expected_aliases = dict(interface.aliases)
    expected_definitions = interface.definitions
    env = module.type_env
    _clear_declaration_tables(env)
    definition = next(td for td in interface.definitions if td.name == "Box")
    env.type_table.register(
        replace(definition, decl_node_id=10**80, fields=(("changed", TextType()),))
    )

    assert module.interface is interface
    assert dict(module.interface.types) == expected_types
    assert dict(module.interface.generics) == expected_generics
    assert dict(module.interface.aliases) == expected_aliases
    assert module.interface.definitions == expected_definitions
    assert env.module_interface() != interface


def test_builder_projection_remains_fresh_before_completion(compiled: _Compiled) -> None:
    source = compiled.module.interface
    types: dict[DeclKey, Type] = {}
    generics: dict[DeclKey, GenericTypeDef] = {}
    aliases: dict[DeclKey, GenericAliasDef] = {}
    env = TypeEnvironment(
        module_id=_LIB_ID,
        program_type_table=types,
        program_generic_table=generics,
        program_alias_table=aliases,
    )
    before = env.module_interface()
    types.update(source.types)
    generics.update(source.generics)
    aliases.update(source.aliases)
    for definition in source.definitions:
        env.type_table.register(definition)
    after = env.module_interface()

    assert not (before.types or before.generics or before.aliases or before.definitions)
    assert after == source
    assert after is not before
    _assert_immutable(after)


@pytest.mark.parametrize("disk", [False, True])
def test_image_reuses_its_immutable_interface_when_rehydrating(
    compiled: _Compiled, disk: bool
) -> None:
    image = compiled.module.image()
    if disk:
        key = b"checked-interface-owned-snapshot"
        artifact_serialization.save(key, "checked-interface-test", image)
        restored = artifact_serialization.load(key, "checked-interface-test")
        assert isinstance(restored, CheckedModuleImage)
    else:
        restored = image
    assert restored.interface == image.interface
    prepared = _prepare_program(compiled.resolved)

    module = restored.rehydrate(compiled.resolved.modules[_LIB_ID], prepared.module_envs[_LIB_ID])
    assert module.interface is restored.interface
    assert module.image().interface is restored.interface
    assert module.environment_facts == restored.environment_facts
    _assert_immutable(restored.interface)


def test_repl_supersession_keeps_old_nominal_values() -> None:
    session = ReplSession()
    session.open()
    try:
        assert session.eval_entry("record Item\n  value: int").ok
        assert session.eval_entry("let saved = Item(value = 7)").ok
        assert session.eval_entry("record Item\n  label: text").ok
        assert session.eval_entry("saved.value").value == IntValue(7)
        assert session.eval_entry('Item(label = "new").label').value == TextValue("new")
    finally:
        session.close()


@pytest.mark.parametrize("edit", ["module", "dependency"])
def test_source_and_dependency_edits_invalidate_the_owned_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: str
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    clear_retained_artifacts()
    modules = {
        "entry": "import lib::*\n",
        "lib": "import dep::*\ntype Item = Payload\n",
        "dep": "record Payload\n  value: int\n",
    }
    first_graph = make_file_graph_from_files(tmp_path, modules)
    first = check_program(resolve_program(first_graph)).modules[_LIB_ID]
    snapshot = first.interface
    if edit == "dependency":
        write_module_file(tmp_path / "root", "dep", "record Payload\n  value: text\n")
    else:
        write_module_file(tmp_path / "root", "lib", "import dep::*\ntype Item = text\n")
    second_graph = make_file_graph_from_files(tmp_path, modules, module_root=tmp_path / "root")
    second = check_program(resolve_program(second_graph)).modules[_LIB_ID]
    key = (_LIB_ID, (), "Item")
    assert second.resolved is not first.resolved
    assert second.interface.types[key] != snapshot.types[key]
    assert first.interface is snapshot
    assert second.interface is not snapshot
