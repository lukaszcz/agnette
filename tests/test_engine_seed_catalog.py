"""Characterization tests for catalog-driven host engine seeding."""

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
from agnette.agm.cli_support.engine_seeds import build_host_engine_seeds
from agnette.config.engine_keys import (
    ENGINE_KEY_NAMES,
    ENGINE_KEYS,
    ENGINE_REGISTERS,
    TRACE_ENGINE_KEYS,
    TRACE_REGISTER,
    EngineKeySpec,
    trace_write_implies_enabled,
)
from agnette.config.general import ExecConfig, exec_config_from_merged
from tests._agl_helpers import agent_value

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


def _config_for(key: str, configured: bool) -> ExecConfig:
    return ExecConfig(
        strict_json=configured if key == "strict-json" else False,
        timeout=12.0 if configured and key == "timeout" else None,
        trace=configured if key == "trace" else False,
        trace_file="configured.jsonl" if configured and key == "trace-file" else None,
        agent='AgentCommand("configured")' if configured and key == "agent" else None,
        sandbox="Native" if configured and key == "sandbox" else None,
        debug=configured if key == "debug" else False,
        parse_error_retries=7 if configured and key == "parse-error-retries" else None,
    )


#: One raw ``[exec]`` TOML value per engine key, paired with the
#: ``ExecConfig`` attribute value ``exec_config_from_merged`` must produce
#: from it. Values are chosen so the hop is meaningful for every key's own
#: parsing (``timeout`` parses a duration string; every other key here is a
#: straight pass-through), never merely a coincidental identity.
_RAW_AND_EXPECTED_BY_KEY: dict[str, tuple[object, object]] = {
    "trace": (True, True),
    "strict-json": (True, True),
    "agent": ('AgentCommand("raw-agent")', 'AgentCommand("raw-agent")'),
    "sandbox": ("Native", "Native"),
    "trace-file": ("raw.jsonl", "raw.jsonl"),
    "timeout": ("5s", 5.0),
    "debug": (True, True),
    "parse-error-retries": (2, 2),
}


@pytest.mark.parametrize("spec", ENGINE_KEYS, ids=lambda spec: spec.name)
def test_exec_config_from_merged_reflects_every_engine_key_config_attr(
    spec: EngineKeySpec,
) -> None:
    """Each ``[exec]`` TOML key reaches its own declared ``ExecConfig`` attribute.

    ``ExecConfig(sandbox=...)`` built directly (as :func:`_config_for`
    does) never runs ``exec_config_from_merged``'s own TOML-key lookup, so a
    typo there (e.g. reading the wrong raw key string) would silently leave
    an attribute at its default with nothing failing. Parametrized over the
    whole catalog, so a future engine key is covered automatically.
    """
    assert spec.config_attr is not None
    raw, expected = _RAW_AND_EXPECTED_BY_KEY[spec.name]

    config = exec_config_from_merged({"exec": {spec.name: raw}})

    assert getattr(config, spec.config_attr) == expected


