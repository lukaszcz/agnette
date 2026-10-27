"""Ranking and decoding of the engine settings an AgL host seeds.

``resolve_engine_settings`` picks each setting's winning source, decodes only that value
and records its tier; ``trace_decision`` reads the derived ``trace`` setting off those tiers.
"""

from __future__ import annotations

import pytest

from agnette.agl.ir.builtin_nominals import NO_BUILTIN_DECLARATIONS
from agnette.agl.ir.static_keys import StaticBindingKey, static_binding_key
from agnette.agl.modules.ids import STD_CONFIG_ID
from agnette.agl.runtime.arguments import OptionSome, ParamTier, option_none_raw
from agnette.agl.runtime.engine_config import (
    EngineSettings,
    EngineValueFailure,
    resolve_engine_settings,
    split_engine_values,
)
from agnette.agl.semantics.values import BoolValue, IntValue, Value
from agnette.agm.cli_support.engine_seeds import engine_seed_values, trace_decision
from agnette.core.log import TraceDecision

_Tiers = list[tuple[ParamTier, dict[str, object]]]


def _key(name: str) -> StaticBindingKey:
    return static_binding_key(STD_CONFIG_ID, (), name)


def _resolve(
    tiers: _Tiers, config: dict[str, Value] | None = None
) -> tuple[EngineSettings, list[EngineValueFailure], list[EngineValueFailure]]:
    """Resolve raw values given by setting name; return the settings and both failure lists."""
    return resolve_engine_settings(
        {tier: {_key(n): raw for n, raw in values.items()} for tier, values in tiers},
        {_key(n): value for n, value in (config or {}).items()},
        nominals=NO_BUILTIN_DECLARATIONS,
    )


class TestWinningTier:
    def test_each_setting_takes_its_highest_tier_and_reports_its_rank(self) -> None:
        settings, configured, usage = _resolve(
            [
                (ParamTier.DECLARED, {"strict-json": False, "trace": False}),
                (ParamTier.INHERITED, {"strict-json": True}),
                (ParamTier.CONFIGURED, {"trace": True}),
                (ParamTier.SUPPLIED, {"debug": True}),
            ],
            {"strict-json": BoolValue(False)},
        )

        assert settings.values == {
            "strict-json": BoolValue(True),
            "trace": BoolValue(True),
            "debug": BoolValue(True),
        }
        assert settings.ranks == {
            "strict-json": ParamTier.INHERITED,
            "trace": ParamTier.CONFIGURED,
            "debug": ParamTier.SUPPLIED,
        }
        assert usage == configured == []

    def test_a_config_attribute_value_outranks_the_declared_tier_only(self) -> None:
        settings, _, _ = _resolve(
            [(ParamTier.DECLARED, {"trace": False}), (ParamTier.CONFIGURED, {"debug": False})],
            {"trace": BoolValue(True), "debug": BoolValue(True)},
        )

        assert settings.ranks == {"trace": ParamTier.CONFIG, "debug": ParamTier.CONFIGURED}

    def test_a_setting_no_tier_sets_is_absent(self) -> None:
        settings, _, _ = _resolve([(ParamTier.DECLARED, {})])

        assert settings == EngineSettings({}, {})


class TestOptionTextSettings:
    def test_a_supplied_blank_value_is_a_usage_failure(self) -> None:
        settings, configured, usage = _resolve([(ParamTier.SUPPLIED, {"timeout": OptionSome("")})])

        assert settings == EngineSettings({}, {})
        assert len(usage) == 1
        assert configured == []

    def test_a_none_envelope_unsets_a_lower_tier(self) -> None:
        settings, configured, usage = _resolve(
            [
                (ParamTier.DECLARED, {"timeout": OptionSome("1s")}),
                (ParamTier.SUPPLIED, {"timeout": option_none_raw()}),
            ]
        )

        assert settings.ranks == {"timeout": ParamTier.SUPPLIED}
        assert usage == configured == []

    def test_a_configured_none_envelope_masks_a_lower_tier_and_records_its_rank(self) -> None:
        settings, configured, usage = _resolve(
            [
                (ParamTier.DECLARED, {"timeout": OptionSome("1s")}),
                (ParamTier.CONFIGURED, {"timeout": option_none_raw()}),
            ]
        )

        assert settings.ranks == {"timeout": ParamTier.CONFIGURED}
        assert usage == configured == []


