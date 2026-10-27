"""Tests for module-parameter config routing without a command host."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agnette.agl.attributes import ProgramOptionSpec
from agnette.agl.ir.static_keys import FunctionParamKey, HostParamKey
from agnette.agl.modules.ids import ENTRY_ID, ModuleId
from agnette.agl.runtime.engine_config import engine_setting_params
from agnette.agl.runtime.types import HostParamInfo, ProgramDeclInfo, ProgramParamInfo
from agnette.agl.semantics.types import BoolType, IntType, TextType, Type
from agnette.agl.syntax.spans import SourceSpan
from agnette.agl.zones import ParamZone
from agnette.agm.cli_support.engine_seeds import split_engine_tiers
from agnette.agm.cli_support.param_config import ParamValueTiers, resolve_param_values
from agnette.agm.cli_support.param_surface import ParamSurface, build_param_surface
from agnette.agm.cli_support.program_options import EXEC_RESERVED_FLAGS
from agnette.config.general import GeneralConfig, PathAnchor
from agnette.config.qualified_keys import QualifiedConfigLookupError
from agnette.core.toml import TomlDict, toml_dict
from tests._agl_helpers import EMPTY_REACHABLE
from tests._config_helpers import unanchored_config as _config

_SPAN = SourceSpan(1, 1, 1, 2, 0, 1)


def _option(name: str) -> ProgramOptionSpec:
    return ProgramOptionSpec(name=name, short=None, env=None, metavar=None, hidden=False, doc=None)


def _binding(
    module: str | ModuleId,
    name: str,
    type_: Type = BoolType(),
    *,
    external: str | None = None,
    scope_path: tuple[str, ...] = (),
    is_path: bool = False,
) -> HostParamInfo:
    return HostParamInfo.module_param(
        ModuleId.from_path(module) if isinstance(module, str) else module,
        scope_path,
        name,
        type_,
        _option(name if external is None else external),
        is_path=is_path,
    )


def _argument(
    name: str,
    kind: ParamZone = ParamZone.NAMED_ONLY,
    type_: Type = BoolType(),
    *,
    has_default: bool = True,
    is_path: bool = False,
) -> ProgramParamInfo:
    return ProgramParamInfo(
        name=name,
        kind=kind,
        type=type_,
        has_default=has_default,
        span=_SPAN,
        cli=_option(name),
        is_path=is_path,
    )


def _program(
    *,
    module: str | ModuleId = "app/main",
    name: str = "run",
    closure: tuple[ModuleId, ...] = (),
    parameters: tuple[ProgramParamInfo, ...] = (),
) -> ProgramDeclInfo:
    module_id = ModuleId.from_path(module) if isinstance(module, str) else module
    return ProgramDeclInfo(
        module=module_id,
        scope_path=(),
        name=name,
        node_id=1,
        span=_SPAN,
        parameters=parameters,
        is_entry=module_id.is_entry,
        doc=None,
        reachable=EMPTY_REACHABLE,
        closure=closure or (module_id,),
    )


def _surface(
    program: ProgramDeclInfo,
    bindings: tuple[HostParamInfo, ...],
    reserved: frozenset[str] = frozenset(),
) -> ParamSurface:
    return build_param_surface(reserved, program, (*bindings, *engine_setting_params()))


def _resolve(
    config: GeneralConfig,
    program: ProgramDeclInfo,
    bindings: tuple[HostParamInfo, ...],
    params: Mapping[tuple[ModuleId, tuple[str, ...], str], object] = {},
    reserved: frozenset[str] = frozenset(),
) -> dict[tuple[ModuleId, tuple[str, ...], str], object]:
    tiers = resolve_param_values(
        config,
        program,
        params,
        entry_segments=("workflow",),
        command_paths=(),
        surface=_surface(program, bindings, reserved),
    )
    return {**tiers.lower, **tiers.upper}


def test_resolves_a_module_route() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(_config({"A": {"logging": {"verbose": True}}}), program, (binding,))

    assert values == {binding.key: True}


def test_resolves_a_scope_region_table() -> None:
    binding = _binding("A/logging", "trace", scope_path=("debug",))
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(_config({"A": {"logging": {"debug": {"trace": True}}}}), program, (binding,))

    assert values == {binding.key: True}


def test_program_route_overrides_a_module_route_from_any_layer() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(
        _config(
            {"A": {"logging": {"verbose": False}}},
            {"workflow": {"run": {"verbose": True}}},
        ),
        program,
        (binding,),
    )

    assert values == {binding.key: True}


def test_a_lower_layer_program_route_overrides_a_higher_layer_module_route() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(
        _config(
            {"workflow": {"run": {"verbose": True}}},
            {"A": {"logging": {"verbose": False}}},
        ),
        program,
        (binding,),
    )

    assert values == {binding.key: True}


def test_rejects_a_leaf_shared_by_a_module_and_program_route() -> None:
    imported = _binding("workflow/main", "verbose")
    own = _binding(ENTRY_ID, "verbose")
    program = _program(module=ENTRY_ID, name="main", closure=(ENTRY_ID, imported.module))

    with pytest.raises(QualifiedConfigLookupError) as exc_info:
        _resolve(_config({"workflow": {"main": {"verbose": True}}}), program, (imported, own))

    assert imported.declaration_path in str(exc_info.value)
    assert own.declaration_path in str(exc_info.value)


def test_distinct_module_and_program_tables_do_not_collide() -> None:
    imported = _binding("A/logging", "verbose")
    own = _binding(ENTRY_ID, "verbose")
    program = _program(module=ENTRY_ID, closure=(ENTRY_ID, imported.module))

    values = _resolve(_config({"A": {"logging": {"verbose": False}}}), program, (imported, own))

    assert values == {imported.key: False}


def test_option_name_is_used_for_module_and_program_routes_but_keeps_the_declared_key() -> None:
    binding = _binding("A/logging", "verbose", external="chatty")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(
        _config(
            {"A": {"logging": {"chatty": False}}},
            {"workflow": {"run": {"chatty": True}}},
        ),
        program,
        (binding,),
    )

    assert values == {binding.key: True}


def test_resolves_a_quoted_module_anchor() -> None:
    binding = _binding("A/logging", "trace", scope_path=("debug",))
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(_config({"A/logging": {"debug": {"trace": True}}}), program, (binding,))

    assert values == {binding.key: True}


def test_rejects_an_ambiguous_program_table_leaf_when_configured() -> None:
    first = _binding("first/logging", "verbose")
    second = _binding("second/logging", "verbose")
    program = _program(
        closure=(ModuleId.from_path("app/main"), first.module, second.module),
    )

    with pytest.raises(QualifiedConfigLookupError) as exc_info:
        _resolve(_config({"workflow": {"run": {"verbose": True}}}), program, (first, second))

    assert first.declaration_path in str(exc_info.value)
    assert second.declaration_path in str(exc_info.value)


def test_rejects_a_configured_duplicate_external_name_in_one_module_route() -> None:
    first = _binding("A/logging", "verbose", external="level")
    second = _binding("A/logging", "detail", external="level")
    program = _program(closure=(ModuleId.from_path("app/main"), first.module))

    with pytest.raises(QualifiedConfigLookupError) as exc_info:
        _resolve(_config({"A": {"logging": {"level": True}}}), program, (first, second))

    assert first.declaration_path in str(exc_info.value)
    assert second.declaration_path in str(exc_info.value)


def test_an_inactive_duplicate_external_name_in_one_module_route_does_not_error() -> None:
    first = _binding("A/logging", "verbose", external="level")
    second = _binding("A/logging", "detail", external="level")
    program = _program(closure=(ModuleId.from_path("app/main"), first.module))

    values = _resolve(_config(), program, (first, second))

    assert values == {}


def test_duplicate_external_names_at_distinct_module_scopes_remain_independent() -> None:
    root = _binding("A/logging", "verbose", external="level")
    scoped = _binding("A/logging", "detail", external="level", scope_path=("debug",))
    program = _program(closure=(ModuleId.from_path("app/main"), root.module))

    values = _resolve(
        _config({"A": {"logging": {"level": True, "debug": {"level": False}}}}),
        program,
        (root, scoped),
    )

    assert values == {root.key: True, scoped.key: False}


def test_an_inactive_ambiguous_program_leaf_does_not_prevent_resolution() -> None:
    first = _binding("first/logging", "verbose")
    second = _binding("second/logging", "verbose")
    program = _program(
        closure=(ModuleId.from_path("app/main"), first.module, second.module),
    )

    values = _resolve(_config(), program, (first, second))

    assert values == {}


def test_program_route_accepts_a_qualified_leaf() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(
        _config({"workflow": {"run": {"logging.verbose": True}}}), program, (binding,)
    )

    assert values == {binding.key: True}


def test_program_route_accepts_the_longest_qualified_leaf() -> None:
    binding = _binding("A/logging", "trace", scope_path=("debug",))
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(
        _config({"workflow": {"run": {"A.logging.debug.trace": True}}}), program, (binding,)
    )

    assert values == {binding.key: True}


def test_a_qualified_leaf_reaches_a_param_whose_bare_name_a_signature_claims() -> None:
    binding = _binding("A/logging", "verbose")
    signature = _argument("verbose", has_default=False)
    program = _program(
        closure=(ModuleId.from_path("app/main"), binding.module),
        parameters=(signature,),
    )

    values = _resolve(
        _config({"workflow": {"run": {"logging.verbose": True}}}), program, (binding,)
    )

    assert values == {binding.key: True}


def test_a_qualified_leaf_reaches_a_param_named_after_an_engine_key() -> None:
    binding = _binding("A/logging", "timeout", TextType())
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(
        _config({"workflow": {"run": {"logging.timeout": "30s"}}}),
        program,
        (binding,),
        reserved=EXEC_RESERVED_FLAGS,
    )

    assert values == {binding.key: "30s"}


def test_a_bare_leaf_reaches_the_param_that_shadows_the_engine_setting() -> None:
    binding = _binding("A/logging", "timeout", TextType())
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    tiers = _tiers(_config({"workflow": {"run": {"timeout": "30s"}}}), program, (binding,))
    module, engine = split_engine_tiers(tiers)

    assert module.upper == {binding.key: "30s"}
    assert engine.program == {}


def _tiers(
    config: GeneralConfig,
    program: ProgramDeclInfo,
    bindings: tuple[HostParamInfo, ...],
    **kwargs: Any,
) -> ParamValueTiers:
    return resolve_param_values(
        config,
        program,
        {},
        entry_segments=("workflow",),
        command_paths=(),
        surface=_surface(program, bindings),
        **kwargs,
    )


def test_a_bare_leaf_reaches_the_engine_setting_when_no_param_claims_the_name() -> None:
    program = _program()

    tiers = _tiers(_config({"workflow": {"run": {"timeout": "30s", "debug": True}}}), program, ())

    module, engine = split_engine_tiers(tiers)

    assert engine.program == {"timeout": "30s", "debug": True}
    assert module.upper == {}


def test_a_qualified_leaf_always_reaches_the_engine_setting() -> None:
    binding = _binding("A/logging", "timeout", TextType())
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    tiers = _tiers(
        _config({"workflow": {"run": {"std.config.timeout": "30s", "logging.timeout": "9s"}}}),
        program,
        (binding,),
    )

    module, engine = split_engine_tiers(tiers)

    assert engine.program == {"timeout": "30s"}
    assert module.upper == {binding.key: "9s"}


def test_manifest_config_supplies_the_engine_settings_beneath_the_program_table() -> None:
    program = _program()

    tiers = _tiers(
        _config({"workflow": {"run": {"debug": True}}}),
        program,
        (),
        package_config={"timeout": "5s", "debug": False},
    )

    _, engine = split_engine_tiers(tiers)

    assert engine.program == {"debug": True}
    assert engine.manifest == {"timeout": "5s"}


def test_rejects_two_spellings_of_one_param_in_one_program_table() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    with pytest.raises(QualifiedConfigLookupError) as exc_info:
        _resolve(
            _config({"workflow": {"run": {"verbose": True, "logging.verbose": False}}}),
            program,
            (binding,),
        )

    assert "verbose" in str(exc_info.value)
    assert "logging.verbose" in str(exc_info.value)


def test_a_later_layer_qualified_leaf_overrides_an_earlier_bare_leaf() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(
        _config(
            {"workflow": {"run": {"verbose": False}}},
            {"workflow": {"run": {"logging.verbose": True}}},
        ),
        program,
        (binding,),
    )

    assert values == {binding.key: True}


def test_a_qualified_program_leaf_beats_a_module_route() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(
        _config(
            {"workflow": {"run": {"logging.verbose": True}}},
            {"A": {"logging": {"verbose": False}}},
        ),
        program,
        (binding,),
    )

    assert values == {binding.key: True}


def test_a_qualified_program_leaf_is_declared_and_draws_no_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))
    config = _config({"workflow": {"run": {"logging.verbose": True}}})

    _resolve(config, program, (binding,))

    assert "logging.verbose" not in capsys.readouterr().err


def test_non_engine_key_leaf_forms_a_program_route_for_a_param() -> None:
    binding = _binding("A/logging", "max-iters", IntType())
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(_config({"workflow": {"run": {"max-iters": 4}}}), program, (binding,))

    assert values == {binding.key: 4}


def test_anonymous_entry_params_do_not_read_a_module_route() -> None:
    binding = _binding(ENTRY_ID, "verbose")
    program = _program(module=ENTRY_ID)

    tiers = resolve_param_values(
        _config({"workflow": {"run": {"verbose": True}}}),
        program,
        {},
        entry_segments=(),
        command_paths=(),
        surface=_surface(program, (binding,)),
    )

    assert {**tiers.lower, **tiers.upper} == {}


def test_cli_or_environment_values_are_not_overwritten() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    values = _resolve(
        _config({"A": {"logging": {"verbose": False}}}),
        program,
        (binding,),
        {binding.key: True},
    )

    assert values == {binding.key: True}


def test_every_declared_leaf_of_each_module_and_program_route_draws_no_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A signature name, a module parameter's bare name and each of its
    qualified spellings, and every engine key are declared on the program
    route; each module route declares its own parameters."""
    own = _binding("app/main", "quiet")
    imported = _binding("A/logging", "verbose")
    signature = _argument("label", type_=TextType(), has_default=False)
    program = _program(closure=(own.module, imported.module), parameters=(signature,))

    # One spelling of a parameter per layer: two in one layer conflict.
    _resolve(
        _config(
            {"app": {"main": {"quiet": True}}},
            {"A": {"logging": {"verbose": True}}},
            {"workflow": {"run": {"label": "x", "quiet": True, "verbose": True, "timeout": "30s"}}},
            {"workflow": {"run": {"main.quiet": True, "logging.verbose": True}}},
            {"workflow": {"run": {"app.main.quiet": True, "A.logging.verbose": True}}},
        ),
        program,
        (own, imported),
    )

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("module", "scope_path"),
    [("devel/review", ()), ("vendor/devel/review", ()), ("devel", ("review",))],
)
@pytest.mark.parametrize("leaf", ["agent", "timeout", "logging.verbose", "unknown", "input"])
def test_command_validation_takes_precedence_over_colliding_module_routes(
    capsys: pytest.CaptureFixture[str],
    module: str,
    scope_path: tuple[str, ...],
    leaf: str,
) -> None:
    colliding = _binding(module, "quiet", scope_path=scope_path)
    imported = _binding("A/logging", "verbose")
    parameters = tuple(
        _argument(name, kind, TextType())
        for name, kind in (("agent", ParamZone.NAMED_ONLY), ("input", ParamZone.POSITIONAL_ONLY))
    )
    program = _program(closure=(colliding.module, imported.module), parameters=parameters)

    resolve_param_values(
        _config({"devel": {"review": {leaf: True}}}),
        program,
        {},
        entry_segments=("app", "main"),
        command_paths=(("devel", "review"),),
        surface=_surface(program, (colliding, imported)),
    )

    reported = capsys.readouterr().err
    if leaf in {"unknown", "input"}:
        assert reported.count(leaf) == 1
        if leaf == "input":
            assert "positional-only" in reported
    else:
        assert reported == ""


