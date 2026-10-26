"""Reading a module's ``use`` items does work linear in their number."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agnette.agl.scope.lookup import Candidate, LookupKind
from agnette.agl.scope.symbols import ScopePath
from agnette.agl.scope.uses import UseReader
from agnette.agl.syntax.nodes import UseDecl
from agnette.agm.cli_support.args import CheckArgs
from agnette.agm.commands import check as check_command


def _write_program(root: Path, uses: int, form: str, callee: str) -> Path:
    (root / "lib.agl").write_text(
        "".join(f"def f{i}() -> int = {i}\n" for i in range(uses)), encoding="utf-8"
    )
    entry = root / "main.agl"
    entry.write_text(
        "import lib\n"
        + "".join(form.format(i=i) + "\n" for i in range(uses))
        + f"def r() -> int = {callee}{uses - 1}()\n",
        encoding="utf-8",
    )
    return entry


@pytest.mark.parametrize(
    ("form", "callee"),
    [
        ("use lib::f{i}", "f"),
        ("use lib::f{i} as g{i}", "g"),
        ("use lib::{{f{i} as g{i}}}", "g"),
    ],
    ids=("plain", "renamed", "tail"),
)
def test_the_exposures_read_per_use_item_do_not_grow_with_their_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str, callee: str
) -> None:
    exposure = UseReader.exposure
    reads = 0

    def counting(
        self: UseReader, site: ScopePath, decl: UseDecl, relative: ScopePath, kind: LookupKind
    ) -> Iterator[Candidate]:
        nonlocal reads
        reads += 1
        return exposure(self, site, decl, relative, kind)

    monkeypatch.setattr(UseReader, "exposure", counting)

    def work(uses: int) -> int:
        nonlocal reads
        reads = 0
        root = tmp_path / str(uses)
        root.mkdir()
        entry = _write_program(root, uses, form, callee)
        check_command.run(CheckArgs(files=[str(entry)]))
        return reads

    assert work(80) < 2.5 * work(40)
