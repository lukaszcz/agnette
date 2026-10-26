"""A chain of exception declarations costs work linear in its length."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agnette.agl.semantics.type_table import TypeDef
from agnette.agm.cli_support.args import CheckArgs
from agnette.agm.commands import check as check_command


def _chain_program(length: int, fields: bool) -> str:
    body = "".join(
        f"exception E{i} extends E{i - 1}\n" + (f"  f{i}: int\n" if fields else "")
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
