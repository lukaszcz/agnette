"""Catalog-driven engine setting resolution: every engine key reaches the seeds from ``[exec]``
and from the command line, the same way ``agm exec`` and ``agm repl`` resolve them."""

from __future__ import annotations

import pytest

from agnette.agl.ir.builtin_nominals import NO_BUILTIN_DECLARATIONS
from agnette.agl.ir.ids import NominalId
from agnette.agl.ir.reserved_nominals import (
    require_reserved_enum_member_id,
    require_reserved_nominal_id,
)
from agnette.agl.runtime.engine_config import (
    engine_default_settings,
)
from agnette.agl.runtime.option import option_text
from agnette.agl.semantics.values import BoolValue, IntValue, RecordValue, TextValue
from agnette.config.engine_keys import (
    ENGINE_KEY_NAMES,
    ENGINE_KEYS,
    ENGINE_REGISTERS,
    TRACE_ENGINE_KEYS,
    TRACE_REGISTER,
    trace_write_implies_enabled,
)
from tests._agl_helpers import agent_value
from tests._engine_settings import programless_seeds, resolve_programless

_CONFIG_RAW_VALUES: dict[str, object] = {
    "trace": True,
    "strict-json": True,
    "agent": 'AgentCommand("configured")',
    "sandbox": "Native",
    "trace-file": "configured.jsonl",
    "timeout": "12s",
    "debug": True,
    "parse-error-retries": 7,
}

_CLI_VALUES: dict[str, object] = {
    "trace": False,
    "strict-json": False,
    "agent": 'AgentCommand("cli")',
    "sandbox": "Disabled",
    "trace-file": "cli.jsonl",
    "timeout": "3s",
    "debug": False,
    "parse-error-retries": 0,
}


@pytest.mark.parametrize("key", [spec.name for spec in ENGINE_KEYS])
@pytest.mark.parametrize(
    ("cli_given", "config_given"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_each_engine_key_seed_has_the_same_cli_config_presence_matrix(
    key: str, cli_given: bool, config_given: bool
) -> None:
    """Every catalog key is seeded exactly when a host layer supplied it."""
    exec_table = {key: _CONFIG_RAW_VALUES[key]} if config_given else {}
    cli = {key: _CLI_VALUES[key]} if cli_given else {}

    actual_keys = set(programless_seeds(cli=cli, exec_table=exec_table))

    expected_keys = {key} if cli_given or config_given else set()
    # A supplied trace-file also implies the readable ``trace`` setting.
    if key == "trace-file" and (cli_given or config_given):
        expected_keys.add("trace")
    # A winning true ``debug`` (the config value here; the CLI value is false)
    # turns the readable ``trace`` setting on.
    if key == "debug" and config_given and not cli_given:
        expected_keys.add("trace")
    assert actual_keys == expected_keys


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("claude/sonnet:medium", agent_value("AgentClaude", model="sonnet", thinking="medium")),
        ("codex/o3:high", agent_value("AgentCodex", model="o3", thinking="high")),
        (
            "pi/openai/gpt-5:low",
            agent_value("AgentPi", provider="openai", model="gpt-5", thinking="low"),
        ),
        (
            "anthropic/claude-opus:custom",
            agent_value("AgentPi", provider="anthropic", model="claude-opus", thinking="custom"),
        ),
        ("worker --flag", agent_value("AgentCommand", command="worker --flag")),
        (
            'AgentClaude("opus", "high")',
            agent_value("AgentClaude", model="opus", thinking="high"),
        ),
        (
            'Agent::AgentPi(provider = "openai", model = "gpt-5", thinking = "low")',
            agent_value("AgentPi", provider="openai", model="gpt-5", thinking="low"),
        ),
        (
            '{"$case": "AgentCommand", "command": "echo hi"}',
            agent_value("AgentCommand", command="echo hi"),
        ),
    ],
)
def test_cli_agent_values_decode_to_the_typed_agent_value(raw: str, expected: object) -> None:
    assert programless_seeds(cli={"agent": raw})["agent"] == expected


def test_toml_agent_decodes_through_the_same_host_text_dispatch() -> None:
    seeds = programless_seeds(exec_table={"agent": "claude/sonnet:experimental"})

    assert seeds["agent"] == agent_value("AgentClaude", model="sonnet", thinking="experimental")


def test_config_table_agent_is_json_shaped_data() -> None:
    """A native TOML table decodes as JSON-shaped data, not host text."""
    raw = {"$case": "AgentClaude", "model": "opus", "thinking": "high"}

    assert programless_seeds(exec_table={"agent": raw})["agent"] == agent_value(
        "AgentClaude", model="opus", thinking="high"
    )


