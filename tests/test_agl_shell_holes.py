"""Static checks for values interpolated into ``exec`` command templates."""

from __future__ import annotations

from agnette.agl import PipelineDriver
from agnette.agl.warning_policy import WarningPolicy


def test_partial_applications_do_not_inherit_exec_hole_guarantees() -> None:
    source = """\
import std/exec::{shell-quote}

@exempts("unquoted-exec-hole")
def vetted(value: text) -> text = value

program def main() -> unit =
  exec $ echo %{shell-quote(?)}
  exec $ echo %{vetted(?)}
"""
    runtime = PipelineDriver(
        resolve_agent_spec=None,
        get_sandbox_context=None,
        warning_policy=WarningPolicy({"unquoted-exec-hole": "deny"}),
    )

    result = runtime.run(source, check_only=True)

    assert not result.ok
    assert [(diagnostic.line, diagnostic.warning_name) for diagnostic in result.diagnostics] == [
        (7, "unquoted-exec-hole"),
        (8, "unquoted-exec-hole"),
    ]


def test_holes_in_try_and_finally_bodies_warn_once_each() -> None:
    source = """\
program def main(value: text) -> unit =
  exec(try "echo try %{value}" finally => exec "echo finally %{value}")
"""
    runtime = PipelineDriver(
        resolve_agent_spec=None,
        get_sandbox_context=None,
        warning_policy=WarningPolicy({"unquoted-exec-hole": "deny"}),
    )

    result = runtime.run(source, check_only=True)

    assert not result.ok
    assert [(diagnostic.line, diagnostic.warning_name) for diagnostic in result.diagnostics] == [
        (2, "unquoted-exec-hole"),
        (2, "unquoted-exec-hole"),
    ]
