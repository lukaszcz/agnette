"""Resolve engine settings the way a host with no program does, for tests."""

from __future__ import annotations

from collections.abc import Mapping

from agnette.agl.diagnostics import Diagnostic, configured_diagnostics
from agnette.agl.ir.static_keys import StaticBindingKey
from agnette.agl.runtime.engine_config import EngineSettings
from agnette.agl.semantics.values import Value
from agnette.agm.cli_support.engine_seeds import engine_seed_values, parse_engine_flags
from agnette.config.general import GeneralConfig
from agnette.host_params.config import resolve_programless_engine_settings
from tests._engine_flags import engine_flag_tokens

#: The settings, the configured failures, then the usage failures (supplied by the command line).
type Resolved = tuple[EngineSettings, list[Diagnostic], list[Diagnostic]]


def _flag_tokens(name: str, value: object | None) -> list[str]:
    """Return the command-line tokens that give engine setting *name* the native *value*."""
    match name:
        case "trace":
            return engine_flag_tokens(trace=True) if value else engine_flag_tokens(no_trace=True)
        case "trace-file":
            return engine_flag_tokens(trace_file=str(value))
        case "timeout":
            return (
                engine_flag_tokens(no_timeout=True)
                if value is None
                else engine_flag_tokens(timeout=str(value))
            )
        case "strict-json":
            return engine_flag_tokens(strict_json=bool(value))
        case "debug":
            return engine_flag_tokens(debug=bool(value))
        case "agent":
            return engine_flag_tokens(agent=str(value))
        case "sandbox":
            return engine_flag_tokens(sandbox=str(value))
        case "parse-error-retries":
            return engine_flag_tokens(parse_error_retries=int(str(value)))
    raise AssertionError(f"no flag for engine setting {name!r}")


def _supplied(cli: Mapping[str, object | None]) -> dict[StaticBindingKey, object]:
    """Return the raw values the real parser yields for the flags spelling *cli*."""
    return parse_engine_flags([t for name, value in cli.items() for t in _flag_tokens(name, value)])


def resolve_programless(
    *,
    cli: Mapping[str, object | None] | None = None,
    exec_table: Mapping[str, object] | None = None,
) -> Resolved:
    """Resolve engine settings from native *cli* flag values over an ``[exec]`` table."""
    config = GeneralConfig.from_layers([{"exec": dict(exec_table or {})}], [None])
    settings, diagnostics, usage = resolve_programless_engine_settings(config, _supplied(cli or {}))
    return settings, configured_diagnostics(diagnostics, usage), list(usage)


def programless_seeds(
    *,
    cli: Mapping[str, object | None] | None = None,
    exec_table: Mapping[str, object] | None = None,
) -> dict[str, Value]:
    """Return the engine seeds of :func:`resolve_programless`, which must not fail."""
    settings, configured, usage = resolve_programless(cli=cli, exec_table=exec_table)
    assert not configured
    assert not usage
    return engine_seed_values(settings)