def _optional_default() -> RecordValue:
    return RecordValue(
        nominal=NominalId(require_reserved_enum_member_id("Optional", "Default")), fields={}
    )


def _option_none() -> RecordValue:
    return RecordValue(
        nominal=NominalId(require_reserved_enum_member_id("Option", "None")), fields={}
    )


def test_bare_sandbox_cli_value_fills_every_omitted_field() -> None:
    """A bare ``Sandbox`` fills all four defaulted fields from the host-side constant table.

    No program exists yet at this pre-execution boundary, so the omitted
    fields are filled by :func:`~agnette.agl.semantics.type_table.reserved_field_default`
    rather than any evaluator.
    """
    assert programless_seeds(cli={"sandbox": "Sandbox"})["sandbox"] == RecordValue(
        nominal=NominalId(require_reserved_nominal_id("Sandbox")),
        fields={
            "memory": _optional_default(),
            "swap": _optional_default(),
            "settings": _option_none(),
            "patch": BoolValue(True),
        },
    )


def test_partial_sandbox_cli_value_keeps_its_explicit_field_and_defaults_the_rest() -> None:
    """An explicit field decodes normally; every omitted field still defaults."""
    seeds = programless_seeds(cli={"sandbox": 'Sandbox(memory = Some("8G"))'})

    assert seeds["sandbox"] == RecordValue(
        nominal=NominalId(require_reserved_nominal_id("Sandbox")),
        fields={
            "memory": RecordValue(
                nominal=NominalId(require_reserved_enum_member_id("Option", "Some")),
                fields={"value": TextValue("8G")},
            ),
            "swap": _optional_default(),
            "settings": _option_none(),
            "patch": BoolValue(True),
        },
    )


def test_an_invalid_command_line_value_is_a_usage_failure_naming_the_flag() -> None:
    """A decode failure of a supplied value names ``--agent`` and is not a configured failure."""
    settings, configured, usage = resolve_programless(cli={"agent": 'AgentClaude(model = "x"'})

    assert "agent" not in settings.values
    assert configured == []
    assert len(usage) == 1
    assert usage[0].line is None


def test_an_invalid_configured_agent_value_names_the_config_key() -> None:
    """A malformed ``[exec]`` value names ``agent``, never the flag."""
    settings, configured, usage = resolve_programless(exec_table={"agent": "   "})

    assert "agent" not in settings.values
    assert usage == []
    assert len(configured) == 1
    assert configured[0].line is None


def test_a_cli_value_shields_a_malformed_configured_value_from_decoding() -> None:
    """A table value the CLI overrides is never decoded, so it cannot fail the run."""
    settings, configured, usage = resolve_programless(
        cli={"agent": "worker --flag"}, exec_table={"agent": "   "}
    )

    assert configured == usage == []
    assert settings.values["agent"] == agent_value("AgentCommand", command="worker --flag")


@pytest.mark.parametrize(
    ("key", "raw_value"),
    [("trace-file", ""), ("trace-file", "  "), ("timeout", ""), ("timeout", 0), ("timeout", -3)],
)
def test_a_blank_option_text_config_value_is_an_explicit_none(key: str, raw_value: object) -> None:
    """A blank or non-positive ``Option[text]`` value is a seeded none, as in ``agm exec``."""
    seed = programless_seeds(exec_table={key: raw_value})[key]

    assert isinstance(seed, RecordValue)
    assert option_text(seed, nominals=NO_BUILTIN_DECLARATIONS) is None


def test_explicit_empty_option_cli_value_remains_a_seed() -> None:
    """``--no-timeout`` is encoded as a present ``None`` value."""
    assert set(programless_seeds(cli={"timeout": None})) == {"timeout"}


def test_configured_numeric_timeout_is_seeded_from_its_raw_spelling() -> None:
    """Option settings preserve a valid numeric TOML value as text."""
    assert "timeout" in programless_seeds(exec_table={"timeout": 0.5})


def test_trace_engine_keys_are_declared_engine_keys() -> None:
    """The trace register pair remains a projection of the engine-key catalog."""
    assert TRACE_ENGINE_KEYS <= ENGINE_KEY_NAMES


def test_every_register_is_named_after_its_own_switch_key() -> None:
    """A register's on/off switch is the key whose name it carries."""
    assert ENGINE_REGISTERS
    for register in ENGINE_REGISTERS:
        switches = [spec for spec in ENGINE_KEYS if spec.name == register]
        assert len(switches) == 1
        assert switches[0].register == register
        assert not switches[0].enables_register


def test_a_some_write_implies_enabled_exactly_for_the_declared_keys() -> None:
    """The runtime implication follows the catalog's ``enables_register`` mark."""
    for spec in ENGINE_KEYS:
        assert trace_write_implies_enabled(spec.name, True) is (
            spec.enables_register and spec.register == TRACE_REGISTER
        )
        assert trace_write_implies_enabled(spec.name, False) is False


