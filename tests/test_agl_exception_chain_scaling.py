"""A chain of exception declarations costs work linear in its length."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agnette.agl import type_schema
from agnette.agl.ir.contracts import EncodePlan
from agnette.agl.semantics.external_names import ExternalName
from agnette.agl.semantics.type_table import TypeDef, TypeTable
from agnette.agl.semantics.types import Type
from agnette.agm.cli_support.args import CheckArgs
from agnette.agm.commands import check as check_command


def _chain_program(length: int, fields: bool, methods: bool = False) -> str:
    body = "".join(
        f"exception E{i} extends E{i - 1}\n"
        + (f"  f{i}: int\n" if fields else "")
        + (f"def E{i}::m{i}(self) -> int = {i}\n" if methods else "")
        for i in range(1, length)
    )
    return "exception E0\n" + body + "def r() -> int = 1\n"


@pytest.fixture
def base_reads(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Count the reads of any declaration's ``base`` link."""
    reads = 0
    real = TypeDef.__getattribute__

    def counting(self: TypeDef, name: str) -> object:
        nonlocal reads
        reads += name == "base"
        return real(self, name)

    monkeypatch.setattr(TypeDef, "__getattribute__", counting)

    def read_and_reset() -> int:
        nonlocal reads
        count, reads = reads, 0
        return count

    return read_and_reset


@pytest.mark.parametrize("fields", [False, True], ids=("fieldless", "with-fields"))
def test_the_base_links_read_to_check_a_chain_grow_linearly_with_its_length(
    tmp_path: Path, base_reads: Callable[[], int], fields: bool
) -> None:
    def work(length: int) -> int:
        entry = tmp_path / f"chain{length}.agl"
        entry.write_text(_chain_program(length, fields), encoding="utf-8")
        base_reads()
        check_command.run(CheckArgs(files=[str(entry)]))
        return base_reads()

    assert work(120) < 2.5 * work(60)


def test_the_declaration_reads_to_check_a_fieldless_chain_grow_linearly_with_its_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = 0
    real = TypeDef.__getattribute__

    def counting(self: TypeDef, name: str) -> object:
        nonlocal reads
        reads += 1
        return real(self, name)

    def work(length: int) -> int:
        nonlocal reads
        entry = tmp_path / f"fieldless{length}.agl"
        entry.write_text(_chain_program(length, fields=False), encoding="utf-8")
        reads = 0
        with monkeypatch.context() as patch:
            patch.setattr(TypeDef, "__getattribute__", counting)
            check_command.run(CheckArgs(files=[str(entry)]))
        return reads

    work(2)  # loads the standard library once, whichever test runs first
    floor = work(2)  # what checking any program reads, whatever its chain
    assert work(240) - floor < 2.5 * (work(120) - floor)


def test_no_ancestor_list_is_built_to_check_a_chain_that_declares_no_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = 0
    real = TypeTable.ancestor_defs

    def counting(self: TypeTable, decl_id: int) -> tuple[TypeDef, ...]:
        nonlocal listed
        ancestors = real(self, decl_id)
        listed += len(ancestors)
        return ancestors

    monkeypatch.setattr(TypeTable, "ancestor_defs", counting)

    def work(length: int) -> int:
        nonlocal listed
        entry = tmp_path / f"methodless{length}.agl"
        entry.write_text(_chain_program(length, fields=True), encoding="utf-8")
        listed = 0
        check_command.run(CheckArgs(files=[str(entry)]))
        return listed

    assert work(120) <= work(60)


def test_the_declaration_reads_to_check_a_chain_of_methods_grow_linearly_with_its_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = 0
    real = TypeDef.__getattribute__

    def counting(self: TypeDef, name: str) -> object:
        nonlocal reads
        reads += 1
        return real(self, name)

    def work(length: int) -> int:
        nonlocal reads
        entry = tmp_path / f"methods{length}.agl"
        entry.write_text(_chain_program(length, fields=False, methods=True), encoding="utf-8")
        reads = 0
        with monkeypatch.context() as patch:
            patch.setattr(TypeDef, "__getattribute__", counting)
            check_command.run(CheckArgs(files=[str(entry)]))
        return reads

    work(2)  # loads the standard library once, whichever test runs first
    floor = work(2)  # what checking any program reads, whatever its chain
    assert work(240) - floor < 2.5 * (work(120) - floor)


def _counted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, install: Callable[..., None]
) -> Callable[[int], int]:
    """Return a function counting what *install* counts over checking a fielded chain."""
    count = 0

    def tick(amount: int = 1) -> None:
        nonlocal count
        count += amount

    def work(length: int) -> int:
        nonlocal count
        entry = tmp_path / f"counted{length}.agl"
        entry.write_text(_chain_program(length, fields=True), encoding="utf-8")
        count = 0
        with monkeypatch.context() as patch:
            install(patch, tick)
            check_command.run(CheckArgs(files=[str(entry)]))
        return count

    return work


def _assert_linear(work: Callable[[int], int]) -> None:
    work(2)  # loads the standard library once, whichever test runs first
    floor = work(2)  # what checking any program counts, whatever its chain
    assert work(240) - floor <= 2.5 * (work(120) - floor)


def test_encode_plans_compiled_for_a_chain_of_fielded_exceptions_grow_linearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def install(patch: pytest.MonkeyPatch, tick: Callable[..., None]) -> None:
        real = type_schema.build_encode_plan

        def counting(field_type: Type, type_table: TypeTable) -> EncodePlan:
            tick()
            return real(field_type, type_table)

        patch.setattr(type_schema, "build_encode_plan", counting)

    _assert_linear(_counted(monkeypatch, tmp_path, install))


def test_chain_links_walked_to_describe_a_chain_of_fielded_exceptions_grow_linearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def install(patch: pytest.MonkeyPatch, tick: Callable[..., None]) -> None:
        real = TypeTable.exception_field_defs

        def counting(self: TypeTable, decl_id: int) -> tuple[TypeDef, ...]:
            links = real(self, decl_id)
            tick(len(links))
            return links

        patch.setattr(TypeTable, "exception_field_defs", counting)

    _assert_linear(_counted(monkeypatch, tmp_path, install))


def test_inherited_external_names_read_to_check_a_chain_of_fielded_exceptions_grow_linearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def install(patch: pytest.MonkeyPatch, tick: Callable[..., None]) -> None:
        real = ExternalName.json

        def counting(self: ExternalName, declared: str) -> str:
            tick()
            return real(self, declared)

        patch.setattr(ExternalName, "json", counting)

    _assert_linear(_counted(monkeypatch, tmp_path, install))
