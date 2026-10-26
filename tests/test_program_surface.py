"""Tests for the pure module-parameter spelling surface."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agnette.agl.attributes import ProgramOptionSpec
from agnette.agl.modules.ids import ENTRY_ID, ModuleId
from agnette.agl.runtime.engine_config import engine_setting_params
from agnette.agl.runtime.types import (
    HostParamInfo,
    HostParamKind,
    ProgramDeclInfo,
    ProgramParamInfo,
)
from agnette.agl.semantics.types import BoolType, IntType, TextType, Type
from agnette.agl.syntax.spans import SourceSpan
from agnette.agl.zones import ParamZone
from agnette.agm.cli_support.param_surface import (
    ParamSurface,
    ParamSurfaceEntry,
    build_param_surface,
)
from agnette.agm.cli_support.program_options import EXEC_RESERVED_FLAGS, REGISTERED_RESERVED_FLAGS
from tests._agl_helpers import EMPTY_REACHABLE

ENGINE = HostParamKind.ENGINE
_SPAN = SourceSpan(1, 1, 1, 2, 0, 1)


def _option(name: str, *, short: str | None = None, hidden: bool = False) -> ProgramOptionSpec:
    return ProgramOptionSpec(
        name=name,
        short=short,
        env=None,
        metavar=None,
        hidden=hidden,
        doc=None,
    )


def _param(
    module: str,
    name: str,
    type_: Type = BoolType(),
    *,
    scope: tuple[str, ...] = (),
    external: str | None = None,
    short: str | None = None,
    hidden: bool = False,
    owner: str | None = None,
) -> HostParamInfo:
    return HostParamInfo.module_param(
        ModuleId.from_path(module),
        scope,
        name,
        type_,
        _option(name if external is None else external, short=short, hidden=hidden),
        owner=None if owner is None else Path(owner),
    )


def _signature(
    name: str,
    *,
    kind: ParamZone = ParamZone.NAMED_ONLY,
    short: str | None = None,
) -> ProgramParamInfo:
    return ProgramParamInfo(
        name=name,
        kind=kind,
        type=TextType(),
        has_default=False,
        span=_SPAN,
        cli=_option(name, short=short),
    )


def _program(*parameters: ProgramParamInfo, owner: str | None = None) -> ProgramDeclInfo:
    return ProgramDeclInfo(
        module=ModuleId.from_path("app/main"),
        scope_path=(),
        name="run",
        node_id=1,
        span=_SPAN,
        parameters=parameters,
        is_entry=True,
        doc=None,
        reachable=EMPTY_REACHABLE,
        closure=(ModuleId.from_path("app/main"),),
        owner=None if owner is None else Path(owner),
    )


def _entry(surface: ParamSurface, param: HostParamInfo) -> ParamSurfaceEntry:
    return next(entry for entry in surface.entries if entry.param == param)


def test_levels_resolve_from_host_to_signature_to_own_then_imports() -> None:
    own_verbose = _param("app/main", "verbose")
    own_limit = _param("app/main", "limit")
    own_theme = _param("app/main", "theme")
    imported_verbose = _param("lib/logging", "verbose")
    imported_theme = _param("lib/theme", "theme")
    surface = build_param_surface(
        frozenset({"--verbose"}),
        _program(_signature("limit")),
        (own_verbose, own_limit, own_theme, imported_verbose, imported_theme),
    )

    assert _entry(surface, own_verbose).spellings == ("main.verbose", "app.main.verbose")
    assert _entry(surface, own_limit).spellings == ("main.limit", "app.main.limit")
    assert _entry(surface, own_theme).spellings[0] == "theme"
    assert _entry(surface, imported_verbose).spellings == (
        "logging.verbose",
        "lib.logging.verbose",
    )
    assert _entry(surface, imported_theme).spellings == ("theme.theme", "lib.theme.theme")


def test_own_package_module_claims_a_bare_name_before_another_package() -> None:
    sibling = _param("app/helper", "verbose", owner="/pkgs/app")
    dependency = _param("lib/logging", "verbose", owner="/pkgs/lib")
    surface = build_param_surface(frozenset(), _program(owner="/pkgs/app"), (dependency, sibling))

    assert _entry(surface, sibling).spellings[0] == "verbose"
    assert _entry(surface, dependency).spellings == ("logging.verbose", "lib.logging.verbose")
    assert "verbose" not in surface.ambiguous
    assert "--verbose" not in surface.ambiguous_options
    assert _entry(surface, dependency).option_spellings[0] == "--logging.verbose"


def test_program_module_claims_a_bare_name_before_its_own_package() -> None:
    own = _param("app/main", "verbose", owner="/pkgs/app")
    sibling = _param("app/helper", "verbose", owner="/pkgs/app")
    surface = build_param_surface(frozenset(), _program(owner="/pkgs/app"), (sibling, own))

    assert _entry(surface, own).spellings[0] == "verbose"
    assert _entry(surface, sibling).spellings == ("helper.verbose", "app.helper.verbose")


def test_peers_of_one_owner_are_ambiguous_on_the_bare_name_only() -> None:
    first = _param("app/one", "verbose", owner="/pkgs/app")
    second = _param("app/two", "verbose", owner="/pkgs/app")
    surface = build_param_surface(frozenset(), _program(owner="/pkgs/app"), (first, second))

    assert surface.ambiguous["verbose"] == (first, second)
    assert _entry(surface, first).spellings == ("one.verbose", "app.one.verbose")
    assert _entry(surface, second).spellings == ("two.verbose", "app.two.verbose")


def test_peers_of_other_owners_are_ambiguous_even_with_an_owned_unrelated_name() -> None:
    first = _param("lib/one", "verbose", owner="/pkgs/lib1")
    second = _param("lib/two", "verbose", owner="/pkgs/lib2")
    owned = _param("app/helper", "level", owner="/pkgs/app")
    surface = build_param_surface(frozenset(), _program(owner="/pkgs/app"), (first, second, owned))

    assert surface.ambiguous["verbose"] == (first, second)
    assert "level" not in surface.ambiguous
    assert _entry(surface, owned).spellings[0] == "level"


def test_an_unknown_owner_is_nobodys_peer() -> None:
    first = _param("app/one", "verbose")
    second = _param("app/two", "verbose", owner="/pkgs/app")
    surface = build_param_surface(frozenset(), _program(), (first, second))

    assert surface.ambiguous["verbose"] == (first, second)


def test_an_owned_spelling_shadows_other_owners_options_and_shorts() -> None:
    own = _param("app/helper", "level", short="l", owner="/pkgs/app")
    other = _param("lib/logging", "level", short="l", owner="/pkgs/lib")
    surface = build_param_surface(frozenset(), _program(owner="/pkgs/app"), (other, own))

    assert _entry(surface, own).short_option == "-l"
    assert _entry(surface, other).short_option is None
    assert "--level" not in _entry(surface, other).option_spellings
    assert "-l" not in surface.ambiguous_options


def test_import_bare_collision_is_ambiguous_but_qualified_forms_resolve() -> None:
    first = _param("first/logging", "verbose")
    second = _param("second/logging", "verbose")
    surface = build_param_surface(frozenset(), _program(), (first, second))

    assert surface.ambiguous["verbose"] == (first, second)
    assert "verbose" not in _entry(surface, first).spellings
    assert _entry(surface, first).spellings == ("first.logging.verbose",)
    assert _entry(surface, second).spellings == ("second.logging.verbose",)
    assert surface.ambiguous["logging.verbose"] == (first, second)
    assert "--verbose" in surface.ambiguous_options


def test_own_module_claims_a_bare_name_before_an_import() -> None:
    own = _param("app/main", "verbose")
    imported = _param("lib/logging", "verbose")
    surface = build_param_surface(frozenset(), _program(), (imported, own))

    assert _entry(surface, own).spellings[0] == "verbose"
    assert "verbose" not in _entry(surface, imported).spellings
    assert "verbose" not in surface.ambiguous


def test_root_and_scope_params_get_each_module_suffix_route_shortest_first() -> None:
    root = _param("A/logging", "verbose")
    scoped = _param("A/logging", "trace", scope=("debug",))
    surface = build_param_surface(frozenset(), _program(), (root, scoped))

    assert _entry(surface, root).spellings == (
        "verbose",
        "logging.verbose",
        "A.logging.verbose",
    )
    assert _entry(surface, scoped).spellings == (
        "trace",
        "logging.debug.trace",
        "A.logging.debug.trace",
    )


def test_option_name_and_short_spelling_are_resolved_on_the_same_level() -> None:
    param = _param("A/logging", "trace", external="trace-level", short="t")
    surface = build_param_surface(frozenset(), _program(), (param,))
    entry = _entry(surface, param)

    assert entry.section == "A/logging"
    assert entry.spellings == (
        "trace-level",
        "logging.trace-level",
        "A.logging.trace-level",
    )
    assert entry.option_spellings == (
        "--trace-level",
        "--no-trace-level",
        "--logging.trace-level",
        "--no-logging.trace-level",
        "--A.logging.trace-level",
        "--no-A.logging.trace-level",
        "-t",
    )


def test_hidden_param_remains_resolvable_and_is_marked_hidden() -> None:
    param = _param("A/logging", "quiet", hidden=True)
    surface = build_param_surface(frozenset(), _program(), (param,))

    entry = _entry(surface, param)
    assert entry.hidden is True
    assert entry.spellings[0] == "quiet"


def test_exec_and_registered_host_surfaces_shadow_different_bare_names() -> None:
    call_depth = _param("A/logging", "max-call-depth", IntType())
    echo = _param("A/logging", "echo")
    exec_surface = build_param_surface(EXEC_RESERVED_FLAGS, _program(), (echo, call_depth))
    registered_surface = build_param_surface(
        REGISTERED_RESERVED_FLAGS, _program(), (echo, call_depth)
    )

    for surface in (exec_surface, registered_surface):
        assert "max-call-depth" not in _entry(surface, call_depth).spellings
        assert "echo" not in _entry(surface, echo).spellings


def test_exec_only_reserves_flags_registered_commands_leave_to_the_program() -> None:
    verbose = _param("A/logging", "warning")
    exec_surface = build_param_surface(EXEC_RESERVED_FLAGS, _program(), (verbose,))
    registered_surface = build_param_surface(REGISTERED_RESERVED_FLAGS, _program(), (verbose,))

    assert "warning" not in _entry(exec_surface, verbose).spellings
    assert "warning" in _entry(registered_surface, verbose).spellings


def test_only_negatable_projections_contribute_negation_spellings() -> None:
    flag = _param("A/logging", "verbose", BoolType())
    text = _param("A/logging", "label", TextType())
    surface = build_param_surface(frozenset(), _program(), (flag, text))

    assert "--no-verbose" in _entry(surface, flag).option_spellings
    text_spellings = _entry(surface, text).option_spellings
    assert all("--no-label" not in spelling for spelling in text_spellings)


def test_entries_are_ordered_by_the_shortest_resolving_spelling() -> None:
    param = _param("A/B/logging", "verbose", scope=("debug",))
    surface = build_param_surface(frozenset(), _program(), (param,))

    assert _entry(surface, param).spellings == (
        "verbose",
        "logging.debug.verbose",
        "B.logging.debug.verbose",
        "A.B.logging.debug.verbose",
    )


def test_anonymous_entry_param_has_only_its_cli_bare_spelling() -> None:
    param = replace(_param("app/main", "verbose"), module=ENTRY_ID)
    surface = build_param_surface(frozenset(), _program(), (param,))

    entry = _entry(surface, param)
    assert entry.spellings == ("verbose",)
    assert entry.section == "<entry>"


def test_signature_short_and_positional_parameters_claim_only_their_valid_flags() -> None:
    param = _param("app/main", "verbose", short="v")
    positional = _param("app/main", "positional")
    signature = (
        _signature("quiet", short="v"),
        _signature("positional", kind=ParamZone.POSITIONAL_ONLY),
    )
    surface = build_param_surface(frozenset(), _program(*signature), (param, positional))

    assert "-v" not in _entry(surface, param).option_spellings
    assert _entry(surface, param).short_option is None
    # A positional-only signature parameter claims its bare name (and so every
    # flag derived from it) without claiming any flag itself, so no live
    # ``--positional`` is left for the module parameter, only its qualified flags.
    assert "positional" not in _entry(surface, positional).spellings
    assert "--positional" not in _entry(surface, positional).option_spellings
    assert "--main.positional" in _entry(surface, positional).option_spellings


def _engine_surface(
    params: tuple[HostParamInfo, ...] = (),
    signature: tuple[ProgramParamInfo, ...] = (),
    *,
    program: bool = True,
) -> ParamSurface:
    return build_param_surface(
        EXEC_RESERVED_FLAGS,
        _program(*signature) if program else None,
        (*params, *engine_setting_params()),
        bare_only=not program,
    )


def _engine(surface: ParamSurface, name: str) -> ParamSurfaceEntry:
    return next(e for e in surface.entries if e.param.name == name and e.param.kind is ENGINE)


def test_an_engine_setting_has_a_bare_and_a_qualified_spelling_and_flags() -> None:
    entry = _engine(_engine_surface(), "timeout")

    assert entry.spellings == ("timeout", "std.config.timeout")
    assert entry.positive_option_spellings == ("--timeout", "--std.config.timeout")
    assert entry.negative_option_spellings == ("--no-timeout", "--no-std.config.timeout")


def test_a_register_enabling_setting_has_no_negative_flag() -> None:
    entry = _engine(_engine_surface(), "trace-file")

    assert entry.negative_option_spellings == ()


def test_a_signature_parameter_shadows_the_bare_engine_spelling_only() -> None:
    entry = _engine(_engine_surface(signature=(_signature("timeout"),)), "timeout")

    assert entry.spellings == ("std.config.timeout",)
    assert entry.positive_option_spellings == ("--std.config.timeout",)
    assert entry.negative_option_spellings == ("--no-std.config.timeout",)


def test_a_shadowed_setting_keeps_no_flag_derived_from_the_lost_name() -> None:
    param = _param("A/logging", "timeout")
    entry = _engine(_engine_surface((param,)), "timeout")

    assert "--no-timeout" not in entry.option_spellings
    assert "--timeout" not in entry.option_spellings


def test_a_module_parameter_shadows_the_bare_engine_spelling_at_its_level() -> None:
    param = _param("A/logging", "debug")
    surface = _engine_surface((param,))

    assert _entry(surface, param).spellings[0] == "debug"
    assert _engine(surface, "debug").spellings == ("std.config.debug",)
    assert "debug" not in surface.ambiguous


def test_a_module_parameters_qualified_spelling_does_not_touch_the_engine_setting() -> None:
    param = _param("A/logging", "debug")
    surface = _engine_surface((param,))

    assert _entry(surface, param).spellings[1:] == ("logging.debug", "A.logging.debug")
    assert _engine(surface, "debug").spellings == ("std.config.debug",)


def test_engine_settings_follow_the_module_parameters_in_entry_order() -> None:
    param = _param("A/logging", "zzz")
    surface = _engine_surface((param,))

    assert surface.entries[0].param == param
    assert all(e.param.kind is ENGINE for e in surface.entries[1:])


def test_engine_settings_without_a_program_take_only_their_bare_spelling() -> None:
    entry = _engine(_engine_surface(program=False), "timeout")

    assert entry.spellings == ("timeout",)
    assert entry.positive_option_spellings == ("--timeout",)
    assert entry.negative_option_spellings == ("--no-timeout",)
