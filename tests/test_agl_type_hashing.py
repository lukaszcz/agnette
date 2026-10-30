"""Semantic type keys retain value identity across reconstruction and table ownership."""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from itertools import product
from pathlib import Path
from typing import cast

import pytest

from agnette.agl.modules.ids import ENTRY_ID, ModuleId
from agnette.agl.semantics.type_table import TypeDef, TypeTable
from agnette.agl.semantics.types import (
    ArrayType,
    BoolType,
    BottomType,
    DecimalType,
    DictType,
    EnumType,
    ExceptionType,
    FunctionType,
    InferenceVarType,
    IntType,
    JsonType,
    RecordType,
    TextType,
    Type,
    TypeVarType,
    UnitType,
)

_PRIMITIVES = (TextType, JsonType, BoolType, IntType, DecimalType, UnitType, BottomType)


class _OtherText(TextType):
    """An inherited descriptor is not the exact built-in type."""


def _keys() -> list[Type]:
    primitives: list[Type] = [constructor() for constructor in _PRIMITIVES]
    pairs = list(product(primitives, repeat=2))
    return [
        *primitives,
        *(ArrayType(value) for value in primitives),
        *(DictType(left, ArrayType(right)) for left, right in pairs),
        *(FunctionType((ArrayType(left), right), left) for left, right in pairs),
        *(RecordType("Box", (left, right), decl_id=901001) for left, right in pairs),
        *(EnumType("Choice", (left, right), decl_id=901002) for left, right in pairs),
        ExceptionType("Failure", decl_id=901003),
        TypeVarType("T"),
    ]


def test_reconstructed_type_keys_keep_separate_dictionary_entries() -> None:
    keys = _keys()
    values = dict(zip(keys, range(len(keys)), strict=True))
    assert len(values) == len(keys)
    for index, original in enumerate(keys):
        rebuilt = cast(Type, pickle.loads(pickle.dumps(original)))
        assert type(rebuilt) is type(original)
        assert rebuilt == original
        assert hash(rebuilt) == hash(original)
        assert values[rebuilt] == index
        values[rebuilt] = -index
        assert values[original] == -index
    assert len(values) == len(keys)


def test_pickled_type_key_dictionary_accepts_fresh_equal_keys() -> None:
    original = {key: index for index, key in enumerate(_keys())}
    rebuilt = cast(dict[Type, int], pickle.loads(pickle.dumps(original)))
    assert rebuilt == original
    for key, index in original.items():
        assert rebuilt[key] == index


def test_pickled_dictionary_rebuilds_keys_under_a_different_hash_seed(tmp_path: Path) -> None:
    script = """
import pickle
import sys
from pathlib import Path
from tests.test_agl_type_hashing import _keys

path = Path(sys.argv[1])
fresh = {key: index for index, key in enumerate(_keys())}
if sys.argv[2] == "write":
    path.write_bytes(pickle.dumps(fresh))
else:
    restored = pickle.loads(path.read_bytes())
    assert restored == fresh
    for key, index in fresh.items():
        assert restored[key] == index
        assert hash(key) == hash(pickle.loads(pickle.dumps(key)))
"""
    payload = tmp_path / "types.pickle"
    for seed, operation in (("1", "write"), ("927", "read")):
        subprocess.run(
            [sys.executable, "-c", script, str(payload), operation],
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )


@pytest.mark.parametrize("constructor", _PRIMITIVES)
def test_empty_primitive_hash_is_discriminated_by_its_exact_class(
    constructor: Callable[[], Type],
) -> None:
    assert hash(constructor()) == hash(constructor)


def test_empty_primitives_use_exact_type_equality() -> None:
    for left, right in product(_PRIMITIVES, repeat=2):
        assert (left() == right()) is (left is right)
    assert TextType() != _OtherText()
    assert _OtherText() != TextType()
    assert _OtherText() == _OtherText()
    assert hash(_OtherText()) == hash(_OtherText())


