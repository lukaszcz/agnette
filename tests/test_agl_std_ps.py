"""`std/ps` outcomes that a program cannot provoke."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from agnette.agl import PipelineDriver
from agnette.agl.runtime.transfer import ImageTransfer, TransferError
from tests._agl_helpers import agl_roots, run_inline_code
from tests.agl.isolated_runner import run_callable

_PROGRAM = """\
import std/ps
import std/ps::{ProcessExitError}

program def main() -> unit =
  try
    print ps::wait(ps::fork(fn() => 1))
  catch ProcessExitError as e =>
    print "exit %{e.exit-code} signal %{e.signal}"
"""


def _run_with_untransferable_result(workdir: Path) -> bool:
    runtime = PipelineDriver(get_sandbox_context=None, resolve_agent_spec=None)
    with mock.patch.object(ImageTransfer, "dumps", side_effect=TransferError("untransferable")):
        return run_inline_code(runtime, _PROGRAM, roots=agl_roots(workdir)).ok


def test_a_result_the_child_cannot_transfer_makes_wait_raise_an_exit_error_without_code(
    tmp_path: Path,
) -> None:
    run = run_callable(_run_with_untransferable_result, tmp_path)

    assert run.value
    assert run.stdout == "exit Option::None signal Option::None\n"
