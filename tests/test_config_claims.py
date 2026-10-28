"""Tests for the claims index and the config sources it is read against."""

from __future__ import annotations

import pytest

from agnette.config.claims import (
    Claimant,
    ClaimsIndex,
    ConfigSource,
    QualifiedConfigLookupError,
    resolve_leaf,
)
from agnette.core.toml import TomlDict
from tests._config_helpers import unanchored_config as _config

_EXACT = (("a", "run"),)
_GROUP = (("a",),)
_NO_ROUTES: frozenset[tuple[str, ...]] = frozenset()


def _source(*layers: TomlDict, nested: frozenset[tuple[str, ...]] = _NO_ROUTES) -> ConfigSource:
    return ConfigSource.from_config(_config(*layers), nested)


def _param(label: str) -> Claimant:
    """A claimant of the parameter *label*."""
    return Claimant(label, "route", label)


class TestConfigSource:
    """A source lists the leaves its layers set, skipping route tables."""

    def test_a_nested_route_table_is_no_leaf_and_an_unknown_table_is_a_value(self) -> None:
        layer = {"a": {"msg": "hi", "route": {"x": 1}, "opts": {"y": 2}}}
        routed = _source(layer, nested=frozenset({("a", "route")}))

        assert list(routed.set_leaves(_GROUP)) == [("msg", ("a",)), ("opts", ("a",))]

    def test_the_first_table_a_layer_scan_meets_a_leaf_in_is_kept(self) -> None:
        source = _source({"b": {"k": 1}}, {"a": {"k": 2}, "b": {"k": 3}})

        assert source.first_tables((("a",), ("b",))) == {"k": ("b",)}

    def test_a_missing_or_non_table_path_sets_nothing(self) -> None:
        source = _source({"a": "scalar"})

        assert list(source.set_leaves((("a",), ("a", "run"), ("z",)))) == []
        assert not source.is_set(("a",), "k")

    def test_a_manifest_source_holds_the_config_table_under_its_prefix(self) -> None:
        source = ConfigSource.from_manifest({"dv": {"k": 1}}, None, _NO_ROUTES)

        assert source.manifest
        assert source.first_tables((("config", "dv"),)) == {"k": ("config", "dv")}


class TestAmbiguity:
    """A set leaf several distinct parameters claim is ambiguous, whatever the tiers."""

    def test_distinct_parameters_on_one_leaf_are_ambiguous_only_when_it_is_set(self) -> None:
        index = ClaimsIndex()
        index.claim(_param("first"), (_EXACT,), ("k",))
        index.claim(None, (_EXACT,), ("k",))
        index.claim(_param("second"), (_EXACT,), ("k",))

        index.reject_ambiguities(_source({"a": {"run": {"other": 1}}}))
        with pytest.raises(QualifiedConfigLookupError) as exc_info:
            index.reject_ambiguities(_source({"a": {"run": {"k": 1}}}))

        assert "first" in str(exc_info.value)
        assert "second" in str(exc_info.value)

    def test_a_claim_in_an_inherited_tier_is_as_ambiguous_as_an_exact_one(self) -> None:
        index = ClaimsIndex()
        index.claim(_param("first"), (_EXACT, _GROUP), ("k",))
        index.claim(_param("second"), ((("b",),), _GROUP), ("k",))

        with pytest.raises(QualifiedConfigLookupError):
            index.reject_ambiguities(_source({"a": {"k": 1}}))

    def test_one_parameter_claiming_through_several_routes_is_not_ambiguous(self) -> None:
        index = ClaimsIndex()
        index.claim(Claimant("p", "program", "p program route"), (_EXACT,), ("k",))
        index.claim(Claimant("p", "declaration", "p declaration route"), (_EXACT,), ("k",))

        index.reject_ambiguities(_source({"a": {"run": {"k": 1}}}))

    def test_a_declare_only_claim_is_never_ambiguous(self) -> None:
        index = ClaimsIndex()
        index.claim(_param("real"), (_EXACT,), ("k",))
        index.claim(None, (_EXACT,), ("k",))

        index.reject_ambiguities(_source({"a": {"run": {"k": 1}}}))

    def test_manifest_claims_are_checked_against_the_manifest_source_only(self) -> None:
        index = ClaimsIndex()
        index.claim(_param("first"), ((("config",),),), ("k",), manifest=True)
        index.claim(_param("second"), ((("config",),),), ("k",), manifest=True)

        index.reject_ambiguities(_source({"config": {"k": 1}}))
        with pytest.raises(QualifiedConfigLookupError):
            index.reject_ambiguities(ConfigSource.from_manifest({"k": 1}, None, _NO_ROUTES))


