"""Resolving a long unknown qualifier chain does work linear in its length."""

from __future__ import annotations

import pytest

from agnette.agl.diagnostics import AglError
from agnette.agl.repl.session import ReplSession
from agnette.agl.scope import lookup


def _owner_computations(session: ReplSession, length: int, monkeypatch: pytest.MonkeyPatch) -> int:
    """Count the owner lookups that miss the walk cache while describing ``a::...::a``."""
    count = 0
    original = lookup._Walk._owners

    def counting(self: lookup._Walk, step: lookup._Step, at: int) -> tuple[lookup.Candidate, ...]:
        nonlocal count
        count += (step.path, at) not in self._reached
        return original(self, step, at)

    with monkeypatch.context() as patch:
        patch.setattr(lookup._Walk, "_owners", counting)
        with pytest.raises(AglError):
            session.info_of("::".join(["a"] * length))
    return count


def test_unknown_chain_cost_is_linear(monkeypatch: pytest.MonkeyPatch) -> None:
    session = ReplSession()
    assert session.eval_entry("scope a\n  def f() -> int = 1\nend a").ok

    short = _owner_computations(session, 30, monkeypatch)
    long = _owner_computations(session, 60, monkeypatch)

    assert long < 3 * short