class TestFailures:
    def test_failures_split_by_whether_the_command_line_supplied_the_value(self) -> None:
        settings, configured, usage = _resolve(
            [
                (ParamTier.DECLARED, {"timeout": OptionSome("forever")}),
                (ParamTier.SUPPLIED, {"strict-json": "maybe"}),
            ]
        )

        assert settings == EngineSettings({}, {})
        assert len(usage) == 1
        assert len(configured) == 1

    def test_a_failing_config_attribute_value_is_reported_even_when_overridden(self) -> None:
        settings, configured, usage = _resolve(
            [(ParamTier.SUPPLIED, {"parse-error-retries": "2"})],
            {"parse-error-retries": IntValue(-1)},
        )

        assert settings.values == {"parse-error-retries": IntValue(2)}
        assert usage == []
        assert len(configured) == 1


class TestSplitEngineValues:
    def test_engine_values_are_separated_from_the_rest(self) -> None:
        from agnette.agl.ir.static_keys import FunctionParamKey
        from agnette.agl.modules.ids import ModuleId

        other = FunctionParamKey(ModuleId.from_path("m"), (), "f", "x")
        others, engine = split_engine_values({_key("timeout"): 1, other: 2})

        assert others == {other: 2}
        assert {key[2]: value for key, value in engine.items()} == {"timeout": 1}


def _settings(values: dict[str, Value], ranks: dict[str, ParamTier]) -> EngineSettings:
    return EngineSettings(values, ranks)


class TestTraceDecision:
    def test_unconfigured_tracing_is_off_and_seeds_nothing(self) -> None:
        settings = _settings({}, {})

        assert trace_decision(settings) == TraceDecision(enabled=False, explicit_path=None)
        assert "trace" not in engine_seed_values(settings)

    def test_debug_turns_tracing_on(self) -> None:
        settings = _settings({"debug": BoolValue(True)}, {"debug": ParamTier.DECLARED})

        assert trace_decision(settings).enabled
        assert engine_seed_values(settings)["trace"] == BoolValue(True)

    def test_a_supplied_trace_decides_alone(self) -> None:
        settings = _settings(
            {"trace": BoolValue(False), "debug": BoolValue(True)},
            {"trace": ParamTier.SUPPLIED, "debug": ParamTier.SUPPLIED},
        )

        assert not trace_decision(settings).enabled

    def test_a_supplied_trace_file_enables_tracing_over_a_configured_trace_false(self) -> None:
        settings, _, _ = _resolve(
            [
                (ParamTier.CONFIGURED, {"trace": False}),
                (ParamTier.SUPPLIED, {"trace-file": OptionSome("out.jsonl")}),
            ]
        )

        assert trace_decision(settings) == TraceDecision(enabled=True, explicit_path="out.jsonl")

    def test_a_configured_trace_file_enables_tracing_beneath_a_trace_false(self) -> None:
        settings, _, _ = _resolve(
            [
                (ParamTier.DECLARED, {"trace-file": OptionSome("t.jsonl")}),
                (ParamTier.CONFIGURED, {"trace": False}),
            ]
        )

        assert trace_decision(settings) == TraceDecision(enabled=True, explicit_path="t.jsonl")

    def test_a_configured_trace_false_alone_keeps_tracing_off(self) -> None:
        settings, _, _ = _resolve([(ParamTier.CONFIGURED, {"trace": False})])

        assert trace_decision(settings) == TraceDecision(enabled=False, explicit_path=None)

    def test_a_none_trace_file_counts_as_a_configured_trace_setting(self) -> None:
        settings, _, _ = _resolve(
            [
                (ParamTier.DECLARED, {"debug": True}),
                (ParamTier.CONFIGURED, {"trace-file": option_none_raw()}),
            ]
        )

        assert trace_decision(settings) == TraceDecision(enabled=False, explicit_path=None)


@pytest.mark.parametrize(
    "tiers",
    [
        [],
        [(ParamTier.DECLARED, {"debug": True})],
        [(ParamTier.DECLARED, {"trace": True})],
        [(ParamTier.CONFIGURED, {"trace": False}), (ParamTier.DECLARED, {"debug": True})],
        [(ParamTier.DECLARED, {"trace-file": OptionSome("t.jsonl")})],
        [(ParamTier.CONFIGURED, {"trace-file": option_none_raw()})],
        [
            (ParamTier.CONFIGURED, {"trace-file": OptionSome("t.jsonl")}),
            (ParamTier.SUPPLIED, {"trace": False}),
        ],
        [
            (ParamTier.DECLARED, {"trace": True}),
            (ParamTier.SUPPLIED, {"trace-file": option_none_raw()}),
        ],
    ],
)
def test_the_seeded_trace_always_agrees_with_the_trace_decision(tiers: _Tiers) -> None:
    settings, _, _ = _resolve(tiers)

    assert trace_decision(settings).enabled == (
        engine_seed_values(settings).get("trace") == BoolValue(True)
    )