class TestConsumed:
    """A set leaf is declared when some claim on its table consumes it."""

    def test_a_declare_only_claim_consumes_a_leaf(self) -> None:
        index = ClaimsIndex()
        index.claim(None, (_EXACT,), ("k",))
        source = _source()

        assert index.consumed(source, ("a", "run"), "k")
        assert not index.consumed(source, ("a", "run"), "other")
        assert not index.consumed(source, ("a",), "k")

    def test_a_declare_only_claim_on_another_commands_exact_table_declares_nothing(self) -> None:
        index = ClaimsIndex()
        index.claim(
            None,
            (_EXACT, _GROUP),
            ("k",),
            foreign=frozenset({("a", "run"), ("a",)}),
        )
        source = _source()

        assert not index.consumed(source, ("a", "run"), "k")
        assert index.consumed(source, ("a",), "k")

    def test_a_feeding_claim_consumes_a_leaf_of_another_commands_exact_table(self) -> None:
        index = ClaimsIndex()
        index.claim(_param("p"), (_EXACT,), ("k",), foreign=frozenset({("a", "run")}))

        assert index.consumed(_source(), ("a", "run"), "k")

    def test_the_manifest_claims_are_a_separate_domain(self) -> None:
        index = ClaimsIndex()
        index.claim(_param("p"), ((("config",),),), ("k",), manifest=True)

        assert index.consumed(ConfigSource.from_manifest({}, None, _NO_ROUTES), ("config",), "k")
        assert not index.consumed(_source(), ("config",), "k")


class TestValues:
    """Each claimant with a subject resolves its own leaf tier by tier across the layers."""

    def test_an_exact_tier_beats_an_inherited_tier_from_any_layer(self) -> None:
        index = ClaimsIndex()
        claimant = _param("p")
        index.claim(claimant, (_EXACT, _GROUP), ("k",))

        resolved = index.resolve(_source({"a": {"run": {"k": 1}}}, {"a": {"k": 2}}))

        assert resolved[claimant].value == 1

    def test_a_declare_only_claim_resolves_nothing(self) -> None:
        index = ClaimsIndex()
        index.claim(None, (_EXACT,), ("k",))

        assert index.resolve(_source({"a": {"run": {"k": 1}}})) == {}

    def test_peers_on_distinct_leaves_each_take_their_own(self) -> None:
        index = ClaimsIndex()
        first, second = _param("first"), _param("second")
        index.claim(first, (_EXACT,), ("k", "alias"))
        index.claim(second, (_EXACT,), ("other",))

        resolved = index.resolve(_source({"a": {"run": {"alias": 1, "other": 2}}}))

        assert {c.label: v.value for c, v in resolved.items()} == {"first": 1, "second": 2}

    def test_two_spellings_of_one_claimant_in_a_tier_and_layer_conflict(self) -> None:
        index = ClaimsIndex()
        index.claim(_param("p"), (_EXACT,), ("k", "alias"))

        with pytest.raises(QualifiedConfigLookupError):
            index.resolve(_source({"a": {"run": {"k": 1, "alias": 2}}}))

    def test_two_spellings_in_the_manifest_root_conflict(self) -> None:
        index = ClaimsIndex()
        index.claim(_param("p"), ((("config",),),), ("verbose", "logging.verbose"), manifest=True)
        manifest = ConfigSource.from_manifest(
            {"verbose": True, "logging.verbose": False}, None, _NO_ROUTES
        )

        with pytest.raises(QualifiedConfigLookupError):
            index.resolve(manifest)

    def test_a_conflict_in_a_shadowed_tier_still_errors(self) -> None:
        index = ClaimsIndex()
        index.claim(_param("p"), (_EXACT, _GROUP), ("k", "alias"))

        with pytest.raises(QualifiedConfigLookupError):
            index.resolve(_source({"a": {"run": {"k": 1}}}, {"a": {"k": 1, "alias": 2}}))

    def test_a_later_layer_replacing_the_table_drops_the_earlier_value(self) -> None:
        index = ClaimsIndex()
        index.claim(_param("p"), (_EXACT,), ("k",))

        assert index.resolve(_source({"a": {"run": {"k": 1}}}, {"a": {"run": "off"}})) == {}

    def test_a_later_layer_omitting_the_leaf_keeps_the_earlier_value(self) -> None:
        index = ClaimsIndex()
        claimant = _param("p")
        index.claim(claimant, (_EXACT,), ("k",))

        resolved = index.resolve(_source({"a": {"run": {"k": 1}}}, {"a": {"run": {"other": 2}}}))

        assert resolved[claimant].value == 1

    def test_a_later_layer_replacing_the_exact_table_falls_through_to_the_inherited_tier(
        self,
    ) -> None:
        index = ClaimsIndex()
        claimant = _param("p")
        index.claim(claimant, (_EXACT, _GROUP), ("k",))

        resolved = index.resolve(_source({"a": {"run": {"k": 1}, "k": 2}}, {"a": {"run": "off"}}))

        assert resolved[claimant].value == 2


class TestResolveLeaf:
    """One leaf over a route's tiers, in the file or the manifest domain."""

    def test_a_file_source_resolves_tier_by_tier(self) -> None:
        source = _source({"a": {"k": 2}}, {"a": {"run": {"k": 1}}})

        found = resolve_leaf(source, (_EXACT, _GROUP), "k", "label")

        assert found is not None
        assert found.value == 1
        assert resolve_leaf(source, (_EXACT, _GROUP), "other", "label") is None

    def test_a_manifest_source_resolves_its_config_tables(self) -> None:
        source = ConfigSource.from_manifest({"a": {"k": 1}}, None, _NO_ROUTES)

        found = resolve_leaf(source, ((("config", "a"),), (("config",),)), "k", "label")

        assert found is not None
        assert found.value == 1
