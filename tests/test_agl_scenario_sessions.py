"""The scenario harness's session host carries listed sessions into a scripted child."""

from __future__ import annotations

import pytest

from agnette.agent.session.protocol import PortableSet
from agnette.agent.spec import AgentClaude, AgentCodex
from agnette.agl.runtime.sessions import SessionHostError
from tests.agl.scenario_harness import ScenarioSessionHost, ScriptedAgent


def test_a_scripted_child_continues_a_listed_session_on_its_own_agent_lazily() -> None:
    parent = ScriptedAgent("ask", ["parent answer"])
    child = ScriptedAgent("ask", ["child answer"])
    host = ScenarioSessionHost({"ask": parent})
    agent = AgentClaude("m", "t")
    listed = host.open(agent, "Cli", env={})
    unlisted = host.open(agent, "Cli", env={})
    default = host.default(agent, "Cli", env={})
    host.ask(listed, "before")

    portables = host.portable([listed, default])
    host.script_child({"ask": child})
    host.enter_forked_child(portables)

    assert child.sessions == []
    assert host.default(agent, "Cli", env={}) == default
    assert host.ask(listed, "after") == "child answer"
    assert [session.parent for session in child.sessions] == [parent.sessions[0].tag]
    assert child.sessions[0].prompts == ["after"]
    with pytest.raises(SessionHostError):
        host.ask(unlisted, "after")
    host.close_all()


def test_an_unlisted_default_is_fresh_in_the_child_without_scripted_children() -> None:
    parent = ScriptedAgent("ask", ["one", "two"])
    host = ScenarioSessionHost({"ask": parent})
    agent = AgentClaude("m", "t")
    default = host.default(agent, "Cli", env={})
    listed = host.open(agent, "Cli", env={})

    host.enter_forked_child(host.portable([listed, listed]))

    assert host.default(agent, "Cli", env={}) != default
    assert host.ask(listed, "q") == "one"
    host.close_all()


def test_snapshotting_a_scripted_session_without_fork_is_a_session_error() -> None:
    host = ScenarioSessionHost({"ask": ScriptedAgent("ask", [])})
    handle = host.open(AgentCodex("m", "t"), "Cli", env={})

    with pytest.raises(SessionHostError):
        host.portable([handle])
    assert host.portable([]) == PortableSet()


def test_a_scripted_child_passes_an_unused_listed_session_on_to_a_grandchild() -> None:
    child = ScriptedAgent("ask", ["child answer"])
    grandchild = ScriptedAgent("ask", ["grandchild answer"])
    host = ScenarioSessionHost({"ask": ScriptedAgent("ask", ["parent answer"])})
    listed = host.open(AgentClaude("m", "t"), "Cli", env={})
    host.script_child({"ask": child})
    host.enter_forked_child(host.portable([listed]))

    passed_on = host.portable([listed])
    host.script_child({"ask": grandchild})
    host.enter_forked_child(passed_on)

    assert host.ask(listed, "q") == "grandchild answer"
    assert [session.parent for session in grandchild.sessions] == [child.sessions[0].tag]
    host.close_all()


def test_an_unsupported_fork_scripted_in_a_child_is_recorded_for_an_unused_listed_session() -> None:
    child = ScriptedAgent("ask", ["child answer"], session_operations={"fork": ["unsupported"]})
    host = ScenarioSessionHost({"ask": ScriptedAgent("ask", ["parent answer"])})
    listed = host.open(AgentClaude("m", "t"), "Cli", env={})
    host.script_child({"ask": child})
    host.enter_forked_child(host.portable([listed]))

    with pytest.raises(SessionHostError):
        host.fork(listed)
    host.fork(listed)

    assert child.sessions[0].operations == [
        ("fork", None, "unsupported"),
        ("fork", None, "success"),
    ]
    host.close_all()


def test_a_child_script_lacking_the_listed_session_agent_is_a_harness_error() -> None:
    host = ScenarioSessionHost({"ask": ScriptedAgent("ask", [])})
    listed = host.open(AgentClaude("m", "t"), "Cli", env={})
    host.script_child({"other": ScriptedAgent("other", [])})

    with pytest.raises(ValueError):
        host.enter_forked_child(host.portable([listed]))