@pytest.mark.parametrize("key", [spec.name for spec in ENGINE_KEYS])
@pytest.mark.parametrize(
    ("cli_given", "config_given"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_each_engine_key_seed_has_the_same_cli_config_presence_matrix(
    key: str, cli_given: bool, config_given: bool
) -> None:
    """Every catalog key is seeded exactly when a host layer supplied it."""
    primary_table = {key: _CONFIG_RAW_VALUES[key]} if config_given else {}
    cli_values = {key: _CLI_VALUES[key]} if cli_given else {}

    seeds = build_host_engine_seeds(
        config=_config_for(key, config_given),
        primary_table=primary_table,
        cli_values=cli_values,
    ).merged()

    actual_keys = set(seeds)
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
    seeds = build_host_engine_seeds(
        config=_config_for("agent", configured=False),
        primary_table={},
        cli_values={"agent": raw},
    ).merged()

    assert seeds["agent"] == expected


def test_toml_agent_decodes_through_the_same_host_text_dispatch() -> None:
    config = ExecConfig(
        strict_json=False,
        timeout=None,
        trace=False,
        trace_file=None,
        agent="claude/sonnet:experimental",
    )

    seeds = build_host_engine_seeds(
        config=config,
        primary_table={"agent": config.agent},
        cli_values={},
    ).merged()

    assert seeds["agent"] == agent_value("AgentClaude", model="sonnet", thinking="experimental")


def test_config_table_agent_is_json_shaped_data() -> None:
    """A native TOML table decodes as JSON-shaped data, not host text."""
    raw = {"$case": "AgentClaude", "model": "opus", "thinking": "high"}
    config = ExecConfig(strict_json=False, timeout=None, trace=False, trace_file=None, agent=raw)

    seeds = build_host_engine_seeds(
        config=config, primary_table={"agent": raw}, cli_values={}
    ).merged()

    assert seeds["agent"] == agent_value("AgentClaude", model="opus", thinking="high")


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
    config = _config_for("sandbox", configured=False)

    seeds = build_host_engine_seeds(
        config=config, primary_table={}, cli_values={"sandbox": "Sandbox"}
    ).merged()

    assert seeds["sandbox"] == RecordValue(
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
    config = _config_for("sandbox", configured=False)

    seeds = build_host_engine_seeds(
        config=config,
        primary_table={},
        cli_values={"sandbox": 'Sandbox(memory = Some("8G"))'},
    ).merged()

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


def test_invalid_agent_value_exits_before_anything_runs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A decode failure prints an error naming ``--agent`` and exits 1."""
    with pytest.raises(SystemExit) as exc_info:
        build_host_engine_seeds(
            config=_config_for("agent", configured=False),
            primary_table={},
            cli_values={"agent": 'AgentClaude(model = "x"'},
        )
    assert exc_info.value.code == 1
    assert "--agent" in capsys.readouterr().err


def test_invalid_configured_agent_value_exits_naming_the_config_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A malformed program-table value names ``agent``, never the flag or ``[exec]``.

    ``primary_table`` here stands for a resolved ``[prog.main]`` table --
    ``exec_config_from_merged`` has already applied program-table-over-
    ``[exec]`` precedence into ``config.agent`` by the time
    ``build_host_engine_seeds`` runs.
    """
    config = ExecConfig(strict_json=False, timeout=None, trace=False, trace_file=None, agent="   ")

    with pytest.raises(SystemExit) as exc_info:
        build_host_engine_seeds(config=config, primary_table={"agent": "   "}, cli_values={})
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "'agent'" in err
    assert "--agent" not in err
    assert "[exec]" not in err


def test_a_cli_value_shields_a_malformed_configured_value_from_decoding() -> None:
    """A table value the CLI overrides is never decoded, so it cannot fail the run."""
    config = ExecConfig(strict_json=False, timeout=None, trace=False, trace_file=None, agent="   ")

    seeds = build_host_engine_seeds(
        config=config,
        primary_table={"agent": "   "},
        cli_values={"agent": "worker --flag"},
    ).merged()

    assert seeds["agent"] == agent_value("AgentCommand", command="worker --flag")


@pytest.mark.parametrize(
    ("key", "raw_value"),
    [("trace-file", ""), ("trace-file", "  "), ("timeout", ""), ("timeout", 0), ("timeout", -3)],
)
def test_a_blank_option_text_config_value_is_an_explicit_none(key: str, raw_value: object) -> None:
    """A blank or non-positive ``Option[text]`` value is a seeded none, as in ``agm exec``."""
    tiers = build_host_engine_seeds(
        config=exec_config_from_merged({"exec": {key: raw_value}}),
        primary_table={key: raw_value},
        cli_values={},
    )

    seed = tiers.upper[key]
    assert isinstance(seed, RecordValue)
    assert option_text(seed, nominals=NO_BUILTIN_DECLARATIONS) is None


def test_explicit_empty_option_cli_value_remains_a_seed() -> None:
    """``--no-timeout`` is encoded as a present ``None`` value."""
    seeds = build_host_engine_seeds(
        config=_config_for("timeout", configured=False),
        primary_table={},
        cli_values={"timeout": None},
    ).merged()

    assert set(seeds) == {"timeout"}


def test_configured_numeric_timeout_is_seeded_from_its_raw_spelling() -> None:
    """Option settings preserve a valid numeric TOML value as text."""
    seeds = build_host_engine_seeds(
        config=ExecConfig(
            strict_json=False,
            timeout=0.5,
            trace=False,
            trace_file=None,
        ),
        primary_table={"timeout": 0.5},
        cli_values={},
    ).merged()

    assert "timeout" in seeds


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
    ("cli_values", "primary_table"),
    [({"parse-error-retries": -1}, {}), ({}, {"parse-error-retries": -1})],
)
def test_negative_parse_error_retries_exits(
    cli_values: dict[str, object], primary_table: dict[str, object]
) -> None:
    config = exec_config_from_merged({"exec": primary_table})

    with pytest.raises(SystemExit) as exc_info:
        build_host_engine_seeds(config=config, primary_table=primary_table, cli_values=cli_values)

    assert exc_info.value.code == 1


@pytest.mark.parametrize("raw", ["three", True, 1.5])
def test_non_integer_configured_parse_error_retries_exits(raw: object) -> None:
    config = exec_config_from_merged({"exec": {"parse-error-retries": raw}})

    with pytest.raises(SystemExit):
        build_host_engine_seeds(
            config=config, primary_table={"parse-error-retries": raw}, cli_values={}
        )


@pytest.mark.parametrize("key", ["strict-json", "trace", "debug"])
def test_configured_bool_string_decodes_like_a_cli_token(key: str) -> None:
    table: dict[str, object] = {key: "true"}
    config = exec_config_from_merged({"exec": table})

    seeds = build_host_engine_seeds(config=config, primary_table=table, cli_values={})

    assert seeds.upper[key] == BoolValue(True)


@pytest.mark.parametrize("key", ["strict-json", "trace", "debug"])
@pytest.mark.parametrize("raw", ["banana", 1])
def test_undecodable_configured_bool_exits_instead_of_seeding_false(key: str, raw: object) -> None:
    table: dict[str, object] = {key: raw}
    config = exec_config_from_merged({"exec": table})

    with pytest.raises(SystemExit) as exc_info:
        build_host_engine_seeds(config=config, primary_table=table, cli_values={})

    assert exc_info.value.code == 1


def test_zero_parse_error_retries_is_a_valid_seed() -> None:
    seeds = build_host_engine_seeds(
        config=_config_for("parse-error-retries", configured=False),
        primary_table={},
        cli_values={"parse-error-retries": 0},
    ).merged()

    assert seeds["parse-error-retries"] == IntValue(0)


def test_engine_defaults_cover_exactly_the_catalog_keys_with_defaults() -> None:
    """A catalog addition cannot leave interpreter defaults without a value."""
    expected = {spec.name for spec in ENGINE_KEYS if spec.has_default}
    assert set(engine_default_settings()) == expected


# ---------------------------------------------------------------------------
# Tier split: cli (explicit CLI flags) and upper ([exec]) -- independent tiers;
# cli wins the seeded value.
# ---------------------------------------------------------------------------


def test_a_primary_table_value_is_in_the_upper_tier() -> None:
    config = ExecConfig(strict_json=True, timeout=None, trace=False, trace_file=None)

    tiers = build_host_engine_seeds(
        config=config, primary_table={"strict-json": True}, cli_values={}
    )

    assert "strict-json" in tiers.upper


def test_a_cli_value_sits_in_its_own_tier_over_an_undecoded_table_value() -> None:
    """A CLI value sits in ``cli``; the table value it overrides seeds no tier."""
    config = ExecConfig(strict_json=False, timeout=None, trace=False, trace_file=None)

    tiers = build_host_engine_seeds(
        config=config,
        primary_table={"strict-json": True},
        cli_values={"strict-json": False},
    )

    assert "strict-json" in tiers.cli
    assert "strict-json" not in tiers.upper
    assert tiers.merged()["strict-json"] == BoolValue(False)


def test_a_named_trace_value_is_seeded_into_its_tier_like_any_other_key() -> None:
    """``trace`` follows the ordinary tier rule; ``merged`` still recomputes it afterward."""
    config = ExecConfig(strict_json=False, timeout=None, trace=True, trace_file=None)

    upper_tiers = build_host_engine_seeds(
        config=config, primary_table={"trace": True}, cli_values={}
    )
    assert "trace" in upper_tiers.upper

    assert upper_tiers.merged()["trace"] == BoolValue(True)


def test_exec_trace_false_and_trace_file_still_seed_trace_true() -> None:
    """Regression: ``[exec] trace = false`` plus ``trace-file`` still implies ``trace``."""
    config = ExecConfig(strict_json=False, timeout=None, trace=False, trace_file="p")
    tiers = build_host_engine_seeds(
        config=config, primary_table={"trace": False, "trace-file": "p"}, cli_values={}
    )

    assert tiers.merged()["trace"] == BoolValue(True)


@pytest.mark.parametrize(
    "table",
    [{"trace-file": "   "}, {"trace": False, "trace-file": "trace.jsonl"}],
    ids=["whitespace_only_trace_file", "trace_false_and_trace_file"],
)
def test_merged_trace_matches_config_trace_or_trace_file_is_set(table: dict[str, object]) -> None:
    """``merged()`` names ``trace`` as ``cfg.trace or cfg.trace_file is not None`` when configured.

    Builds ``cfg`` through ``exec_config_from_merged`` -- the same path a real
    caller uses -- rather than a hand-built ``ExecConfig``, so this pins the
    derived rule against the real config layering. A fully invalid/unnamed
    combination leaves ``trace`` absent instead of an explicit ``false`` --
    the same effective setting, since that is the builtin default.
    """
    config = exec_config_from_merged({"exec": table})

    tiers = build_host_engine_seeds(config=config, primary_table=table, cli_values={})

    assert tiers.merged().get("trace", BoolValue(False)) == BoolValue(
        config.trace or config.trace_file is not None
    )


def test_trace_stays_absent_when_neither_trace_nor_trace_file_is_configured_anywhere() -> None:
    config = exec_config_from_merged({"exec": {}})

    tiers = build_host_engine_seeds(config=config, primary_table={}, cli_values={})

    assert "trace" not in tiers.merged()
