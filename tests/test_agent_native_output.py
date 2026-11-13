"""Native schema enforcement through the production AgL session path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from agnette.agent import runner
from tests._agl_helpers import write_file_program
from tests.conftest import FakeAgentTransport
from tests.test_exec_agent_values import _invoke


@pytest.mark.parametrize("agent", ["Claude", "Codex"])
@pytest.mark.parametrize("echo", [False, True])
def test_typed_ask_uses_native_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_agent_transport: FakeAgentTransport,
    agent: str,
    echo: bool,
) -> None:
    schemas: list[object] = []
    paths: list[Path] = []
    capture = runner.prepare_rendered_prompt_run

    def run(prompt: str, *, runner: list[str], **kwargs: object):
        argv = runner
        if agent == "Codex":
            path = Path(argv[argv.index("--output-schema") + 1])
            paths.append(path)
            schemas.append(json.loads(path.read_text()))
        else:
            schemas.append(json.loads(argv[argv.index("--json-schema") + 1]))
        return capture(prompt, runner=runner, **kwargs)

    monkeypatch.setattr(runner, "prepare_rendered_prompt_run", run)
    program = tmp_path / "program.agl"
    write_file_program(
        program,
        f'let result: int = Agent{agent}().ask("Count.")\nprint result\n',
    )
    reply = '{"result":42}'
    outcome = (
        fake_agent_transport.codex_success(reply)
        if agent == "Codex"
        else fake_agent_transport.success(
            json.dumps(
                {
                    "type": "result",
                    "result": "A summary, not the answer.",
                    "structured_output": {"result": 42},
                }
            )
        )
    )
    fake_agent_transport.queue(outcome)

    result = _invoke(
        CliRunner(), ["exec", "--no-trace", "--echo" if echo else "--no-echo", str(program)]
    )

    assert result.exit_code == 0, result.output
    assert "42\n" in result.stdout
    assert schemas == [
        {
            "type": "object",
            "properties": {"result": {"type": "integer"}},
            "required": ["result"],
            "additionalProperties": False,
        }
    ]
    assert "```json" not in fake_agent_transport.calls[0][0]
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"enum": ["red", "blue"]}, {"type": "string", "enum": ["red", "blue"]}),
        ({"const": "red"}, {"type": "string", "enum": ["red"]}),
        (
            {"type": "array", "items": {"type": "boolean"}},
            {"type": "array", "items": {"type": "boolean"}},
        ),
        (
            {"oneOf": [{"const": "red"}, {"const": "blue"}]},
            {"anyOf": [{"type": "string", "enum": ["red"]}, {"type": "string", "enum": ["blue"]}]},
        ),
        (
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        ),
    ],
)
def test_native_schema_preserves_closed_shapes(schema: object, expected: object) -> None:
    from agnette.agent.output_schema import native_output_schema
    from agnette.agent.spec import AgentCodex

    encoded = native_output_schema(AgentCodex("", ""), json.dumps(schema))
    assert encoded is not None
    assert json.loads(encoded)["properties"]["result"] == expected


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"type": "object", "additionalProperties": {"type": "integer"}},
        {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": [],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"x": {}},
            "required": ["x"],
            "additionalProperties": False,
        },
        {"$ref": "#/$defs/tree", "$defs": {"tree": {}}},
        {"type": "array", "items": {}},
        {"oneOf": [{"type": "integer"}, {}]},
    ],
)
def test_unrepresentable_schemas_use_prompt_fallback(schema: object) -> None:
    from agnette.agent.output_schema import native_output_schema
    from agnette.agent.spec import AgentClaude, AgentPi

    assert native_output_schema(AgentClaude("", ""), json.dumps(schema)) is None
    assert native_output_schema(AgentPi("", "", ""), json.dumps(schema)) is None


def test_recursive_schema_definitions_stay_at_document_root() -> None:
    from agnette.agent.output_schema import native_output_schema
    from agnette.agent.spec import AgentClaude

    schema = {
        "$ref": "#/$defs/tree",
        "$defs": {"tree": {"type": "array", "items": {"$ref": "#/$defs/tree"}}},
    }
    encoded = native_output_schema(AgentClaude("", ""), json.dumps(schema))
    assert encoded is not None
    adapted = json.loads(encoded)
    assert adapted["$defs"] == schema["$defs"]
    assert adapted["properties"]["result"] == {"$ref": "#/$defs/tree"}


def test_envelope_extraction_preserves_numbers_and_ignores_nested_names() -> None:
    from agnette.agent.output_schema import json_field, unwrap_native_output

    assert (
        json_field('{"other":{"result":1}, "result" : 0.12345678901234567890123456789}', "result")
        == "0.12345678901234567890123456789"
    )
    assert unwrap_native_output('{"result": [1, 2]}') == "[1, 2]"


@pytest.mark.parametrize("output", ['{"other": 1}', "[]", '{"result": 1, "result": 2}', "broken"])
def test_missing_or_malformed_native_envelope_fails(output: str) -> None:
    from agnette.agent.output_schema import unwrap_native_output

    with pytest.raises(ValueError):
        unwrap_native_output(output)


@pytest.mark.parametrize("agent", ["Claude", "Codex"])
@pytest.mark.parametrize("mode", ["free", "session", "method"])
def test_native_schema_changes_between_asks_and_keeps_validation_retries(
    tmp_path: Path,
    fake_agent_transport: FakeAgentTransport,
    agent: str,
    mode: str,
) -> None:
    prefix = ""
    if mode == "free":
        prefix = f"import std/config\nstd/config::agent := Agent{agent}()\n"
        ask = "ask"
    elif mode == "session":
        prefix = f"let session = Session::open(Agent{agent}())\n"
        ask = "session.ask"
    else:
        ask = f"Agent{agent}().ask"
    program = tmp_path / "program.agl"
    write_file_program(
        program,
        prefix + f'let number: int = {ask}("Count.", parse-error-retries = 1)\n'
        f'let label: text = {ask}("Label.")\nprint number\nprint label\n',
    )
    for value in ["wrong", 42]:
        reply = json.dumps({"result": value})
        fake_agent_transport.queue(
            fake_agent_transport.codex_success(reply)
            if agent == "Codex"
            else fake_agent_transport.success(
                json.dumps({"type": "result", "structured_output": {"result": value}})
            )
        )
    fake_agent_transport.queue(
        fake_agent_transport.codex_success("done")
        if agent == "Codex"
        else fake_agent_transport.success("done")
    )

    result = _invoke(CliRunner(), ["exec", "--no-trace", "--no-echo", str(program)])

    assert result.exit_code == 0, result.output
    assert result.stdout == "42\ndone\n"
    flag = "--json-schema" if agent == "Claude" else "--output-schema"
    assert [flag in argv for _, argv in fake_agent_transport.calls] == [True, True, False]
    if mode != "method":
        assert (
            "--resume" in fake_agent_transport.calls[1][1]
            or "resume" in fake_agent_transport.calls[1][1]
        )


@pytest.mark.parametrize("agent", ["Claude", "Codex"])
@pytest.mark.parametrize("reply", ["{}", "broken", "[]"])
def test_native_protocol_failures_are_agent_call_errors(
    tmp_path: Path,
    fake_agent_transport: FakeAgentTransport,
    agent: str,
    reply: str,
) -> None:
    program = tmp_path / "program.agl"
    write_file_program(
        program,
        f'try\n  let answer: int = Agent{agent}().ask("Count.")\n  print answer\n'
        'catch AgentCallError => print "failed"\n',
    )
    fake_agent_transport.queue(
        fake_agent_transport.codex_success(reply)
        if agent == "Codex"
        else fake_agent_transport.success(reply)
    )
    result = _invoke(CliRunner(), ["exec", "--no-trace", "--no-echo", str(program)])
    assert result.exit_code == 0, result.output
    assert result.stdout == "failed\n"


@pytest.mark.parametrize("agent", ["Claude", "Codex", "Pi", "Command"])
def test_open_dictionary_ask_keeps_prompt_contract(
    tmp_path: Path,
    fake_agent_transport: FakeAgentTransport,
    agent: str,
) -> None:
    program = tmp_path / "program.agl"
    constructor = 'AgentCommand("worker")' if agent == "Command" else f"Agent{agent}()"
    prefix = ""
    if agent == "Pi":
        prefix = "let session = Session::open(AgentPi(), transport = Some(SessionTransport::Cli))\n"
        constructor = "session"
    write_file_program(
        program,
        prefix
        + f'let answer: dict[text, int] = {constructor}.ask("Count.")\nprint answer["count"]\n',
    )
    reply = '{"count":42}'
    fake_agent_transport.queue(
        fake_agent_transport.codex_success(reply)
        if agent == "Codex"
        else fake_agent_transport.success(reply)
    )
    result = _invoke(CliRunner(), ["exec", "--no-trace", "--no-echo", str(program)])
    assert result.exit_code == 0, result.output
    assert result.stdout == "42\n"
    prompt, argv = fake_agent_transport.calls[0]
    assert "```json" in prompt
    assert "--json-schema" not in argv
    assert "--output-schema" not in argv


def test_codex_schema_file_is_removed_after_spawn_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_agent_transport: FakeAgentTransport,
) -> None:
    paths: list[Path] = []
    prepare = runner.prepare_rendered_prompt_run

    def capture(prompt: str, *, runner: list[str], **kwargs: object):
        path = Path(runner[runner.index("--output-schema") + 1])
        assert path.is_file()
        paths.append(path)
        return prepare(prompt, runner=runner, **kwargs)

    monkeypatch.setattr(runner, "prepare_rendered_prompt_run", capture)
    fake_agent_transport.queue(fake_agent_transport.failure(spawn_error="unavailable"))
    program = tmp_path / "program.agl"
    write_file_program(
        program,
        'try\n  let answer: int = AgentCodex().ask("Count.")\n  print answer\n'
        'catch AgentCallError => print "failed"\n',
    )
    result = _invoke(CliRunner(), ["exec", "--no-trace", "--no-echo", str(program)])
    assert result.exit_code == 0, result.output
    assert result.stdout == "failed\n"
    assert len(paths) == 1
    assert not paths[0].exists()


def test_claude_schema_annotations_are_verbatim_command_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agnette.agent.output_schema import native_output_schema
    from agnette.agent.session.protocol import SessionAskRequest
    from agnette.agent.spec import AgentClaude
    from tests.test_agent_cli_session_adapters import CaptureOutcome, CaptureTransport, _open

    description = r"Keep %{UNSET_SCHEMA_VARIABLE}, %% and \%{literal}."
    schema = native_output_schema(
        AgentClaude("", ""), json.dumps({"type": "integer", "description": description})
    )
    assert schema is not None
    transport = CaptureTransport(
        [CaptureOutcome(stdout='{"type":"result","structured_output":{"result":42}}')]
    )
    transport.install(monkeypatch)
    backend = _open(AgentClaude("", ""))
    response = backend.ask(SessionAskRequest("Count.", output_schema=schema))
    assert response.content == "42"
    argv, _ = transport.calls[0]
    actual = json.loads(argv[argv.index("--json-schema") + 1])
    assert actual["properties"]["result"]["description"] == description


@pytest.mark.parametrize("agent", ["Claude", "Codex"])
def test_native_number_range_errors_still_allow_parse_retries(
    tmp_path: Path,
    fake_agent_transport: FakeAgentTransport,
    agent: str,
) -> None:
    program = tmp_path / "program.agl"
    write_file_program(
        program,
        f'let answer: decimal = Agent{agent}().ask("Measure.", parse-error-retries = 1)\n'
        "print answer\n",
    )
    for value in ["1e99999999999999999999999", "1.25"]:
        reply = '{"result":' + value + "}"
        fake_agent_transport.queue(
            fake_agent_transport.codex_success(reply)
            if agent == "Codex"
            else fake_agent_transport.success('{"type":"result","structured_output":' + reply + "}")
        )
    result = _invoke(CliRunner(), ["exec", "--no-trace", "--no-echo", str(program)])
    assert result.exit_code == 0, result.output
    assert result.stdout == "1.25\n"
    assert len(fake_agent_transport.calls) == 2


@pytest.mark.parametrize("agent", ["Claude", "Codex"])
def test_native_recursive_enum_with_nested_record_and_array(
    tmp_path: Path,
    fake_agent_transport: FakeAgentTransport,
    agent: str,
) -> None:
    program = tmp_path / "program.agl"
    write_file_program(
        program,
        "record Reading\n  count: int\nenum Tree\n  | Leaf\n"
        "  | Node(reading: Reading, children: array[Tree])\n"
        f'let tree: Tree = Agent{agent}().ask("Build.")\n'
        "case tree of\n  | Node(reading, _) => print reading.count\n  | Leaf => print 0\n",
    )
    value = {"$case": "Node", "reading": {"count": 42}, "children": [{"$case": "Leaf"}]}
    reply = json.dumps({"result": value})
    fake_agent_transport.queue(
        fake_agent_transport.codex_success(reply)
        if agent == "Codex"
        else fake_agent_transport.success(
            json.dumps({"type": "result", "structured_output": {"result": value}})
        )
    )
    result = _invoke(CliRunner(), ["exec", "--no-trace", "--no-echo", str(program)])
    assert result.exit_code == 0, result.output
    assert result.stdout == "42\n"
    flag = "--json-schema" if agent == "Claude" else "--output-schema"
    assert flag in fake_agent_transport.calls[0][1]