def test_path_valued_engine_keys_carry_a_text_option() -> None:
    """A path-valued key is an ``Option[text]``, so a cleared path stays expressible."""
    from agnette.config.engine_keys import EngineKeyKind

    paths = [spec for spec in ENGINE_KEYS if spec.is_path]
    assert paths
    assert all(spec.kind is EngineKeyKind.OPTION_TEXT for spec in paths)


@pytest.mark.parametrize(
    ("cli", "exec_table", "supplied"),
    [({"parse-error-retries": -1}, {}, True), ({}, {"parse-error-retries": -1}, False)],
    ids=["cli", "config"],
)
def test_negative_parse_error_retries_fails(
    cli: dict[str, object | None], exec_table: dict[str, object], supplied: bool
) -> None:
    _, configured, usage = resolve_programless(cli=cli, exec_table=exec_table)

    assert len(usage if supplied else configured) == 1
    assert not (configured if supplied else usage)


@pytest.mark.parametrize("raw", ["three", True, 1.5])
def test_non_integer_configured_parse_error_retries_fails(raw: object) -> None:
    _, configured, usage = resolve_programless(exec_table={"parse-error-retries": raw})

    assert len(configured) == 1
    assert usage == []


@pytest.mark.parametrize("key", ["strict-json", "trace", "debug"])
def test_configured_bool_string_decodes_like_a_cli_token(key: str) -> None:
    settings, _, _ = resolve_programless(exec_table={key: "true"})

    assert settings.values[key] == BoolValue(True)


@pytest.mark.parametrize("key", ["strict-json", "trace", "debug"])
@pytest.mark.parametrize("raw", ["banana", 1])
def test_undecodable_configured_bool_fails_instead_of_seeding_false(key: str, raw: object) -> None:
    settings, configured, _ = resolve_programless(exec_table={key: raw})

    assert key not in settings.values
    assert len(configured) == 1


def test_zero_parse_error_retries_is_a_valid_seed() -> None:
    seeds = programless_seeds(cli={"parse-error-retries": 0})

    assert seeds["parse-error-retries"] == IntValue(0)


def test_engine_defaults_cover_exactly_the_catalog_keys_with_defaults() -> None:
    """A catalog addition cannot leave interpreter defaults without a value."""
    expected = {spec.name for spec in ENGINE_KEYS if spec.has_default}
    assert set(engine_default_settings()) == expected


# ---------------------------------------------------------------------------
# Tiers: the command line outranks ``[exec]``, which seeds on its own.
# ---------------------------------------------------------------------------


def test_an_exec_value_is_a_declared_tier_value() -> None:
    from agnette.agl.runtime.arguments import ParamTier

    settings, _, _ = resolve_programless(exec_table={"strict-json": True})

    assert settings.ranks == {"strict-json": ParamTier.DECLARED}


def test_a_cli_value_outranks_the_exec_value_it_overrides() -> None:
    from agnette.agl.runtime.arguments import ParamTier

    settings, _, _ = resolve_programless(
        cli={"strict-json": False}, exec_table={"strict-json": True}
    )

    assert settings.ranks == {"strict-json": ParamTier.SUPPLIED}
    assert settings.values["strict-json"] == BoolValue(False)


def test_a_named_trace_value_is_seeded_like_any_other_key() -> None:
    """``trace`` follows the ordinary tier rule; the seeds still derive it afterward."""
    settings, _, _ = resolve_programless(exec_table={"trace": True})

    assert "trace" in settings.values
    assert programless_seeds(exec_table={"trace": True})["trace"] == BoolValue(True)


def test_exec_trace_false_and_trace_file_still_seed_trace_true() -> None:
    """Regression: ``[exec] trace = false`` plus ``trace-file`` still implies ``trace``."""
    seeds = programless_seeds(exec_table={"trace": False, "trace-file": "p"})

    assert seeds["trace"] == BoolValue(True)


@pytest.mark.parametrize(
    ("table", "traced"),
    [({"trace-file": "   "}, False), ({"trace": False, "trace-file": "trace.jsonl"}, True)],
    ids=["whitespace_only_trace_file", "trace_false_and_trace_file"],
)
def test_derived_trace_is_set_when_trace_or_a_trace_file_is_configured(
    table: dict[str, object], traced: bool
) -> None:
    """``trace`` derives as ``trace or trace-file is a real path`` when configured."""
    assert programless_seeds(exec_table=table).get("trace", BoolValue(False)) == BoolValue(traced)


def test_trace_stays_absent_when_neither_trace_nor_trace_file_is_configured_anywhere() -> None:
    assert "trace" not in programless_seeds()
