"""An inline enum member's arity is settled before any field naming it resolves."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.typecheck.env import AglTypeError
from tests._agl_helpers import check_agl_program
from tests.agl.module_graph import resolve_and_check_inline_entry

_ERASING_ALIAS = "type C[a] = text\n"
_GENERIC_ENUM = "enum E[t]\n  | M(x: C[t])\n  | Q(y: t)\n"
_REFERRER = "enum F\n  | A(v: E::M)\n  | B\n"


@pytest.mark.parametrize("referrer_first", [True, False], ids=("forward", "backward"))
def test_a_member_naming_a_later_or_earlier_alias_erased_member_resolves_in_one_module(
    referrer_first: bool,
) -> None:
    declarations = [_REFERRER, _ERASING_ALIAS + _GENERIC_ENUM]
    resolve_and_check_inline_entry(
        "".join(declarations if referrer_first else declarations[::-1]) + "()"
    )


@pytest.mark.parametrize("referrer", ["entry", "zzz"])
def test_a_member_naming_an_alias_erased_member_of_another_module_resolves(
    tmp_path: Path, referrer: str
) -> None:
    check_agl_program(
        tmp_path,
        {
            referrer: "import lib\nenum F\n  | A(v: lib::E::M)\n  | B\n()",
            "lib": _ERASING_ALIAS + _GENERIC_ENUM,
        },
    )


def test_members_naming_each_other_through_arrays_settle_together() -> None:
    resolve_and_check_inline_entry(
        _ERASING_ALIAS
        + "enum E[t]\n  | M(x: C[t], y: array[G::N])\n  | Q(z: t)\n"
        + "enum G[u]\n  | N(w: array[E::M], v: C[u])\n  | R\n"
        + "()"
    )


def test_a_member_naming_itself_settles() -> None:
    resolve_and_check_inline_entry(
        _ERASING_ALIAS + "enum E[t]\n  | M(x: C[t], y: array[E::M])\n  | Q(z: t)\n()"
    )


def test_a_member_that_keeps_a_parameter_still_requires_its_argument() -> None:
    with pytest.raises(AglTypeError):
        resolve_and_check_inline_entry(
            "enum E[t]\n  | M(x: t)\n  | Q\nenum F\n  | A(v: E::M)\n  | B\n()"
        )


def test_type_arguments_for_an_alias_erased_member_are_still_rejected() -> None:
    with pytest.raises(AglTypeError):
        resolve_and_check_inline_entry(
            _ERASING_ALIAS + _GENERIC_ENUM + "enum F\n  | A(v: E::M[int])\n  | B\n()"
        )


def test_a_member_applied_to_too_many_arguments_is_rejected() -> None:
    with pytest.raises(AglTypeError):
        resolve_and_check_inline_entry("enum E[t]\n  | M(x: E::Q[int, int])\n  | Q(y: t)\n()")