def test_a_sibling_command_table_is_not_validated_as_a_module_route(
    capsys: pytest.CaptureFixture[str],
) -> None:
    program = _program(closure=(ModuleId.from_path("devel/review"),))

    resolve_param_values(
        _config({"devel": {"review": {"agent": "claude/opus"}}}),
        program,
        {},
        entry_segments=("app", "main"),
        command_paths=(("devel", "revise"),),
        package_command_paths=frozenset({("devel", "review"), ("devel", "revise")}),
        surface=_surface(program, ()),
    )

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("command_first", [True, False])
def test_a_command_table_does_not_hide_unknown_keys_on_a_module_table(
    capsys: pytest.CaptureFixture[str],
    command_first: bool,
) -> None:
    program = _program(closure=(ModuleId.from_path("devel/review"),))
    command_layer = {"devel": {"review": {"stray": True}}}
    module_layer = {"review": {"stray": True}}
    layers = (command_layer, module_layer) if command_first else (module_layer, command_layer)

    resolve_param_values(
        _config(*layers),
        program,
        {},
        entry_segments=("app", "main"),
        command_paths=(("devel", "revise"),),
        package_command_paths=frozenset({("devel", "review"), ("devel", "revise")}),
        surface=_surface(program, ()),
    )

    reported = capsys.readouterr().err
    assert reported.count("stray") == 1
    assert "devel.review" not in reported


