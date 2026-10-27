"""A module-level ``@!doc`` in a REPL entry or loaded source documents nothing."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.repl.entry import EntryResult
from agnette.agl.repl.session import ReplSession
from agnette.agl.warning_policy import WarningPolicy

UNUSED_MODULE_DOC = "unused-module-doc"


def _session(*, deny: bool = False) -> ReplSession:
    policy = WarningPolicy({UNUSED_MODULE_DOC: "deny"} if deny else {})
    session = ReplSession(warning_policy=policy)
    session.open()
    return session


def _names(result: EntryResult) -> list[str | None]:
    return [warning.warning_name for warning in result.warnings]


@pytest.mark.parametrize("entry", ['@!doc("m")\nlet x = 1', '@!doc("m")'])
def test_entry_with_a_module_doc_warns(entry: str) -> None:
    result = _session().eval_entry(entry)

    assert result.ok
    assert _names(result) == [UNUSED_MODULE_DOC]


def test_region_doc_in_an_entry_does_not_warn() -> None:
    result = _session().eval_entry('scope r\n  @!doc("R.")\n  def g() -> int = 1\nend r')

    assert result.ok
    assert _names(result) == []


def test_allow_silences_the_warning() -> None:
    result = _session().eval_entry(f'@!allow("{UNUSED_MODULE_DOC}")\n@!doc("m")\nlet x = 1')

    assert result.ok
    assert _names(result) == []


def test_denied_warning_rejects_the_entry() -> None:
    session = _session(deny=True)

    result = session.eval_entry('@!doc("m")\nlet x = 1')

    assert not result.ok
    assert session.bindings() == []


@pytest.mark.parametrize(("allowed", "expected"), [(False, [UNUSED_MODULE_DOC]), (True, [])])
def test_loaded_source_warns_unless_allowed(
    tmp_path: Path, allowed: bool, expected: list[str]
) -> None:
    allow = f'@!allow("{UNUSED_MODULE_DOC}")\n' if allowed else ""
    path = tmp_path / "doc.agl"
    path.write_text(f'{allow}@!doc("m")\nlet x = 1\nlet y = 2\n', encoding="utf-8")
    session = _session()

    results = session.load_file(path)

    assert all(result.ok for result in results)
    assert [name for result in results for name in _names(result)] == expected
    assert [name for name, _type, _value in session.bindings()] == ["x", "y"]
