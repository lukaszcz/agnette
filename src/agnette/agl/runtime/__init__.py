"""AgL host runtime services package (eval-free).

Public API
----------
- :class:`AgentRequest` — request object passed to host agent callables.
- :class:`AgentResponse` — response from a host agent callable.
- :class:`OutputCodec` — protocol for output codecs.
- :class:`TextCodec` — the built-in passthrough text codec.
- :class:`JsonCodec` — the built-in structured-output codec.
- :class:`OutputContract` — materialized per-call output contract.
- :func:`render_value` — option-driven value renderer for interpolation, print,
  casts, builtin ``render``, and REPL echo.
"""

from __future__ import annotations

from agnette.agl.runtime.agents import AgentFn
from agnette.agl.runtime.codec import JsonCodec, OutputCodec, ParseResult, TextCodec
from agnette.agl.runtime.contract import OutputContract
from agnette.agl.runtime.render import render_value
from agnette.agl.runtime.request import AgentRequest, AgentResponse

__all__ = [
    "AgentFn",
    "AgentRequest",
    "AgentResponse",
    "JsonCodec",
    "OutputCodec",
    "OutputContract",
    "ParseResult",
    "TextCodec",
    "render_value",
]
