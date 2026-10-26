"""REPL bare type entries: unopened sessions, inner attributes, and open-failure recovery."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agnette.agl.diagnostics import AglError
from agnette.agl.repl.session import ReplSession, SessionOpenError
from agnette.agl.warning_policy import WarningPolicy

UNUSED_MODULE_DOC = "unused-module-doc"
STDLIB = Path(__file__).resolve().parents[1] / "packages" / "stdlib"


def _session(*, deny: bool = False) -> ReplSession:
    session = ReplSession(warning_policy=WarningPolicy({UNUSED_MODULE_DOC: "deny"} if deny else {}))
    session.open()
    return session


@pytest.mark.parametrize("reset", [False, True])
def test_a_bare_generic_type_displays_in_a_session_that_has_not_opened(reset: bool) -> None:
    session = ReplSession()
    if reset:
        session.open()
        session.reset()

    result = session.eval_entry("Option")

    assert result.ok
    assert result.kind == "type"
    assert result.type_display is not None


@pytest.mark.parametrize("deny", [False, True])
@pytest.mark.parametrize("generic", ["Option", "Box"])
def test_an_attributed_bare_generic_type_is_governed(generic: str, deny: bool) -> None:
    session = _session(deny=deny)
    session.eval_entry("record Box[T]\n  value: T")

    result = session.eval_entry(f'@!doc("x")\n{generic}')

    assert result.ok is not deny
    assert (result.kind == "type") is not deny
    assert [w.warning_name for w in result.warnings] == ([] if deny else [UNUSED_MODULE_DOC])


def test_an_allow_silences_an_attributed_bare_generic_type() -> None:
    result = _session().eval_entry(f'@!allow("{UNUSED_MODULE_DOC}")\n@!doc("x")\nOption')

    assert result.ok
    assert result.warnings == []


@pytest.mark.parametrize("type_text", ["int -> int", "(int, text) -> int", "dict[text, int]"])
def test_attributes_may_head_a_type_that_is_no_value(type_text: str) -> None:
    result = _session().eval_entry(f'@!doc("x")\n{type_text}')

    assert result.ok
    assert result.kind == "type"
    assert [w.warning_name for w in result.warnings] == [UNUSED_MODULE_DOC]


@pytest.mark.parametrize(
    "header",
    ["@!allow(1)", "@!foo", "@!doc(1)", '@!doc("a")\n@!doc("b")'],
)
def test_an_invalid_attribute_heading_a_type_is_what_is_reported(header: str) -> None:
    session = _session()
    result = session.eval_entry(f"{header}\nint")
    reference = session.eval_entry(f"{header}\n1")

    assert not result.ok
    assert not reference.ok
    assert result.diagnostics[0].line == reference.diagnostics[0].line


def test_a_scope_region_is_no_expression_for_type_of() -> None:
    with pytest.raises(AglError):
        _session().type_of("scope s\n  def f() -> int = 1\nend s")


@pytest.fixture
def broken_std(tmp_path: Path) -> tuple[Path, Path, str]:
    std = tmp_path / "std"
    shutil.copytree(STDLIB, std)
    prelude = std / "src" / "prelude.agl"
    good = prelude.read_text()
    prelude.write_text(good + '\nlet broken: int = "x"\n')
    return std, prelude, good


@pytest.mark.parametrize("recovery", ["int", "zz", "1"])
def test_a_checked_entry_clears_a_remembered_open_failure(
    broken_std: tuple[Path, Path, str], recovery: str
) -> None:
    std, prelude, good = broken_std
    session = ReplSession(stdlib_root=std)
    with pytest.raises(SessionOpenError):
        session.info_of("print")
    prelude.write_text(good)

    session.eval_entry(recovery)

    assert session.info_of("print")
