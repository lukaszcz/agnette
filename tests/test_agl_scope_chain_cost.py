"""Resolving a long unknown qualifier chain does work linear in its length."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agnette.agl.diagnostics import AglError
from agnette.agl.repl.session import ReplSession
from agnette.agl.scope import lookup


def _lookup_work(
    session: ReplSession, resolve: Callable[[ReplSession], object], monkeypatch: pytest.MonkeyPatch
) -> int:
    """Count every owner lookup, cache hit or miss, and every full-path reading *resolve* makes."""
    count = 0
    owners, reading = lookup._Walk._owners, lookup._Walk._reading

    def counting_owners(self: lookup._Walk, step: lookup._Step, at: int) -> object:
        nonlocal count
        count += 1
        return owners(self, step, at)

    def counting_reading(
        self: lookup._Walk,
        step: lookup._Step,
        kind: lookup.LookupKind,
        *,
        injects: bool,
        owners_within: int,
    ) -> object:
        nonlocal count
        count += 1
        return reading(self, step, kind, injects=injects, owners_within=owners_within)

    with monkeypatch.context() as patch:
        patch.setattr(lookup._Walk, "_owners", counting_owners)
        patch.setattr(lookup._Walk, "_reading", counting_reading)
        resolve(session)
    return count


def _info(chain: str) -> Callable[[ReplSession], object]:
    def resolve(session: ReplSession) -> object:
        with pytest.raises(AglError):
            session.info_of(chain)
        return None

    return resolve


def _entry(chain: str) -> Callable[[ReplSession], object]:
    return lambda session: session.eval_entry(chain)


@pytest.mark.parametrize("segment", ["a", "zz"])
@pytest.mark.parametrize("resolver", [_info, _entry])
def test_unknown_chain_cost_is_linear(
    segment: str,
    resolver: Callable[[str], Callable[[ReplSession], object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ReplSession()
    assert session.eval_entry("scope a\n  def f() -> int = 1\nend a").ok

    short = _lookup_work(session, resolver("::".join([segment] * 30)), monkeypatch)
    long = _lookup_work(session, resolver("::".join([segment] * 60)), monkeypatch)

    assert long < 3 * short
