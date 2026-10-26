"""Checking a module of many type declarations costs work linear in their number."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.scope.resolver import _Resolver
from agnette.agl.semantics.types import Type
from agnette.agl.typecheck.env import TypeEnvironment
from agnette.agm.cli_support.args import CheckArgs
from agnette.agm.commands import check as check_command
from tests.agl.instruction_counts import instructions_run_in


def _check(root: Path, name: str, text: str) -> None:
    entry = root / f"{name}.agl"
    entry.write_text(text + "def r() -> int = 1\n", encoding="utf-8")
    check_command.run(CheckArgs(files=[str(entry)]))


def test_the_type_namespace_is_listed_a_constant_number_of_times_per_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listings = 0
    real = TypeEnvironment.non_builtin_type_items

    def counting(self: TypeEnvironment) -> list[tuple[str, Type]]:
        nonlocal listings
        listings += 1
        return real(self)

    monkeypatch.setattr(TypeEnvironment, "non_builtin_type_items", counting)

    def work(records: int) -> int:
        nonlocal listings
        listings = 0
        _check(tmp_path, f"r{records}", "".join(f"record R{i}\n  a: int\n" for i in range(records)))
        return listings

    work(1)  # loads the standard library once, whichever test runs first
    assert work(80) == work(10)


def test_the_scope_layers_of_many_types_are_built_in_linear_work(tmp_path: Path) -> None:
    def work(records: int) -> int:
        return instructions_run_in(
            _Resolver._build_scope_nodes,
            lambda: _check(
                tmp_path, f"s{records}", "".join(f"record R{i}\n  a: int\n" for i in range(records))
            ),
        )

    work(1)  # loads the standard library once, whichever test runs first
    floor = work(1)
    assert work(160) - floor < 2.5 * (work(80) - floor)
