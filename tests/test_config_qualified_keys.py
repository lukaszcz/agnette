"""Tests for the config tables a route addresses, read through the claims index."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from agnette.config.claims import QualifiedConfigLookupError
from agnette.config.general import PathAnchor, load_general_config
from agnette.config.qualified_keys import (
    ConfigRoute,
    known_route_tables,
    resolve_manifest_leaf,
    resolve_route_leaf,
    route_table_paths,
)
from agnette.core.toml import TomlDict
from tests._config_helpers import unanchored_config as _config

_REVIEW = ConfigRoute(("review-tools", "judge"), ("review",))
_MAIN = ConfigRoute(("tools", "review"), ("main",), (("devel", "review"),))


def _value(route: ConfigRoute, leaf: str, *layers: Mapping[str, object]) -> object:
    """Return the value of *leaf* of *route* across file *layers*, ``None`` when unset."""
    found = resolve_route_leaf(_config(*layers), route, leaf)
    return None if found is None else found.value


def _manifest_value(route: ConfigRoute, leaf: str, manifest: TomlDict) -> object:
    found = resolve_manifest_leaf(manifest, route, leaf)
    return None if found is None else found.value


class TestRouteTables:
    @pytest.mark.parametrize(
        ("table", "expected"),
        [
            ({"judge": {"review": {"max-tries": 1}}}, 1),
            ({"tools": {"judge": {"review": {"max-tries": 2}}}}, 2),
            ({"review-tools": {"tools": {"judge": {"review": {"max-tries": 3}}}}}, 3),
        ],
    )
    def test_resolves_unambiguous_module_suffix_at_each_depth(
        self, table: dict[str, object], expected: int
    ) -> None:
        route = ConfigRoute(("review-tools", "tools", "judge"), ("review",))

        assert _value(route, "max-tries", table) == expected

    def test_quoted_module_route_is_an_exact_anchor(self) -> None:
        other = ConfigRoute(("quality-tools", "judge"), ("review",))
        config = {"review-tools/judge": {"review": {"max-tries": 4}}}

        assert _value(_REVIEW, "max-tries", config) == 4
        assert _value(other, "max-tries", config) is None

    def test_names_a_quoted_anchor_in_a_same_layer_conflict(self) -> None:
        config = {
            "judge": {"review": {"max-tries": 1}},
            "review-tools/judge": {"review": {"max-tries": 2}},
        }

        with pytest.raises(QualifiedConfigLookupError) as exc_info:
            _value(_REVIEW, "max-tries", config)

        assert '["review-tools/judge"].review' in str(exc_info.value)

    @pytest.mark.parametrize(
        "reserved_name",
        ("exec", "wsp", "wt", "deps", "modules", "packages", "params"),
    )
    def test_reserved_sections_are_not_unscoped_config_modules(self, reserved_name: str) -> None:
        route = ConfigRoute((reserved_name,), ())

        assert _value(route, "max-tries", {reserved_name: {"max-tries": 1}}) is None

    @pytest.mark.parametrize("schema_name", ("deps", "modules", "packages", "params"))
    def test_schema_sections_are_not_config_modules_at_any_depth(self, schema_name: str) -> None:
        """A section keyed by AgM's own schema never doubles as a program table."""
        route = ConfigRoute((schema_name,), ("main",))

        assert _value(route, "region", {schema_name: {"main": {"region": "configured"}}}) is None

    @pytest.mark.parametrize("command_name", ("exec", "wsp", "wt"))
    def test_command_sections_still_serve_a_loose_files_program_table(
        self, command_name: str
    ) -> None:
        route = ConfigRoute((command_name,), ("main",))

        config = {command_name: {"main": {"region": "configured"}}}
        assert _value(route, "region", config) == "configured"

    def test_legacy_params_subtable_cannot_route_to_a_params_module(self) -> None:
        route = ConfigRoute(("params", "workflow"), ())

        assert _value(route, "region", {"params": {"workflow": {"region": "legacy"}}}) is None

    def test_returns_no_value_when_no_table_matches(self) -> None:
        assert _value(_REVIEW, "max-tries", {"other": {"review": {"max-tries": 1}}}) is None

    def test_ignores_malformed_non_table_paths(self) -> None:
        assert _value(_REVIEW, "max-tries", {"judge": "not a table"}) is None

    def test_route_table_paths_share_a_table_between_a_suffix_and_its_module_route(self) -> None:
        """A nested module route reads the table its last segment names."""
        entry = route_table_paths(("demo",))
        imported = route_table_paths(("lib", "demo"))

        assert entry == (("demo",),)
        assert set(entry).issubset(imported)

    def test_scope_region_route_includes_suffixes_and_the_quoted_module_anchor(self) -> None:
        """A scope-region leaf reads the same qualified table spellings as its binding."""
        assert route_table_paths(("A", "logging"), ("debug",)) == (
            ("logging", "debug"),
            ("A", "logging", "debug"),
            ("A/logging", "debug"),
        )

    def test_resolves_a_registered_command_path_table(self) -> None:
        """A command path addresses the program its package registers under it."""
        route = ConfigRoute(("review-tools", "review"), ("main",), (("dv", "review"),))

        assert _value(route, "strict", {"dv": {"review": {"strict": True}}}) is True

    def test_rejects_a_command_path_table_conflicting_with_its_module_route(self) -> None:
        route = ConfigRoute(("review-tools", "judge"), ("main",), (("dv", "review"),))
        config = {"dv": {"review": {"strict": True}}, "judge": {"main": {"strict": False}}}

        with pytest.raises(QualifiedConfigLookupError):
            _value(route, "strict", config)

    def test_a_later_layer_command_path_table_overrides_a_module_route(self) -> None:
        route = ConfigRoute(("review-tools", "judge"), ("main",), (("dv", "review"),))

        assert (
            _value(
                route,
                "strict",
                {"judge": {"main": {"strict": False}}},
                {"dv": {"review": {"strict": True}}},
            )
            is True
        )

    def test_resolves_every_command_path_registered_for_one_program(self) -> None:
        """Two registrations of one program are two spellings of its address."""
        route = ConfigRoute(("tools", "run"), ("main",), (("dv", "run"), ("dv", "r")))

        assert _value(route, "level", {"dv": {"r": {"level": "high"}}}) == "high"

    def test_command_path_table_does_not_duplicate_a_module_route_spelling(self) -> None:
        """A command path that coincides with a module route stays one table."""
        route = ConfigRoute(("tools", "judge"), ("main",), (("judge", "main"),))

        assert (
            route_table_paths(("tools", "judge"), ("main",), (("judge", "main"),)).count(
                ("judge", "main")
            )
            == 1
        )
        assert _value(route, "level", {"judge": {"main": {"level": "high"}}}) == "high"

    def test_a_schema_rooted_command_path_has_no_config_table(self) -> None:
        """A command registered under a schema section (``agent review``) reads no
        ``[agent.review]`` or inherited ``[agent]`` table, which belong to AgM's schema."""
        route = ConfigRoute(("tools", "review"), ("main",), (("agent", "review"),))
        config = {"agent": {"strict": True, "review": {"strict": True}}}

        assert ("agent", "review") not in route.exact_table_paths()
        assert _value(route, "strict", config) is None

    def test_resolves_file_layers_with_dotted_and_quoted_headers_and_their_anchors(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        cwd = tmp_path / "workspace"
        home_config = home / ".agm"
        cwd_config = cwd / ".agm"
        home_config.mkdir(parents=True)
        cwd_config.mkdir(parents=True)
        (home_config / "config.toml").write_text(
            "[judge.review]\ntrace-file = 'lower.log'\n", encoding="utf-8"
        )
        (cwd_config / "config.toml").write_text(
            "[\"review-tools/judge\".review]\ntrace-file = 'higher.log'\n",
            encoding="utf-8",
        )

        config = load_general_config(home=home, proj_dir=None, cwd=cwd)

        assert config.layers == (
            {"judge": {"review": {"trace-file": "lower.log"}}},
            {"review-tools/judge": {"review": {"trace-file": "higher.log"}}},
        )
        found = resolve_route_leaf(config, _REVIEW, "trace-file")
        assert found is not None
        assert (found.value, found.anchor) == ("higher.log", PathAnchor(cwd_config, cwd))


class TestGroupTableInheritance:
    """A command path's proper prefixes are inherited config tables."""

    def test_a_group_table_feeds_a_command_beneath_it(self) -> None:
        assert _value(_MAIN, "strict", {"devel": {"strict": True}}) is True

    def test_a_deeper_group_table_wins_over_a_shallower_one(self) -> None:
        route = ConfigRoute(("tools", "review"), ("main",), (("devel", "sub", "review"),))
        config = {"devel": {"strict": False, "sub": {"strict": True}}}

        assert _value(route, "strict", config) is True

    def test_alias_prefixes_are_already_expanded_command_paths(self) -> None:
        """An alias's own group table (``[dv]``) inherits down just like the canonical one."""
        route = ConfigRoute(("tools", "review"), ("main",), (("dv", "review"), ("devel", "review")))

        assert _value(route, "strict", {"dv": {"strict": True}}) is True

    def test_same_depth_conflict_from_two_command_paths_in_one_layer_errors(self) -> None:
        route = ConfigRoute(
            ("tools", "review"), ("main",), (("devel", "review"), ("staging", "review"))
        )
        config = {"devel": {"strict": True}, "staging": {"strict": False}}

        with pytest.raises(QualifiedConfigLookupError):
            _value(route, "strict", config)

    def test_no_command_paths_means_no_inheritance(self) -> None:
        route = ConfigRoute(("tools", "review"), ("main",))

        assert _value(route, "strict", {"tools": {"strict": True}}) is None


class TestManifestTiers:
    """A package manifest ``[config]`` table resolves command > groups > root."""

    def test_a_command_table_wins_over_a_group_table(self) -> None:
        manifest = {"devel": {"strict": False, "review": {"strict": True}}}

        assert _manifest_value(_MAIN, "strict", manifest) is True

    def test_a_group_table_wins_over_the_root(self) -> None:
        manifest = {"strict": False, "devel": {"strict": True}}

        assert _manifest_value(_MAIN, "strict", manifest) is True

    def test_a_deeper_group_table_wins_over_a_shallower_one(self) -> None:
        route = ConfigRoute(("tools", "review"), ("main",), (("devel", "sub", "review"),))
        manifest = {"devel": {"strict": False, "sub": {"strict": True}}}

        assert _manifest_value(route, "strict", manifest) is True

    def test_a_program_with_no_command_paths_reads_only_the_root(self) -> None:
        route = ConfigRoute(("tools", "review"), ("main",))
        manifest = {"strict": True, "devel": {"strict": False}}

        assert _manifest_value(route, "strict", manifest) is True

    def test_no_root_and_no_command_table_resolves_nothing(self) -> None:
        assert _manifest_value(_MAIN, "strict", {"devel": {"other": 1}}) is None

    def test_a_module_suffix_route_never_addresses_a_manifest_table(self) -> None:
        """Only command paths and the root apply; a bare module name is not a route."""
        route = ConfigRoute(("tools", "review"), ("main",))

        assert _manifest_value(route, "strict", {"tools": {"strict": True}}) is None


class TestSectionRoutes:
    """A section route addresses one fixed top-level table, whatever else a route would add."""

    def test_a_section_leaf_is_read_from_its_one_table(self) -> None:
        route = ConfigRoute.in_section("exec")
        config = {"exec": {"timeout": "30s"}, "timeout": "5s"}

        assert route.exact_table_paths() == (("exec",),)
        assert route.table_paths() == (("exec",),)
        assert _value(route, "timeout", config) == "30s"

    def test_a_section_is_a_route_table_and_not_a_leaf_value(self) -> None:
        route = ConfigRoute.in_section("exec")

        assert known_route_tables((route,)) == frozenset({("exec",)})
