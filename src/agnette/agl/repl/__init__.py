"""AgL REPL package.

The UI-free incremental session core lives in :mod:`agnette.agl.repl.session`.
Sibling modules hold the shared loop, the two front ends, meta-command dispatch,
and result rendering.
"""

from __future__ import annotations

from agnette.agl.repl.entry import EntryResult
from agnette.agl.repl.session import ReplSession

__all__ = ["EntryResult", "ReplSession"]