@pytest.mark.parametrize("constructor", [RecordType, EnumType])
def test_nominal_keys_preserve_current_owner_and_type_arguments(
    constructor: type[RecordType] | type[EnumType],
) -> None:
    original = constructor("Box", (TextType(),), decl_id=901010)
    variants = [
        original,
        replace(original, decl_id=901011),
        replace(original, module_id=ModuleId(("other",))),
        replace(original, scope_path=("Nested",)),
        replace(original, name="Other"),
        replace(original, type_args=(IntType(),)),
    ]
    values = {key: index for index, key in enumerate(variants)}
    assert len(values) == len(variants)
    for index, key in enumerate(variants):
        assert values[replace(key)] == index
        assert hash(replace(key)) == hash(key)


def test_exception_and_inference_variable_keys_remain_distinct() -> None:
    exception = ExceptionType("Failure", decl_id=901020)
    variants: list[Type] = [
        exception,
        replace(exception, decl_id=901021),
        replace(exception, module_id=ModuleId(("other",))),
        replace(exception, scope_path=("Nested",)),
        replace(exception, name="Other"),
        InferenceVarType(),
        InferenceVarType(),
    ]
    values = {key: index for index, key in enumerate(variants)}
    assert len(values) == len(variants)
    rebuilt = cast(dict[Type, int], pickle.loads(pickle.dumps(values)))
    assert rebuilt == values


def test_type_key_fields_cannot_change_after_dictionary_insertion() -> None:
    array = ArrayType(TextType())
    nominal = RecordType("Box", (array,), decl_id=901030)
    values: dict[Type, str] = {array: "array", nominal: "nominal"}
    with pytest.raises(FrozenInstanceError):
        setattr(array, "elem", IntType())
    with pytest.raises(FrozenInstanceError):
        setattr(nominal, "decl_id", 901031)
    assert values[ArrayType(TextType())] == "array"
    assert values[replace(nominal)] == "nominal"


def test_generic_enum_keys_preserve_members_and_current_owner() -> None:
    member = RecordType("Member", (TypeVarType("T"),), decl_id=901050)
    definition = TypeDef(
        kind="enum",
        name="Choice",
        module_id=ENTRY_ID,
        decl_node_id=901051,
        type_params=("T",),
        members=(member,),
    )
    table = TypeTable()
    table.register(definition)
    old_owner = [
        EnumType("Choice", (constructor(),), decl_id=901051) for constructor in _PRIMITIVES
    ]
    for key in old_owner:
        expected = (replace(member, type_args=key.type_args),)
        assert table.enum_members(key) == expected
        restored = cast(EnumType, pickle.loads(pickle.dumps(key)))
        assert table.enum_members(restored) == expected

    table.register(replace(definition, decl_node_id=901052, members=()))
    for key in old_owner:
        assert table.enum_members(replace(key, decl_id=901052)) == ()
        assert table.enum_members(key) == (replace(member, type_args=key.type_args),)


def test_generic_key_lookups_remain_isolated_between_type_tables() -> None:
    parameter = TypeVarType("T")
    definition = TypeDef(
        kind="record",
        name="Box",
        module_id=ENTRY_ID,
        decl_node_id=901040,
        type_params=("T",),
        fields=(("value", parameter),),
    )
    original = TypeTable()
    changed = TypeTable()
    original.register(definition)
    changed.register(replace(definition, fields=(("value", ArrayType(parameter)),)))
    for constructor in _PRIMITIVES:
        handle = RecordType("Box", (constructor(),), decl_id=901040)
        assert original.record_fields(handle) == {"value": constructor()}
        assert changed.record_fields(replace(handle)) == {"value": ArrayType(constructor())}
        assert original.record_fields(replace(handle)) == {"value": constructor()}

    later = replace(definition, decl_node_id=901041, fields=(("later", parameter),))
    original.register(later)
    assert original.record_fields(RecordType("Box", (TextType(),), decl_id=901040)) == {
        "value": TextType()
    }
    assert original.record_fields(RecordType("Box", (TextType(),), decl_id=901041)) == {
        "later": TextType()
    }
