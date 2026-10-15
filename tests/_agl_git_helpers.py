"""Shared runner for AgL tests of the ``std/git`` module.

Every ``std/git`` test module runs its program through :func:`run_git_program`, so
each workflow compiles the module once and shares one way of capturing output.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from agnette.agl.pipeline import RunResult
from tests._agl_helpers import run_program


def run_git_program(
    body: str,
    cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    imports: tuple[str, ...] = (),
) -> tuple[str, RunResult]:
    """Run *body* as ``main`` with ``std/git`` imported and the process cwd set to *cwd*.

    ``std/env::environ`` is seeded from the current (isolated) ``os.environ``, as ``agm exec``
    does. *imports* adds further ``import`` lines. Returns the program's stdout and its result.
    """
    header = "".join(f"import {name}\n" for name in ("std/git", *imports))
    source = f"{header}program def main() -> unit =\n{textwrap.indent(body.strip(), '  ')}\n"
    monkeypatch.chdir(cwd)
    result = run_program(source, process_environment=dict(os.environ))
    return capsys.readouterr().out, result


def agl_check(name: str, actual: str, expected: str) -> str:
    """Return AgL statements printing ``ok`` when *actual* equals *expected*, else *actual*.

    *name* labels the binding, so statements built from different calls can share one scope.
    """
    return f"""
let {name} = {actual}
if {name} == {expected} =>
  print("ok")
else =>
  print({name})
"""
