"""A chain of exception declarations costs work linear in its length."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agnette.agl.semantics.type_table import TypeDef, TypeTable
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