def test_a_module_root_is_reported_before_its_scope_routes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    scoped = _binding("A/logging", "trace", scope_path=("debug",))
    program = _program(closure=(ModuleId.from_path("app/main"), scoped.module))

    _resolve(
        _config({"A": {"logging": {"stray": True, "debug": {"trace": True, "typo": True}}}}),
        program,
        (scoped,),
    )

    reported = capsys.readouterr().err
    assert "trace" not in reported
    assert reported.index("stray") < reported.index("typo")


def test_a_positional_only_signature_leaf_gets_its_distinct_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    positional = _argument("input", ParamZone.POSITIONAL_ONLY, TextType(), has_default=False)
    program = _program(parameters=(positional,))

    _resolve(_config({"workflow": {"run": {"input": "value"}}}), program, ())

    reported = capsys.readouterr().err
    assert "positional-only" in reported
    assert "is not a declared" not in reported


def test_undeclared_key_reporting_uses_every_returned_route(
    capsys: pytest.CaptureFixture[str],
) -> None:
    own = _binding("app/main", "quiet")
    imported = _binding("A/logging", "verbose")
    program = _program(closure=(own.module, imported.module))
    config = _config(
        {"A": {"logging": {"verbose": True, "misspelled": True}}},
        {"workflow": {"run": {"unexpected": True}}},
    )

    _resolve(config, program, (own, imported))

    reported = capsys.readouterr().err
    assert "misspelled" in reported
    assert "unexpected" in reported


