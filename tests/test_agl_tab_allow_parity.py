"""`agm check`, a load and a halted load report the same TAB advisories around an `@allow`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import agnette.agm.commands.check as check_command
from agnette.agl.repl.session import ReplSession
from agnette.agm.cli_support.args import CheckArgs

TAB = "tab-indentation"
_ALLOW = '@allow("tab-indentation")'

# Source text -> the TAB advisories (line, tab-expanded column) the file reports.
_CASES = {
    "after-the-allowed-item": (f"\t\t{_ALLOW} let pre = 1;\tlet\tb = 1\n", [(1, 47), (1, 51)]),
    "inside-the-allowed-item": (f"\t\t{_ALLOW} let\tpre = 1; let\tb = 1\n", [(1, 51)]),
    "before-the-allowed-item": (f"\tlet\ta = 1; {_ALLOW} let b = 1\n", [(1, 1), (1, 8)]),
}


def _checked(path: Path, capsys: pytest.CaptureFixture[str]) -> list[tuple[int, int]]:
    check_command.run(CheckArgs(files=[str(path)]))
    err = capsys.readouterr().err
    return [
        (int(m[1]), int(m[2]))
        for m in re.finditer(rf"^{re.escape(str(path))}:(\d+):(\d+):", err, re.M)
    ]


def _loaded(source: str, path: Path) -> list[tuple[int, int]]:
    path.write_text(source)
    session = ReplSession()
    session.open()
    return sorted(
        (w.line, w.column or 0)
        for result in session.load_file(path)
        for w in result.warnings
        if w.warning_name == TAB
    )


@pytest.mark.parametrize("name", sorted(_CASES))
def test_check_and_load_report_the_same_tabs_around_an_allow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    source, expected = _CASES[name]
    path = tmp_path / "p.agl"
    path.write_text(source)

    assert _checked(path, capsys) == expected
    assert _loaded(source, path) == expected


@pytest.mark.parametrize("name", sorted(_CASES))
def test_a_halted_load_reports_the_tabs_around_an_allow_as_a_load_does(
    tmp_path: Path, name: str
) -> None:
    source, expected = _CASES[name]
    base = source[: len(source) - len(source.lstrip("\t"))]
    halted = _loaded(f'{base}let a: int = "s"\n{source}', tmp_path / "p.agl")

    tabs_of_base = [(1, 4 * tab + 1) for tab in range(len(base))]
    assert halted == tabs_of_base + [(line + 1, column) for line, column in expected]


_HOLE = "unquoted-exec-hole"
_MODULE_ALLOW = f'@!allow("{_HOLE}", "{TAB}")\n'
# No trailing newline, as a REPL entry never has one.
_TAB_INDENTED_LAST_LINE = (
    f"{_MODULE_ALLOW}program def main(name: text) -> unit =\n"
    '\tif true =>\n\t\tif true =>\n\t\t\tprint (exec "ls %{name}")'
)


def test_a_module_allow_covers_a_tab_indented_last_line_without_a_newline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "p.agl"
    path.write_text(_TAB_INDENTED_LAST_LINE)
    check_command.run(CheckArgs(files=[str(path)]))
    session = ReplSession()
    session.open()
    loaded = session.load_file(path)
    typed = ReplSession()
    typed.open()
    entry = typed.eval_entry(_TAB_INDENTED_LAST_LINE)

    assert "warning" not in capsys.readouterr().err
    assert [w for r in loaded for w in r.warnings] == []
    assert entry.warnings == []
