"""Shared runner for AgL tests of the ``std/git`` module.

Every ``std/git`` test module runs its program through :func:`run_git_program`, so
each workflow compiles the module once and shares one way of capturing output.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from agnette.agl.modules.roots import RootSet
from agnette.agl.pipeline import PipelineDriver, RunResult
from tests._agl_helpers import run_program


def run_git_program(
    body: str,
    cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    imports: tuple[str, ...] = (),
    runtime: PipelineDriver | None = None,
    roots: RootSet | None = None,
) -> tuple[str, RunResult]:
    """Run *body* as ``main`` with ``std/git`` and ``GitError`` imported and cwd set to *cwd*.

    ``std/env::environ`` is seeded from the current (isolated) ``os.environ``, as ``agm exec``
    does. *imports* adds further ``import`` lines; *runtime* and *roots* replace the default driver
    and roots (see :func:`~tests._agl_helpers.run_program`). Returns the program's stdout and its
    result.
    """
    header = "".join(f"import {name}\n" for name in ("std/git", "std/git::GitError", *imports))
    source = f"{header}program def main() -> unit =\n{textwrap.indent(body.strip(), '  ')}\n"
    monkeypatch.chdir(cwd)
    result = run_program(source, process_environment=dict(os.environ), runtime=runtime, roots=roots)
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


def agl_raises(call: str) -> str:
    """Return AgL printing ``raised`` if *call* raises ``GitError``, else ``returned``."""
    return f"""
try
  let _ = {call}
  print("returned")
catch GitError =>
  print("raised")
"""


def agl_completed(label: str, call: str) -> str:
    """Return AgL printing the new ``HEAD`` when *call* is ``Completed``, else ``not completed``."""
    return f"""
let {label} = {call}
case {label} of
  | Completed(head) => print(head)
  | _ => print("not completed")
"""


def agl_array(*items: str) -> str:
    """Return the AgL array literal of the AgL expressions *items*."""
    return "[" + ", ".join(items) + "]"


def tracked_entry(path: str, staged: str = "Unmodified", unstaged: str = "Unmodified") -> str:
    """Return the AgL ``StatusEntry`` literal of a tracked *path* with the given ``Change``s."""
    return f'StatusEntry("{path}", Tracked({staged}, {unstaged}))'


def untracked_entry(path: str) -> str:
    """Return the AgL ``StatusEntry`` literal of an untracked *path*."""
    return f'StatusEntry("{path}", Untracked)'
