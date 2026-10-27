"""Reading a module's ``use`` items does work linear in their number."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agnette.agl.pipeline import PipelineDriver
from agnette.agl.scope.lookup import Candidate, LookupKind
from agnette.agl.scope.symbols import ScopePath
from agnette.agl.scope.uses import UseReader
from agnette.agl.syntax.nodes import UseDecl
from agnette.agm.cli_support.args import CheckArgs
from agnette.agm.commands import check as check_command
from tests._agl_helpers import agl_roots, repl_session_with_root, run_inline_code


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


@pytest.mark.parametrize("shape", ["distinct", "repeated"])
def test_reading_many_glob_uses_does_not_multiply_the_work_per_extra_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    reads = {"path": 0, "exposure": 0}

    def counting(name: str, key: str) -> None:
        original = getattr(UseReader, name)

        def wrapper(self: UseReader, *args: Any, **kwargs: Any) -> Any:
            reads[key] += 1
            return original(self, *args, **kwargs)

        monkeypatch.setattr(UseReader, name, wrapper)

    counting("_use_path_reached", "path")
    counting("exposure", "exposure")

    def work(globs: int) -> dict[str, int]:
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
        for key in reads:
            reads[key] = 0
        check_command.run(CheckArgs(files=[str(entry)]))
        return dict(reads)

    work(1)
    small, large = work(10), work(20)
    # Quadratic work at most: doubling the globs multiplies it by under 2^2.5.
    for key in reads:
        assert large[key] < 5.6 * small[key], key


def _glob_program(root: Path, globs: int, lets: int, *, scoped: bool = False) -> str:
    """A script importing and globbing *globs* modules, binding *lets* values, then calling one.

    Each module also declares a scope, which a *scoped* let reads too.
    """
    for i in range(globs):
        scope = f"scope K{i}\n  def g() -> int = {i}\nend K{i}\n" if scoped else ""
        (root / f"m{i}.agl").write_text(f"def f{i}() -> int = {i}\n{scope}", encoding="utf-8")
    return (
        "".join(f"import m{i}\n" for i in range(globs))
        + "".join(f"use m{i}::*\n" for i in range(globs))
        + "".join(
            f"let v{j} = f{j % globs}()" + (f" + K{j % globs}::g()\n" if scoped else "\n")
            for j in range(lets)
        )
        + f"f{globs - 1}()\n"
    )


@pytest.mark.parametrize("lets", [0, 5, -1], ids=("plain", "binding", "scaling"))
@pytest.mark.parametrize("host", ["script", "repl"])
def test_reading_glob_uses_of_a_script_or_repl_entry_does_not_cube_the_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, lets: int
) -> None:
    """Binding a value discards only reads that can depend on it; -1 lets one bind per glob."""
    reads = 0
    original = UseReader._use_path_reached

    def counting(self: UseReader, *args: Any, **kwargs: Any) -> Any:
        nonlocal reads
        reads += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(UseReader, "_use_path_reached", counting)

    def work(globs: int) -> int:
        nonlocal reads
        root = tmp_path / f"{host}{lets}x{globs}"
        root.mkdir()
        text = _glob_program(root, globs, globs if lets < 0 else lets, scoped=lets < 0)
        reads = 0
        if host == "script":
            result = run_inline_code(
                PipelineDriver(get_sandbox_context=None, resolve_agent_spec=None),
                text,
                roots=agl_roots(root),
                entry_path=root / "main.agl",
            )
            assert result.ok
        else:
            assert repl_session_with_root(root).eval_entry(text).ok
        return reads

    work(2)
    small, large = work(10), work(20)
    # Quadratic work at most: doubling the globs multiplies it by under 2^2.5.
    assert large < 5.6 * small


@pytest.mark.parametrize("host", ["script", "repl"])
def test_a_glob_use_reads_a_scoped_binding_only_from_where_the_walk_binds_it(
    tmp_path: Path, host: str
) -> None:
    (tmp_path / "m.agl").write_text("def g() -> int = 1\n", encoding="utf-8")

    def ok(last: str) -> bool:
        text = (
            "import m\nuse m::*\nscope S\n  use T::*\n  let a = g()\n"
            f"  scope T\n    let g = 7\n  end T\n{last}end S\n"
        )
        if host == "repl":
            return repl_session_with_root(tmp_path).eval_entry(text).ok
        return run_inline_code(
            PipelineDriver(get_sandbox_context=None, resolve_agent_spec=None),
            text,
            roots=agl_roots(tmp_path),
            entry_path=tmp_path / "main.agl",
        ).ok

    # ``g`` is ``m::g`` above the scope ``T``'s binding and ``T::g``, an int, below it.
    assert ok("")
    assert not ok("  let b = g()\n")


@pytest.mark.parametrize("host", ["script", "repl"])
def test_a_first_name_beneath_globs_is_read_before_and_after_a_scope_binds_it(
    tmp_path: Path, host: str
) -> None:
    (tmp_path / "m.agl").write_text("scope K\n  def x() -> int = 1\nend K\n", encoding="utf-8")
    text = (
        "import m\nuse m::*\nuse T::*\n\n"
        "scope T\n  let a = K::x()\n  let K = 2\n  let b = K\nend T\n"
    )
    if host == "repl":
        assert repl_session_with_root(tmp_path).eval_entry(text).ok
        return
    assert run_inline_code(
        PipelineDriver(get_sandbox_context=None, resolve_agent_spec=None),
        text,
        roots=agl_roots(tmp_path),
        entry_path=tmp_path / "main.agl",
    ).ok


def _scoped_glob_program(root: Path, globs: int) -> str:
    """A script globbing *globs* modules, declaring as many scopes and reading one scope each."""
    for i in range(globs):
        scope = f"scope K{i}\n  def g() -> int = {i}\nend K{i}\n"
        (root / f"m{i}.agl").write_text(f"def f{i}() -> int = {i}\n{scope}", encoding="utf-8")
    return (
        "".join(f"import m{i}\n" for i in range(globs))
        + "".join(f"use m{i}::*\n" for i in range(globs))
        + "".join(f"scope S{j}\n  let w = f{j}()\nend S{j}\n" for j in range(globs))
        + "".join(f"let v{j} = K{j}::g()\n" for j in range(globs))
    )


@pytest.mark.parametrize("host", ["script", "repl"])
def test_a_name_is_asked_only_of_the_globs_that_can_expose_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Own scopes and one scope per glob target ask no more than the globs exposing them."""
    asked = {"beneath": 0, "exposure": 0}

    def counting(name: str, key: str) -> None:
        original = getattr(UseReader, name)

        def wrapper(self: UseReader, *args: Any, **kwargs: Any) -> Any:
            asked[key] += 1
            return original(self, *args, **kwargs)

        monkeypatch.setattr(UseReader, name, wrapper)

    counting("_reaches_beneath_target", "beneath")
    counting("exposure", "exposure")

    def work(globs: int) -> dict[str, int]:
        root = tmp_path / f"{host}{globs}"
        root.mkdir()
        text = _scoped_glob_program(root, globs)
        for key in asked:
            asked[key] = 0
        if host == "script":
            result = run_inline_code(
                PipelineDriver(get_sandbox_context=None, resolve_agent_spec=None),
                text,
                roots=agl_roots(root),
                entry_path=root / "main.agl",
            )
            assert result.ok
        else:
            assert repl_session_with_root(root).eval_entry(text).ok
        return dict(asked)

    work(2)
    small, large = work(10), work(20)
    # Linear work at most: doubling the globs multiplies it by under 2^1.5.
    for key in asked:
        assert large[key] < 2.8 * small[key], key
