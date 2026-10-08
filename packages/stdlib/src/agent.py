"""Interactive conversations for `std/agent`."""

from __future__ import annotations

from typing import cast

from agl import AglException, json, nominals, runtime

from agnette.agent.values import agent_spec_shape
from agnette.agl.runtime.agents import decode_agent_value, run_agent_chat
from agnette.agl.runtime.boundary import decode_boundary_value
from agnette.agl.runtime.host_agents import current_agent_services
from agnette.agl.runtime.request import AgentCallHostError, AgentRequest
from agnette.agl.runtime.sandbox_values import decode_agent_sandbox, permission_mode_and_limits
from agnette.agl.semantics.values import DictValue, RecordValue, TextValue

AgentCallError = nominals.std.agent.AgentCallError


def chat(prompt: str, agent: object, sandbox: object, env: object) -> None:
    """Hand the terminal to a fresh agent conversation; see `std/agent::chat`."""
    services = current_agent_services()
    spec = decode_agent_value(cast(RecordValue, decode_boundary_value(agent)), services.nominals)
    if services.resolve_agent_spec is not None:
        spec = services.resolve_agent_spec(spec)
    mode, limits = permission_mode_and_limits(
        decode_agent_sandbox(cast(RecordValue, decode_boundary_value(sandbox)), services.nominals)
    )
    environ = cast(RecordValue, decode_boundary_value(env))
    variables = cast(DictValue, environ.fields["vars"])
    request = AgentRequest(
        agent=spec,
        prompt=prompt,
        env={name: cast(TextValue, value).value for name, value in variables.text_items()},
        permission_mode=mode,
        sandbox=limits,
    )
    runtime.trace(
        "agent_chat",
        {
            "prompt": prompt,
            "agent": agent_spec_shape(spec),
            "sandboxed": limits is not None,
            "permission_mode": mode.value,
        },
    )
    try:
        response = run_agent_chat(request, get_sandbox_context=services.get_sandbox_context)
    except AgentCallHostError as error:
        metadata = {
            "exit_code": error.exit_code,
            "stderr_tail": error.stderr_tail,
            "elapsed": error.elapsed,
        }
        runtime.trace("agent_chat_end", {"ok": False, "cause": error.cause, **metadata})
        raise AglException(
            AgentCallError(
                message=f"Agent failed: {error.cause}\n{error.stderr_tail}",
                agent=agent,
                cause=error.cause,
                metadata=json(metadata),
            )
        ) from error
    runtime.trace("agent_chat_end", {"ok": True, **response.metadata})


def agent_chat(agent: object, prompt: str, sandbox: object, env: object) -> None:
    """Receiver form of `chat`."""
    chat(prompt, agent, sandbox, env)


__all__ = ["chat", "agent_chat"]
