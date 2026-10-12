"""Tests for the AgM command-tree layout.

End-to-end command coverage is enforced separately and dynamically by
``tests/_command_coverage.py``, which records the commands actually executed
through the ``agm`` binary rather than scanning test sources for call literals.
"""

from __future__ import annotations

from pathlib import Path

from agnette.agm.cli import app
from agnette.command_catalog import COMMAND_ALIASES


def test_command_subpackages_match_cli_command_groups() -> None:
    """commands/ sub-packages must mirror CLI command groups (repository guideline)."""
    commands_dir = Path(__file__).resolve().parents[1] / "src" / "agnette" / "agm" / "commands"
    subpackages = {
        path.name
        for path in commands_dir.iterdir()
        # Deleted packages can leave directories containing only bytecode caches.
        if path.is_dir() and (path / "__init__.py").is_file()
    }

    assert subpackages == {
        group.name for group in app.registered_groups if group.name not in COMMAND_ALIASES
    }
