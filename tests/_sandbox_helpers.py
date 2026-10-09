"""Shared sandbox test helpers."""

from __future__ import annotations

from pathlib import Path

from agnette.sandbox.request import PreparedSandboxCommand


def settings_path_of(prepared: PreparedSandboxCommand) -> Path:
    """Return the SRT settings file named in a sandboxed *prepared*'s argv."""
    argv = prepared.argv
    return Path(argv[argv.index("--settings") + 1])
