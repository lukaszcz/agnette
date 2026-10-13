"""Warning levels from ``[warnings]`` configuration and ``--warning`` flags."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from agnette.agl.warning_policy import WarningPolicy
from agnette.agl.warnings import (
    WARNING_CATALOG,
    WARNING_LEVELS,
    WarningLevel,
    warning_level_setting,
)
from agnette.agm import completion
from agnette.agm.cli_support.warning_levels import (
    WarningFlagError,
    advisory_warning_policy,
    command_warning_policy,
    parse_warning_flags,
    resolve_warning_policy,
)
from agnette.config.context import ConfigContext
from agnette.config.general import load_merged_config, warning_levels_from_merged

_NAME = "tab-indentation"
_OTHER = "noop-parse-retries"


def _context(tmp_path: Path, *, home_toml: str = "", cwd_toml: str = "") -> ConfigContext:
    home = tmp_path / "home"
    (home / ".agm").mkdir(parents=True)
    (home / ".agm" / "config.toml").write_text(home_toml, encoding="utf-8")
    cwd = tmp_path / "cwd"
    (cwd / ".agm").mkdir(parents=True)
    (cwd / ".agm" / "config.toml").write_text(cwd_toml, encoding="utf-8")
    return ConfigContext(home=home, proj_dir=None, cwd=cwd)


def test_the_level_tuple_lists_every_level_of_the_type() -> None:
    assert set(WARNING_LEVELS) == set(get_args(WarningLevel.__value__))


class TestLevelSetting:
    def test_a_catalog_name_and_a_level_validate(self) -> None:
        assert warning_level_setting(_NAME, "deny") == "deny"

    @pytest.mark.parametrize("name", ["no-such-warning", ""])
    def test_an_unknown_name_is_rejected(self, name: str) -> None:
        with pytest.raises(ValueError, match="unknown warning"):
            warning_level_setting(name, "deny")

    @pytest.mark.parametrize("level", ["loud", "", 1, True, None])
    def test_a_non_level_is_rejected_listing_the_levels(self, level: object) -> None:
        with pytest.raises(ValueError, match="allow, warn, deny"):
            warning_level_setting(_NAME, level)


class TestFlagParsing:
    def test_flags_set_levels_by_name(self) -> None:
        levels = parse_warning_flags([f"{_NAME}=deny", f"{_OTHER}=allow"])

        assert levels == {_NAME: "deny", _OTHER: "allow"}

    def test_repeating_a_name_with_the_same_level_is_accepted(self) -> None:
        assert parse_warning_flags([f"{_NAME}=warn", f"{_NAME}=warn"]) == {_NAME: "warn"}

    @pytest.mark.parametrize(
        "flags",
        [
            ["no-such-warning=deny"],
            [f"{_NAME}=loud"],
            [_NAME],
            [f"{_NAME}="],
            [f"{_NAME}=deny", f"{_NAME}=allow"],
        ],
    )
    def test_invalid_flags_are_rejected(self, flags: list[str]) -> None:
        with pytest.raises(WarningFlagError):
            parse_warning_flags(flags)


class TestConfigReading:
    def test_the_table_sets_levels_by_name(self) -> None:
        merged = {"warnings": {_NAME: "allow", _OTHER: "deny"}}

        assert warning_levels_from_merged(merged) == {_NAME: "allow", _OTHER: "deny"}

    def test_a_missing_table_sets_nothing(self) -> None:
        assert warning_levels_from_merged({}) == {}

    @pytest.mark.parametrize(
        "table",
        [{"no-such-warning": "deny"}, {_NAME: "loud"}, {_NAME: 1}, {_NAME: True}, "deny"],
    )
    def test_invalid_tables_are_errors(self, table: object) -> None:
        with pytest.raises(ValueError, match="warnings"):
            warning_levels_from_merged({"warnings": table})

    def test_layers_merge_by_key_with_the_more_specific_layer_winning(self, tmp_path: Path) -> None:
        context = _context(
            tmp_path,
            home_toml=f'[warnings]\n{_NAME} = "deny"\n{_OTHER} = "allow"\n',
            cwd_toml=f'[warnings]\n{_NAME} = "allow"\n',
        )
        merged = load_merged_config(home=context.home, proj_dir=None, cwd=context.cwd)

        assert warning_levels_from_merged(merged) == {_NAME: "allow", _OTHER: "allow"}


class TestResolution:
    def test_unset_names_keep_their_catalog_default(self) -> None:
        policy = resolve_warning_policy({})

        assert all(policy.level(name) == "warn" for name in WARNING_CATALOG)

    def test_config_overrides_the_default(self) -> None:
        policy = resolve_warning_policy({"warnings": {_NAME: "deny"}})

        assert policy.level(_NAME) == "deny"
        assert policy.level(_OTHER) == "warn"

    def test_flags_override_config_per_name(self) -> None:
        merged = {"warnings": {_NAME: "deny", _OTHER: "allow"}}

        policy = resolve_warning_policy(merged, [f"{_NAME}=allow"])

        assert policy.level(_NAME) == "allow"
        assert policy.level(_OTHER) == "allow"

    def test_equal_inputs_resolve_to_equal_policies(self) -> None:
        merged = {"warnings": {_NAME: "deny"}}

        assert resolve_warning_policy(merged, [f"{_OTHER}=allow"]) == resolve_warning_policy(
            merged, [f"{_OTHER}=allow"]
        )


class TestHostPolicies:
    def test_command_policy_layers_flags_over_the_configured_context(self, tmp_path: Path) -> None:
        context = _context(tmp_path, home_toml=f'[warnings]\n{_NAME} = "deny"\n')

        configured = command_warning_policy(["exec"], context=context)
        flagged = command_warning_policy(["exec"], [f"{_NAME}=allow"], context=context)

        assert configured.level(_NAME) == "deny"
        assert flagged.level(_NAME) == "allow"

    def test_an_invalid_flag_is_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as raised:
            command_warning_policy(["check"], ["bogus=deny"], context=_context(tmp_path))

        assert raised.value.code == 1
        assert "bogus" in capsys.readouterr().err

    def test_an_invalid_configuration_is_a_host_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        context = _context(tmp_path, home_toml='[warnings]\nbogus = "deny"\n')

        with pytest.raises(SystemExit) as raised:
            command_warning_policy(["exec"], context=context)

        assert raised.value.code == 1
        assert "bogus" in capsys.readouterr().err

    def test_a_malformed_config_file_is_a_host_error_not_a_crash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        context = _context(tmp_path, home_toml="[warnings\nbroken")

        with pytest.raises(SystemExit) as raised:
            command_warning_policy(["exec"], context=context)

        assert raised.value.code == 1
        assert capsys.readouterr().err
        assert advisory_warning_policy(context=context) == WarningPolicy()

    def test_the_advisory_policy_degrades_to_the_default_on_any_error(self, tmp_path: Path) -> None:
        bad_config = _context(tmp_path, home_toml='[warnings]\nbogus = "deny"\n')

        assert advisory_warning_policy(context=bad_config) == WarningPolicy()
        assert advisory_warning_policy(["bogus=deny"], context=bad_config) == WarningPolicy()

    def test_the_advisory_policy_matches_the_command_policy_when_valid(
        self, tmp_path: Path
    ) -> None:
        context = _context(tmp_path, cwd_toml=f'[warnings]\n{_OTHER} = "deny"\n')
        flags = [f"{_NAME}=allow"]

        assert advisory_warning_policy(flags, context=context) == command_warning_policy(
            ["exec"], flags, context=context
        )


class TestCompletion:
    def test_names_complete_to_every_pair(self) -> None:
        assert completion.complete_warning("tab") == [
            f"{_NAME}=allow",
            f"{_NAME}=deny",
            f"{_NAME}=warn",
        ]
        assert set(completion.complete_warning("")) == {
            f"{name}={level}" for name in WARNING_CATALOG for level in WARNING_LEVELS
        }

    def test_levels_complete_after_the_equals_sign(self) -> None:
        assert completion.complete_warning(f"{_NAME}=de") == [f"{_NAME}=deny"]
        assert completion.complete_warning(f"{_NAME}=deny") == [f"{_NAME}=deny"]

    def test_an_unknown_name_completes_nothing(self) -> None:
        assert completion.complete_warning("bogus=") == []
