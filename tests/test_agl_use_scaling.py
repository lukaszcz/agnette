"""Reading a module's ``use`` items does work linear in their number."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agnette.agl.scope.lookup import Candidate, LookupKind
from agnette.agl.scope.symbols import ScopeNode, ScopePath
from agnette.agl.scope.uses import UseReader, _UseIndex
from agnette.agl.syntax.nodes import ScopeSegment, UseDecl
from agnette.agl.syntax.spans import SourceSpan
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


def _use(node_id: int, *names: str) -> UseDecl:
    span = SourceSpan(1, 1, 1, 1, 0, 0)
    return UseDecl(
        anchored=False,
        target=tuple(ScopeSegment(name, span, node_id) for name in names),
        tail=None,
        hidden=(),
        alias=None,
        span=span,
        node_id=node_id,
    )


def test_a_use_index_follows_its_layer_when_the_uses_are_replaced() -> None:
    layer = ScopeNode(node_id=0)
    layer.uses.append(_use(1, "m", "a"))
    index = _UseIndex(layer)
    assert [use.node_id for use in index.matching("a")] == [1]
    layer.uses = [_use(2, "m", "b")]
    assert index.matching("a") == []
    assert [use.node_id for use in index.matching("b")] == [2]


@pytest.mark.parametrize("shape", ["distinct", "repeated"])
def test_reading_many_glob_uses_does_not_multiply_the_work_per_extra_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    path_reads = UseReader._use_path_reached
    reads = 0

    def counting(self: UseReader, *args: object, **kwargs: object) -> object:
        nonlocal reads
        reads += 1
        return path_reads(self, *args, **kwargs)

    def work(globs: int) -> int:
        nonlocal reads
        root = tmp_path / f"{shape}{globs}"
        root.mkdir()
        modules = globs if shape == "distinct" else 1
        for i in range(modules):
            (root / f"m{i}.agl").write_text(f"def f{i}() -> int = {i}\n", encoding="utf-8")
        if shape == "distinct":
            text = "".join(f"import m{i}\n" for i in range(globs))
            text += "".join(f"use m{i}::*\n" for i in range(globs))
        else:
            text = "import m0\n" + "use m0::*\n" * globs
        entry = root / "main.agl"
        entry.write_text(text + "def r() -> int = f0()\n", encoding="utf-8")
        reads = 0
        check_command.run(CheckArgs(files=[str(entry)]))
        return reads

    monkeypatch.setattr(UseReader, "_use_path_reached", counting)
    work(1)
    small, large = work(6), work(12)
    # Doubling the globs at most multiplies the reads by 2^3 (distinct) or 2^2 (repeated).
    assert large < (12 if shape == "distinct" else 6) * small
