"""A halted load reports the TAB advisories of the entries after the failing one."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.repl.session import ReplSession
from agnette.agl.warning_policy import WarningPolicy

TAB = "tab-indentation"


@pytest.mark.parametrize("deny", [False, True], ids=("warn", "deny"))
def test_a_halted_load_reports_a_later_tab_where_the_file_has_it(
    tmp_path: Path, deny: bool
) -> None:
    path = tmp_path / "p.agl"
    path.write_text('\t\tlet a: int = "s"; let\tb = 1\n')
    session = ReplSession(warning_policy=WarningPolicy({TAB: "deny"}) if deny else None)
    session.open()

    (result,) = session.load_file(path)

    reported = result.diagnostics if deny else result.warnings
    assert not result.ok
    assert [(d.line, d.column) for d in reported if d.warning_name == TAB] == [
        (1, 1),
        (1, 2),
        (1, 24),
    ]
