"""Engine-setting CLI tokens for tests that build ``ExecArgs``/``ReplArgs`` directly."""

from __future__ import annotations

from dataclasses import replace
from typing import TypedDict, Unpack

from agnette.agm.cli_support.args import ExecArgs


class EngineSettings(TypedDict, total=False):
    """The engine settings :func:`engine_flag_tokens` takes, by keyword."""

    strict_json: bool | None
    no_trace: bool
    trace: bool
    trace_file: str | None
    timeout: str | None
    no_timeout: bool
    agent: str | None
    sandbox: str | None
    debug: bool | None
    parse_error_retries: int | None


ENGINE_SETTING_ARGS = frozenset(EngineSettings.__annotations__)


def engine_flag_tokens(**engine: Unpack[EngineSettings]) -> list[str]:
    """Return the flag tokens that set the given engine settings on the command line."""
    tokens: list[str] = []
    for flag, value in (("strict-json", engine.get("strict_json")), ("debug", engine.get("debug"))):
        if value is not None:
            tokens.append(f"--{flag}" if value else f"--no-{flag}")
    tokens += ["--no-trace"] * engine.get("no_trace", False)
    tokens += ["--trace"] * engine.get("trace", False)
    tokens += ["--no-timeout"] * engine.get("no_timeout", False)
    retries = engine.get("parse_error_retries")
    for flag, text in (
        ("trace-file", engine.get("trace_file")),
        ("timeout", engine.get("timeout")),
        ("agent", engine.get("agent")),
        ("sandbox", engine.get("sandbox")),
        ("parse-error-retries", None if retries is None else str(retries)),
    ):
        if text is not None:
            tokens += [f"--{flag}", text]
    return tokens


def exec_args_with(args: ExecArgs, **engine: Unpack[EngineSettings]) -> ExecArgs:
    """Return *args* with the given engine settings prepended to its argument tokens."""
    return replace(args, argument_tokens=[*engine_flag_tokens(**engine), *args.argument_tokens])