def test_a_bare_leaf_a_signature_claims_draws_no_undeclared_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    binding = _binding("A/logging", "verbose")
    signature = _argument("verbose", has_default=False)
    program = _program(
        closure=(ModuleId.from_path("app/main"), binding.module),
        parameters=(signature,),
    )
    config = _config({"workflow": {"run": {"verbose": True}}})

    _resolve(config, program, (binding,))

    assert capsys.readouterr().err == ""


def test_rejects_an_ambiguous_qualified_program_leaf_when_configured() -> None:
    first = _binding("first/logging", "verbose")
    second = _binding("second/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), first.module, second.module))

    with pytest.raises(QualifiedConfigLookupError) as exc_info:
        _resolve(
            _config({"workflow": {"run": {"logging.verbose": True}}}), program, (first, second)
        )

    assert first.declaration_path in str(exc_info.value)
    assert second.declaration_path in str(exc_info.value)


def test_reports_surface_modules_missing_from_the_recorded_closure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"),))

    _resolve(
        _config({"A": {"logging": {"verbose": True, "misspelled": True}}}), program, (binding,)
    )

    reported = capsys.readouterr().err
    assert "misspelled" in reported
    assert "'verbose'" not in reported


class TestDeclaredButUnreachableParams:
    """A closure-declared parameter the program does not reach has no value and no warning."""

    @staticmethod
    def _resolve(
        config: GeneralConfig, *, package_config: Mapping[str, object] | None = None
    ) -> ParamValueTiers:
        unreachable = _binding("A/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), unreachable.module))
        return resolve_param_values(
            config,
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, ()),
            declared_params=(unreachable,),
            package_config=None if package_config is None else dict(package_config),
        )

    @pytest.mark.parametrize(
        "layer",
        [
            {"A": {"logging": {"verbose": True}}},
            {"workflow": {"run": {"verbose": True}}},
            {"workflow": {"run": {"logging.verbose": True}}},
            {"devel": {"review": {"A.logging.verbose": True}}},
        ],
    )
    def test_a_configured_key_draws_no_warning_and_supplies_no_value(
        self, layer: Mapping[str, object], capsys: pytest.CaptureFixture[str]
    ) -> None:
        tiers = self._resolve(_config(layer))

        assert capsys.readouterr().err == ""
        assert not tiers.upper
        assert not tiers.lower

    def test_a_manifest_key_draws_no_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._resolve(_config(), package_config={"devel": {"review": {"logging.verbose": True}}})

        assert capsys.readouterr().err == ""

    def test_an_undeclared_key_still_draws_a_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._resolve(_config({"A": {"logging": {"misspelled": True}}}))

        assert "misspelled" in capsys.readouterr().err


class TestSpellingsLostByReachedParams:
    """A reached parameter's lost spelling is not declared just because it was a candidate."""

    @staticmethod
    def _resolve(
        config: GeneralConfig,
        *,
        reserved: frozenset[str] = frozenset(),
        signature: bool = False,
        package_config: Mapping[str, object] | None = None,
    ) -> None:
        binding = _binding("A/logging", "verbose")
        parameters = (_argument("verbose", has_default=False),) if signature else ()
        program = _program(
            closure=(ModuleId.from_path("app/main"), binding.module), parameters=parameters
        )
        resolve_param_values(
            config,
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (binding,), reserved),
            declared_params=(binding,),
            package_config=None if package_config is None else dict(package_config),
        )

    def test_a_manifest_leaf_lost_to_a_reserved_flag_warns(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._resolve(
            _config(),
            reserved=frozenset({"--verbose"}),
            package_config={"devel": {"review": {"verbose": True}}},
        )

        assert "verbose" in capsys.readouterr().err

    def test_a_program_table_leaf_lost_to_a_reserved_flag_warns(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._resolve(
            _config({"workflow": {"run": {"verbose": True}}}), reserved=frozenset({"--verbose"})
        )

        assert "verbose" in capsys.readouterr().err


def _resolve_tiers(
    config: GeneralConfig,
    program: ProgramDeclInfo,
    bindings: tuple[HostParamInfo, ...],
    params: Mapping[tuple[ModuleId, tuple[str, ...], str], object] = {},
) -> ParamValueTiers:
    return resolve_param_values(
        config,
        program,
        params,
        entry_segments=("workflow",),
        command_paths=(),
        surface=_surface(program, bindings),
    )


def test_a_module_route_only_value_is_in_the_lower_tier() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    tiers = _resolve_tiers(_config({"A": {"logging": {"verbose": True}}}), program, (binding,))

    assert tiers.lower == {binding.key: True}
    assert tiers.upper == {}


def test_a_program_route_value_is_in_the_upper_tier_and_absent_from_lower() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    tiers = _resolve_tiers(
        _config({"workflow": {"run": {"logging.verbose": True}}}), program, (binding,)
    )

    assert tiers.upper == {binding.key: True}
    assert tiers.lower == {}


def test_a_program_route_value_shadows_a_configured_module_route_in_lower() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    tiers = _resolve_tiers(
        _config(
            {"workflow": {"run": {"logging.verbose": True}}},
            {"A": {"logging": {"verbose": False}}},
        ),
        program,
        (binding,),
    )

    assert tiers.upper == {binding.key: True}
    assert binding.key not in tiers.lower


def test_a_cli_supplied_value_is_in_the_upper_tier_and_shadows_a_module_route_in_lower() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    tiers = _resolve_tiers(
        _config({"A": {"logging": {"verbose": False}}}),
        program,
        (binding,),
        {binding.key: True},
    )

    assert tiers.upper == {binding.key: True}
    assert tiers.lower == {}


def test_supplied_and_program_route_tiers_combine_over_module_route() -> None:
    """``upper`` (program route) and ``lower`` (module route) partition disjoint
    keys here; the full precedence chain, including where ``@config`` ranks
    between them, is ``PipelineDriver.preflight_arguments``'s own merge."""
    module_only = _binding("A/logging", "verbose")
    program_routed = _binding("B/logging", "trace")
    program = _program(
        closure=(ModuleId.from_path("app/main"), module_only.module, program_routed.module)
    )

    tiers = _resolve_tiers(
        _config(
            {"A": {"logging": {"verbose": True}}},
            {"workflow": {"run": {"logging.trace": True}}},
        ),
        program,
        (module_only, program_routed),
    )

    assert tiers.upper == {program_routed.key: True}
    assert tiers.lower == {module_only.key: True}


def test_a_group_table_supplies_a_module_parameter_via_the_program_route() -> None:
    """``[devel]`` feeds a program registered under ``devel review``."""
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    tiers = resolve_param_values(
        _config({"devel": {"verbose": True}}),
        program,
        {},
        entry_segments=("workflow",),
        command_paths=(("devel", "review"),),
        surface=_surface(program, (binding,)),
    )

    assert tiers.upper == {binding.key: True}


def test_rejects_a_group_table_leaf_claimed_by_peer_parameters() -> None:
    """A config group table setting an ambiguous bare leaf errors, as a manifest one does."""
    first = _binding("first/logging", "verbose")
    second = _binding("second/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), first.module, second.module))

    with pytest.raises(QualifiedConfigLookupError) as exc_info:
        resolve_param_values(
            _config({"devel": {"verbose": True}}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (first, second)),
        )

    assert first.declaration_path in str(exc_info.value)
    assert second.declaration_path in str(exc_info.value)


def test_a_nearer_command_table_beats_an_inherited_group_table() -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    tiers = resolve_param_values(
        _config({"devel": {"review": {"verbose": True}, "verbose": False}}),
        program,
        {},
        entry_segments=("workflow",),
        command_paths=(("devel", "review"),),
        surface=_surface(program, (binding,)),
    )

    assert tiers.upper == {binding.key: True}


def test_an_unconsumed_group_table_leaf_draws_no_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    binding = _binding("A/logging", "verbose")
    program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

    resolve_param_values(
        _config({"devel": {"unrelated": True}}),
        program,
        {},
        entry_segments=("workflow",),
        command_paths=(("devel", "review"),),
        surface=_surface(program, (binding,)),
    )

    assert capsys.readouterr().err == ""


def test_rejects_a_group_table_leaf_ambiguous_with_a_module_route() -> None:
    """A leaf an inherited table sets is ambiguous against a same-named module route."""
    own = _binding("app/main", "x")
    imported = _binding("devel", "x")
    program = _program(closure=(own.module, imported.module))

    with pytest.raises(QualifiedConfigLookupError) as exc_info:
        resolve_param_values(
            _config({"devel": {"x": True}}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (own, imported)),
        )

    assert own.declaration_path in str(exc_info.value)
    assert imported.declaration_path in str(exc_info.value)


@pytest.mark.parametrize(
    "layers",
    [
        ({"devel": {"x": True}}, {"devel": {"review": {"x": True}}}),
        ({"devel": {"review": {"x": True}}}, {"devel": {"x": True}}),
        ({"devel": {"x": True, "review": {"x": True}}},),
    ],
    ids=["inherited_then_exact", "exact_then_inherited", "both_in_one_layer"],
)
def test_rejects_a_group_table_ambiguity_regardless_of_layer_order(
    layers: tuple[Mapping[str, object], ...],
) -> None:
    """The exact route also setting the leaf must not hide the inherited-table ambiguity."""
    own = _binding("app/main", "x")
    imported = _binding("devel", "x")
    program = _program(closure=(own.module, imported.module))

    with pytest.raises(QualifiedConfigLookupError) as exc_info:
        resolve_param_values(
            _config(*layers),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (own, imported)),
        )

    assert own.declaration_path in str(exc_info.value)
    assert imported.declaration_path in str(exc_info.value)


def _resolve_argument_routes(
    config: GeneralConfig,
    argument: str,
    bindings: tuple[HostParamInfo, ...],
    *,
    kind: ParamZone = ParamZone.NAMED_ONLY,
    command_paths: tuple[tuple[str, ...], ...] = (("devel", "review"),),
    package_config: TomlDict | None = None,
) -> ParamValueTiers:
    """Resolve a program with one named argument over *bindings*' declaration routes."""
    program = _program(
        closure=(ModuleId.from_path("app/main"), *(b.module for b in bindings)),
        parameters=(_argument(argument, kind),),
    )
    return resolve_param_values(
        config,
        program,
        {},
        entry_segments=("workflow",),
        command_paths=command_paths,
        surface=_surface(program, bindings),
        package_config=package_config,
    )


class TestProgramArgumentsAgainstDeclarationRoutes:
    """One config leaf may address only one parameter, program arguments included."""

    @pytest.mark.parametrize(
        ("module", "table"),
        [
            ("devel/review", {"devel": {"review": {"agent": True}}}),
            ("devel", {"devel": {"agent": True}}),
            ("x/devel/review", {"devel": {"review": {"agent": True}}}),
        ],
        ids=["command_table", "group_table", "short_module_route"],
    )
    def test_rejects_a_leaf_feeding_a_program_argument_and_a_module_parameter(
        self, module: str, table: Mapping[str, object]
    ) -> None:
        binding = _binding(module, "agent")

        with pytest.raises(QualifiedConfigLookupError) as exc_info:
            _resolve_argument_routes(_config(table), "agent", (binding,))

        assert binding.declaration_path in str(exc_info.value)

    def test_rejects_the_dual_feed_set_in_several_layers(self) -> None:
        binding = _binding("devel/review", "agent")
        config = _config(
            {"devel": {"review": {"agent": True}}}, {"devel": {"review": {"agent": False}}}
        )

        with pytest.raises(QualifiedConfigLookupError):
            _resolve_argument_routes(config, "agent", (binding,))

    def test_a_leaf_addressing_only_the_module_parameter_applies(self) -> None:
        binding = _binding("devel/review", "agent")

        tiers = _resolve_argument_routes(
            _config({"devel": {"review": {"agent": True}}}), "other", (binding,)
        )

        assert tiers.upper == {binding.key: True}

    def test_a_leaf_in_a_table_the_program_does_not_read_feeds_only_the_module_parameter(
        self,
    ) -> None:
        binding = _binding("devel/review", "agent")

        tiers = _resolve_argument_routes(
            _config({"devel": {"review": {"agent": True}}}),
            "agent",
            (binding,),
            command_paths=(("dv", "run"),),
        )

        assert tiers.lower == {binding.key: True}

    def test_a_positional_only_argument_is_not_addressed_by_the_program_table(self) -> None:
        binding = _binding("devel/review", "agent")

        tiers = _resolve_argument_routes(
            _config({"devel": {"review": {"agent": True}}}),
            "agent",
            (binding,),
            kind=ParamZone.POSITIONAL_ONLY,
        )

        assert tiers.lower == {binding.key: True}

    def test_an_ambiguous_name_set_only_in_unread_tables_does_not_error(self) -> None:
        binding = _binding("devel/review", "agent")

        tiers = _resolve_argument_routes(
            _config({"workflow": {"run": {"agent": True}}, "unrelated": {"agent": False}}),
            "agent",
            (binding,),
        )

        assert tiers.lower == {}
        assert tiers.upper == {_program().param_key("agent"): True}

    def test_a_manifest_leaf_does_not_feed_a_shadowed_module_parameter(self) -> None:
        """The program argument wins the bare name, so a manifest leaf names no module parameter."""
        binding = _binding("devel/review", "agent")

        tiers = _resolve_argument_routes(
            _config(), "agent", (binding,), package_config={"devel": {"review": {"agent": True}}}
        )

        assert binding.key not in tiers.lower
        assert tiers.lower == {_program().param_key("agent"): True}


class TestManifestConfigMergesBelowConfigFileRoutes:
    """A package manifest ``[config]`` value ranks below file routes, above module route."""

    def test_a_manifest_command_table_supplies_a_module_parameter(self) -> None:
        binding = _binding("A/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

        tiers = resolve_param_values(
            _config(),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (binding,)),
            package_config={"devel": {"review": {"verbose": True}}},
        )

        assert tiers.lower == {binding.key: True}
        assert tiers.upper == {}

    def test_a_manifest_group_table_supplies_a_module_parameter(self) -> None:
        binding = _binding("A/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

        tiers = resolve_param_values(
            _config(),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (binding,)),
            package_config={"devel": {"verbose": True}},
        )

        assert tiers.lower == {binding.key: True}

    def test_a_manifest_root_leaf_supplies_a_module_parameter(self) -> None:
        binding = _binding("A/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

        tiers = resolve_param_values(
            _config(),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(),
            surface=_surface(program, (binding,)),
            package_config={"verbose": True},
        )

        assert tiers.lower == {binding.key: True}

    def test_manifest_wins_over_the_file_module_route(self) -> None:
        binding = _binding("A/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

        tiers = resolve_param_values(
            _config({"logging": {"verbose": False}}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (binding,)),
            package_config={"devel": {"review": {"verbose": True}}},
        )

        assert tiers.lower == {binding.key: True}

    def test_a_file_program_route_wins_over_the_manifest(self) -> None:
        binding = _binding("A/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

        tiers = resolve_param_values(
            _config({"workflow": {"run": {"verbose": False}}}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (binding,)),
            package_config={"devel": {"review": {"verbose": True}}},
        )

        assert tiers.upper == {binding.key: False}
        assert tiers.lower == {}

    def test_a_file_group_table_wins_over_the_manifest(self) -> None:
        binding = _binding("A/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

        tiers = resolve_param_values(
            _config({"devel": {"verbose": False}}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (binding,)),
            package_config={"devel": {"review": {"verbose": True}}},
        )

        assert tiers.upper == {binding.key: False}
        assert tiers.lower == {}

    def test_no_manifest_leaves_leaves_the_module_route_default(self) -> None:
        binding = _binding("A/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

        tiers = resolve_param_values(
            _config({"logging": {"verbose": True}}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (binding,)),
            package_config=None,
        )

        assert tiers.lower == {binding.key: True}

    def test_a_cli_supplied_value_wins_over_the_manifest(self) -> None:
        """A parsed CLI/environment value forms ``upper`` and beats a manifest leaf in ``lower``,
        exactly like a program-route config value does."""
        binding = _binding("A/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

        tiers = resolve_param_values(
            _config(),
            program,
            {binding.key: False},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (binding,)),
            package_config={"devel": {"review": {"verbose": True}}},
        )

        assert tiers.upper == {binding.key: False}
        assert tiers.lower == {}

    def test_a_manifest_same_tier_conflicting_spelling_errors(self) -> None:
        own = _binding("A/logging", "x")
        program = _program(closure=(ModuleId.from_path("app/main"), own.module))

        with pytest.raises(QualifiedConfigLookupError):
            resolve_param_values(
                _config(),
                program,
                {},
                entry_segments=("workflow",),
                command_paths=(("devel", "review"),),
                surface=_surface(program, (own,)),
                package_config={"devel": {"review": {"x": True, "logging.x": False}}},
            )

    def test_a_manifest_ambiguous_bare_leaf_errors_like_a_config_file_one(self) -> None:
        """A manifest table setting a bare leaf two peer parameters claim is rejected too."""
        first = _binding("first/logging", "verbose")
        second = _binding("second/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), first.module, second.module))

        with pytest.raises(QualifiedConfigLookupError) as exc_info:
            resolve_param_values(
                _config(),
                program,
                {},
                entry_segments=("workflow",),
                command_paths=(("devel", "review"),),
                surface=_surface(program, (first, second)),
                package_config={"devel": {"review": {"verbose": True}}},
            )

        assert first.declaration_path in str(exc_info.value)
        assert second.declaration_path in str(exc_info.value)

    def test_a_manifest_group_table_ambiguous_leaf_errors(self) -> None:
        """The ambiguity check also reaches an inherited manifest group table."""
        first = _binding("first/logging", "verbose")
        second = _binding("second/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), first.module, second.module))

        with pytest.raises(QualifiedConfigLookupError):
            resolve_param_values(
                _config(),
                program,
                {},
                entry_segments=("workflow",),
                command_paths=(("devel", "review"),),
                surface=_surface(program, (first, second)),
                package_config={"devel": {"verbose": True}},
            )

    def test_a_manifest_root_ambiguous_leaf_errors(self) -> None:
        """The ambiguity check also reaches the manifest root."""
        first = _binding("first/logging", "verbose")
        second = _binding("second/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), first.module, second.module))

        with pytest.raises(QualifiedConfigLookupError):
            resolve_param_values(
                _config(),
                program,
                {},
                entry_segments=("workflow",),
                command_paths=(),
                surface=_surface(program, (first, second)),
                package_config={"verbose": True},
            )


class TestUnusedManifestLeafWarnings:
    """A manifest command-table leaf the selected program does not consume is reported."""

    @staticmethod
    def _resolve(
        package_config: Mapping[str, object],
        *,
        package_command_paths: frozenset[tuple[str, ...]] = frozenset(),
        command_paths: tuple[tuple[str, ...], ...] = (("devel", "review"),),
    ) -> None:
        binding = _binding("A/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))
        resolve_param_values(
            _config(),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=command_paths,
            surface=_surface(program, (binding,)),
            package_command_paths=package_command_paths,
            package_config=dict(package_config),
        )

    def test_warns_for_a_command_table_leaf_naming_no_reachable_parameter(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._resolve({"devel": {"review": {"other.level": 1}}})

        reported = capsys.readouterr().err
        assert "other.level" in reported
        assert "devel.review" in reported

    def test_warns_for_a_command_table_leaf_the_program_signature_shadows(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        binding = _binding("A/logging", "verbose")
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))
        resolve_param_values(
            _config(),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, ()),
            package_config={"devel": {"review": {"verbose": True}}},
        )

        assert "verbose" in capsys.readouterr().err

    def test_a_positional_only_argument_leaf_draws_exactly_one_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        positional = _argument("input", ParamZone.POSITIONAL_ONLY, TextType(), has_default=False)
        program = _program(parameters=(positional,))

        resolve_param_values(
            _config(),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, ()),
            package_config={"devel": {"review": {"input": "value", "typo": 1}}},
        )

        lines = [line for line in capsys.readouterr().err.splitlines() if "'input'" in line]
        assert len(lines) == 1

    def test_a_consumed_leaf_draws_no_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._resolve({"devel": {"review": {"logging.verbose": True}}})

        assert capsys.readouterr().err == ""

    def test_an_engine_setting_leaf_draws_no_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._resolve({"devel": {"review": {"trace": True}}})

        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize(
        "package_config",
        [{"devel": {"unrelated": True}}, {"unrelated": True}],
        ids=["group", "root"],
    )
    def test_an_inherited_table_leaf_draws_no_warning(
        self, package_config: Mapping[str, object], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A sibling command may be the one that consumes an inherited default."""
        self._resolve(package_config)

        assert capsys.readouterr().err == ""

    def test_a_table_with_a_registered_descendant_draws_no_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._resolve(
            {"devel": {"review": {"unrelated": True}}},
            package_command_paths=frozenset({("devel", "review"), ("devel", "review", "deep")}),
        )

        assert capsys.readouterr().err == ""


class TestProgramArgumentsResolveThroughTheOneChain:
    """A program argument reads the same tiers as any host parameter."""

    @staticmethod
    def _resolve(
        *,
        config: GeneralConfig | None = None,
        params: Mapping[HostParamKey, object] | None = None,
        positional: frozenset[FunctionParamKey] = frozenset(),
        package_config: TomlDict | None = None,
        argument: str = "limit",
    ) -> tuple[ParamValueTiers, FunctionParamKey]:
        program = _program(parameters=(_argument(argument, type_=IntType()),))
        tiers = resolve_param_values(
            _config() if config is None else config,
            program,
            {} if params is None else params,
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, ()),
            package_config=package_config,
            positional=positional,
        )
        return tiers, program.param_key(argument)

    def test_a_manifest_leaf_supplies_a_program_argument(self) -> None:
        tiers, key = self._resolve(package_config={"devel": {"review": {"limit": 5}}})

        assert tiers.lower == {key: 5}
        assert tiers.upper == {}

    def test_a_manifest_root_leaf_supplies_a_program_argument(self) -> None:
        tiers, key = self._resolve(package_config={"limit": 5})

        assert tiers.lower == {key: 5}

    def test_the_program_route_beats_the_manifest(self) -> None:
        tiers, key = self._resolve(
            config=_config({"workflow": {"run": {"limit": 7}}}),
            package_config={"devel": {"review": {"limit": 5}}},
        )

        assert tiers.upper == {key: 7}
        assert tiers.lower == {}

    def test_a_supplied_value_beats_the_program_route_and_the_manifest(self) -> None:
        program_key = _program().param_key("limit")

        tiers, key = self._resolve(
            config=_config({"workflow": {"run": {"limit": 7}}}),
            params={program_key: "9"},
            package_config={"devel": {"review": {"limit": 5}}},
        )

        assert tiers.upper == {key: "9"}
        assert tiers.lower == {}
        assert tiers.supplied == {key}

    def test_a_positionally_bound_argument_takes_no_configured_value(self) -> None:
        key = _program().param_key("limit")

        tiers, _ = self._resolve(
            config=_config({"workflow": {"run": {"limit": 7}}}),
            positional=frozenset({key}),
            package_config={"limit": 5},
        )

        assert tiers.upper == {}
        assert tiers.lower == {}
        assert tiers.supplied == {key}

    def test_a_manifest_leaf_naming_a_program_argument_draws_no_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._resolve(package_config={"devel": {"review": {"limit": 5}}})

        assert capsys.readouterr().err == ""

    def test_a_manifest_leaf_lost_to_a_program_argument_feeds_only_the_argument(self) -> None:
        """The argument wins the bare name, so the engine setting of that name is not fed."""
        tiers, key = self._resolve(
            package_config={"devel": {"review": {"agent": True}}}, argument="agent"
        )

        assert tiers.lower == {key: True}

    def test_a_program_argument_and_a_surface_entry_never_conflict_with_themselves(self) -> None:
        """One table is the program route and a module route; distinct parameters still apply."""
        binding = _binding("devel/review", "other")

        tiers = _resolve_argument_routes(
            _config({"devel": {"review": {"limit": 3, "other": 4}}}), "limit", (binding,)
        )

        assert tiers.upper == {
            _program().param_key("limit"): 3,
            binding.key: 4,
        }


class TestConfiguredPathValuesAnchorToTheirDefiningFile:
    """A config-sourced value of a path-typed parameter resolves like an engine path key."""

    @staticmethod
    def _dirs(tmp_path: Path) -> tuple[Path, Path]:
        config_dir = tmp_path / "cfg"
        cwd = tmp_path / "cwd"
        config_dir.mkdir()
        cwd.mkdir()
        (config_dir / "data.txt").write_text("", encoding="utf-8")
        return config_dir, cwd

    @staticmethod
    def _file_config(config_dir: Path, cwd: Path, layer: Mapping[str, object]) -> GeneralConfig:
        return GeneralConfig.from_layers([toml_dict(layer)], [PathAnchor(config_dir, cwd)])

    def test_a_program_route_argument_anchors_to_its_config_file(self, tmp_path: Path) -> None:
        config_dir, cwd = self._dirs(tmp_path)
        program = _program(parameters=(_argument("data", type_=TextType(), is_path=True),))

        tiers = resolve_param_values(
            self._file_config(config_dir, cwd, {"workflow": {"run": {"data": "data.txt"}}}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(),
            surface=_surface(program, ()),
        )

        assert tiers.upper == {program.param_key("data"): str(config_dir / "data.txt")}

    def test_a_group_table_argument_anchors_to_its_config_file(self, tmp_path: Path) -> None:
        config_dir, cwd = self._dirs(tmp_path)
        program = _program(parameters=(_argument("data", type_=TextType(), is_path=True),))

        tiers = resolve_param_values(
            self._file_config(config_dir, cwd, {"devel": {"data": "data.txt"}}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, ()),
        )

        assert tiers.upper == {program.param_key("data"): str(config_dir / "data.txt")}

    def test_a_module_route_parameter_anchors_to_its_config_file(self, tmp_path: Path) -> None:
        config_dir, cwd = self._dirs(tmp_path)
        binding = _binding("A/logging", "data", TextType(), is_path=True)
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

        tiers = resolve_param_values(
            self._file_config(config_dir, cwd, {"A": {"logging": {"data": "data.txt"}}}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(),
            surface=_surface(program, (binding,)),
        )

        assert tiers.lower == {binding.key: str(config_dir / "data.txt")}

    def test_a_path_missing_beside_the_config_file_falls_back_to_the_invocation_directory(
        self, tmp_path: Path
    ) -> None:
        config_dir, cwd = self._dirs(tmp_path)
        program = _program(parameters=(_argument("data", type_=TextType(), is_path=True),))

        tiers = resolve_param_values(
            self._file_config(config_dir, cwd, {"workflow": {"run": {"data": "other.txt"}}}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(),
            surface=_surface(program, ()),
        )

        assert tiers.upper == {program.param_key("data"): str(cwd / "other.txt")}

    def test_a_manifest_value_anchors_to_the_package_directory(self, tmp_path: Path) -> None:
        config_dir, cwd = self._dirs(tmp_path)
        program = _program(parameters=(_argument("data", type_=TextType(), is_path=True),))

        tiers = resolve_param_values(
            self._file_config(config_dir, cwd, {}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, ()),
            package_config={"data": "data.txt"},
            manifest_anchor=PathAnchor(config_dir, cwd),
        )

        assert tiers.lower == {program.param_key("data"): str(config_dir / "data.txt")}

    def test_a_manifest_path_missing_in_the_package_falls_back_to_the_invocation_directory(
        self, tmp_path: Path
    ) -> None:
        config_dir, cwd = self._dirs(tmp_path)
        program = _program(parameters=(_argument("data", type_=TextType(), is_path=True),))

        tiers = resolve_param_values(
            self._file_config(config_dir, cwd, {}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, ()),
            package_config={"data": "other.txt"},
            manifest_anchor=PathAnchor(config_dir, cwd),
        )

        assert tiers.lower == {program.param_key("data"): str(cwd / "other.txt")}

    def test_a_manifest_module_parameter_and_engine_path_anchor_to_the_package_directory(
        self, tmp_path: Path
    ) -> None:
        config_dir, cwd = self._dirs(tmp_path)
        binding = _binding("A/logging", "data", TextType(), is_path=True)
        program = _program(closure=(ModuleId.from_path("app/main"), binding.module))

        tiers = resolve_param_values(
            self._file_config(config_dir, cwd, {}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(("devel", "review"),),
            surface=_surface(program, (binding,)),
            package_config={"data": "data.txt", "trace-file": "data.txt"},
            manifest_anchor=PathAnchor(config_dir, cwd),
        )
        module_tiers, engine = split_engine_tiers(tiers)

        assert module_tiers.lower == {binding.key: str(config_dir / "data.txt")}
        assert engine.manifest == {"trace-file": str(config_dir / "data.txt")}

    def test_a_text_parameter_and_a_supplied_value_stay_as_written(self, tmp_path: Path) -> None:
        config_dir, cwd = self._dirs(tmp_path)
        program = _program(
            parameters=(
                _argument("note", type_=TextType()),
                _argument("data", type_=TextType(), is_path=True),
            )
        )

        tiers = resolve_param_values(
            self._file_config(
                config_dir, cwd, {"workflow": {"run": {"note": "data.txt", "data": "data.txt"}}}
            ),
            program,
            {program.param_key("data"): "data.txt"},
            entry_segments=("workflow",),
            command_paths=(),
            surface=_surface(program, ()),
        )

        assert tiers.upper == {
            program.param_key("note"): "data.txt",
            program.param_key("data"): "data.txt",
        }

    def test_a_layer_without_an_anchor_leaves_a_path_as_written(self) -> None:
        program = _program(parameters=(_argument("data", type_=TextType(), is_path=True),))

        tiers = resolve_param_values(
            _config({"workflow": {"run": {"data": "data.txt"}}}),
            program,
            {},
            entry_segments=("workflow",),
            command_paths=(),
            surface=_surface(program, ()),
        )

        assert tiers.upper == {program.param_key("data"): "data.txt"}
