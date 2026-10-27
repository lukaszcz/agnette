"""Tests for the project-local package store: layering, selection, and installation."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import semver

import agnette.agm.commands.pkg.info as info_command
import agnette.agm.commands.pkg.list as list_command
from agnette.agm.cli_support.args import PkgInfoArgs, PkgListArgs
from agnette.agm.cli_support.pkg_store import (
    pkg_target,
    warn_if_project_selection_broken,
)
from agnette.agm.project.layout import project_packages_dir
from agnette.config.context import ConfigContext
from agnette.core import dry_run
from agnette.packages.activation import (
    ActivationIndex,
    ActivePackage,
    PackageActivationError,
    activating_store,
    by_command_priority,
    effective_command_index,
    load_activation_index,
    resolve_indexed_packages,
    select_active_packages,
    select_package_roots,
    write_activation_index,
)
from agnette.packages.archive import write_archive
from agnette.packages.command_registry import command_registry
from agnette.packages.dependencies import DependencyError, validate_dependencies
from agnette.packages.development import (
    containing_development_package,
    discover_development_packages,
)
from agnette.packages.install import (
    PackageInstallError,
    activate_installed_package_with_plan,
    install_archive_with_plan,
    install_directory_with_plan,
    sync_active_python_dependencies,
    uninstall_package,
)
from agnette.packages.layout import MODULE_TREE_DIRNAME, StoreScope
from agnette.packages.model import PackageInfo
from agnette.packages.store import (
    PackageStore,
    StoreLayers,
    context_layers,
    global_store,
    project_store,
)
from agnette.version import AGM_VERSION

V1 = semver.Version.parse("1.0.0")


def _source(
    root: Path,
    name: str,
    version: str = "1.0.0",
    *,
    dependencies: str = "",
    body: str = "program def main() -> unit = ()\n",
) -> Path:
    (root / MODULE_TREE_DIRNAME).mkdir(parents=True)
    (root / "package.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "{version}"\n{dependencies}', encoding="utf-8"
    )
    (root / MODULE_TREE_DIRNAME / "main.agl").write_text(body, encoding="utf-8")
    return root


class _World:
    """A global store under an AgM home, plus a project directory beside it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.env = {"AGM_HOME": str(self.home / ".agm")}
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.global_store = global_store(home=self.home, env=self.env)
        self.project_store = project_store(self.project)
        self.layers = StoreLayers(self.global_store, self.project_store)

    def install_global(self, source: Path, **kwargs: bool) -> PackageInfo:
        return install_directory_with_plan(StoreLayers(self.global_store), source, **kwargs).package

    def install_project(self, source: Path, **kwargs: bool) -> PackageInfo:
        return install_directory_with_plan(self.layers, source, **kwargs).package

    def select(self, *, inside: bool = True) -> dict[str, PackageInfo]:
        packages = select_active_packages(
            home=self.home,
            proj_dir=self.project if inside else None,
            cwd=self.project,
            env=self.env,
        )
        return {package.manifest.name: package for package in packages}

    def pin(self, **pins: str) -> None:
        config = self.project / "config"
        config.mkdir(exist_ok=True)
        lines = "".join(f'{name} = "{version}"\n' for name, version in pins.items())
        (config / "config.toml").write_text(f"[packages]\n{lines}", encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


def test_project_packages_dir_is_the_packages_directory_of_the_project(tmp_path: Path) -> None:
    assert project_packages_dir(tmp_path) == tmp_path / "packages"


def test_project_store_is_a_project_scoped_store_beneath_the_project(tmp_path: Path) -> None:
    store = project_store(tmp_path)

    assert store == PackageStore(tmp_path / "packages", StoreScope.PROJECT)
    assert store.index_path == tmp_path / "packages" / "index.toml"
    assert global_store(home=tmp_path / "home", env={}).scope is StoreScope.GLOBAL


def test_layers_outside_a_project_are_the_global_store_alone(world: _World) -> None:
    layers = context_layers(home=world.home, proj_dir=None, env=world.env)

    assert layers.stores == (world.global_store,)


def test_layers_inside_a_project_put_the_project_store_first(world: _World) -> None:
    layers = context_layers(home=world.home, proj_dir=world.project, env=world.env)

    assert layers.stores == (world.project_store, world.global_store)
    assert layers.project == world.project_store


def test_layers_ignore_a_project_store_that_is_the_global_store(tmp_path: Path) -> None:
    env = {"AGM_HOME": str(tmp_path)}

    layers = context_layers(home=tmp_path, proj_dir=tmp_path, env=env)

    assert layers.project is None


def test_layers_recognize_the_trees_of_both_stores(world: _World) -> None:
    layers = StoreLayers(world.global_store, world.project_store)
    version = semver.Version.parse("1.0.0")

    assert layers.is_package_root(world.global_store.package_path("a", version), "a", version)
    assert layers.is_package_root(world.project_store.package_path("a", version), "a", version)
    assert not layers.is_package_root(world.tmp / "a", "a", version)


def test_development_discovery_leaves_project_store_trees_to_selection(world: _World) -> None:
    package = world.install_project(_source(world.tmp / "src-alpha", "alpha"))
    layers = context_layers(home=world.home, proj_dir=world.project, env=world.env)

    assert containing_development_package(layers, package.root) is None
    assert discover_development_packages(layers, package.root) == ()
    unlayered = StoreLayers(world.global_store)
    assert containing_development_package(unlayered, package.root) is not None


def test_project_install_creates_the_project_store_lazily(world: _World) -> None:
    assert not project_packages_dir(world.project).exists()

    package = world.install_project(_source(world.tmp / "src-alpha", "alpha"))

    assert package.root == world.project_store.package_path("alpha", package.manifest.version)
    assert world.project_store.index_path.is_file()
    assert not world.global_store.root.exists()


def test_a_project_package_is_selected_only_inside_the_project(world: _World) -> None:
    world.install_project(_source(world.tmp / "src-alpha", "alpha"))

    inside = world.select()["alpha"]

    assert inside.store_scope is StoreScope.PROJECT
    assert inside.installed
    assert world.select(inside=False) == {}


def test_global_packages_keep_the_global_scope_inside_a_project(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-alpha", "alpha"))

    assert world.select()["alpha"].store_scope is StoreScope.GLOBAL


def test_an_editable_project_package_is_live_source(world: _World) -> None:
    source = _source(world.tmp / "src-alpha", "alpha")

    world.install_project(source, editable=True)
    package = world.select()["alpha"]

    assert package.store_scope is StoreScope.PROJECT
    assert not package.installed
    assert package.root == source.resolve()


def test_the_project_index_replaces_a_same_named_global_entry(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-g", "alpha", "1.0.0"))
    world.install_global(_source(world.tmp / "src-h", "bravo"))
    world.install_project(_source(world.tmp / "src-p", "alpha", "2.0.0"))

    selected = world.select()

    assert str(selected["alpha"].manifest.version) == "2.0.0"
    assert selected["alpha"].store_scope is StoreScope.PROJECT
    assert selected["bravo"].store_scope is StoreScope.GLOBAL
    assert str(world.select(inside=False)["alpha"].manifest.version) == "1.0.0"


def test_package_roots_inside_a_project_mount_both_stores(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-g", "alpha"))
    world.install_project(_source(world.tmp / "src-p", "bravo"))

    packages = select_package_roots(
        home=world.home, proj_dir=world.project, cwd=world.project, env=world.env
    )

    assert [package.manifest.name for package in packages] == ["bravo", "alpha"]


def test_package_roots_follow_command_priority_so_the_first_registrant_owns_a_path(
    world: _World,
) -> None:
    world.install_global(_go_source(world, "alpha"))
    world.install_global(_go_source(world, "bravo"), shadow=True)
    world.install_project(_go_source(world, "carol"))
    world.install_project(_go_source(world, "delta"), shadow=True)

    packages = select_package_roots(
        home=world.home, proj_dir=world.project, cwd=world.project, env=world.env
    )
    registry = command_registry(packages)

    assert [package.manifest.name for package in packages] == ["delta", "carol", "bravo", "alpha"]
    assert registry[("go",)].package.manifest.name == "delta"


def test_command_priority_ranks_development_checkouts_as_the_stores_do(world: _World) -> None:
    world.install_global(_go_source(world, "alpha"))
    world.install_global(_go_source(world, "bravo"), shadow=True)
    world.install_project(_go_source(world, "carol", command="other"))
    checkouts = [
        PackageInfo.live_source(_go_source(world, name, "9.0.0")) for name in ("zulu", "alpha")
    ]
    mounted = (*checkouts, *world.select().values())

    ranked = by_command_priority(
        mounted, home=world.home, proj_dir=world.project, cwd=world.project, env=world.env
    )

    assert [(p.manifest.name, p.manifest.version.major) for p in ranked] == [
        ("carol", 1),
        ("bravo", 1),
        ("alpha", 9),
        ("alpha", 1),
        ("zulu", 9),
    ]
    assert command_registry(ranked)[("go",)].package.manifest.name == "bravo"
    assert command_registry(mounted)[("go",)].package.manifest.name == "zulu"


def test_a_pin_resolves_the_project_store_before_the_global_one(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-g", "alpha", body="let x = 1\n"))
    project_alpha = world.install_project(_source(world.tmp / "src-p", "alpha", "2.0.0"))
    world.install_project(_source(world.tmp / "src-q", "alpha", "1.0.0", body="let y = 1\n"))
    world.pin(alpha="1.0.0")

    pinned = world.select()["alpha"]

    assert pinned.store_scope is StoreScope.PROJECT
    assert pinned.root == project_alpha.root.parent / "1.0.0"
    assert (pinned.root / MODULE_TREE_DIRNAME / "main.agl").read_text() == "let y = 1\n"


def test_a_pin_falls_back_to_the_global_store(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-g", "alpha", "1.0.0"))
    world.install_global(_source(world.tmp / "src-h", "alpha", "2.0.0"))
    world.install_project(_source(world.tmp / "src-p", "alpha", "3.0.0"))
    world.pin(alpha="1.0.0")

    pinned = world.select()["alpha"]

    assert pinned.store_scope is StoreScope.GLOBAL
    assert str(pinned.manifest.version) == "1.0.0"


def test_a_pin_naming_no_stored_version_is_an_error(world: _World) -> None:
    world.install_project(_source(world.tmp / "src-p", "alpha", "3.0.0"))
    world.pin(alpha="9.0.0")

    with pytest.raises(PackageActivationError):
        world.select()


def _go_source(world: _World, name: str, version: str = "1.0.0", *, command: str = "go") -> Path:
    """A package registering *command*."""
    return _source(
        world.tmp / f"src-{name}-{version}",
        name,
        version,
        dependencies=f'\n[commands]\n{command} = {{ program = "{name}/main::main" }}\n',
    )


def _commands(world: _World) -> dict[str, str]:
    index = effective_command_index(
        home=world.home, proj_dir=world.project, cwd=world.project, env=world.env
    )
    return {path: registration.package for path, registration in index.commands.items()}


def test_project_registrations_rank_above_every_global_one_without_shadow(world: _World) -> None:
    world.install_global(_go_source(world, "alpha"))
    world.install_global(_go_source(world, "bravo"), shadow=True)
    world.install_project(_go_source(world, "carol"))

    assert _commands(world) == {"go": "carol"}


def test_a_project_install_reports_the_global_commands_it_shadows(world: _World) -> None:
    world.install_global(_go_source(world, "alpha"))
    world.install_global(_go_source(world, "bravo", command="stay"))

    plan = install_directory_with_plan(world.layers, _go_source(world, "carol"))

    assert [(shadow.path_name, shadow.displaced_packages) for shadow in plan.command_shadows] == [
        ("go", ("alpha",))
    ]


def test_a_project_package_replacing_a_global_one_shadows_none_of_its_commands(
    world: _World,
) -> None:
    world.install_global(_go_source(world, "alpha"))

    plan = install_directory_with_plan(
        world.layers,
        _go_source(world, "alpha", "2.0.0", command="went"),
    )

    assert plan.command_shadows == ()


def test_a_project_package_replacing_a_global_one_registers_its_commands(world: _World) -> None:
    world.install_global(_go_source(world, "alpha"))
    world.install_project(_go_source(world, "alpha", "2.0.0", command="went"))

    assert _commands(world) == {"went": "alpha"}


def test_registrations_conflicting_within_the_project_store_need_shadow(world: _World) -> None:
    world.install_project(_go_source(world, "alpha"))
    rival = _go_source(world, "bravo")

    with pytest.raises(PackageInstallError):
        world.install_project(rival)
    world.install_project(rival, shadow=True)

    assert _commands(world) == {"go": "bravo"}


def test_a_pinned_project_package_keeps_its_priority_over_global_registrations(
    world: _World,
) -> None:
    world.install_global(_go_source(world, "alpha"))
    world.install_project(_go_source(world, "bravo"))
    world.install_project(_go_source(world, "bravo", "2.0.0"))
    world.pin(bravo="1.0.0")

    assert _commands(world) == {"go": "bravo"}


def test_the_activating_store_is_the_one_whose_layered_entry_selects_the_package(
    world: _World,
) -> None:
    world.install_global(_source(world.tmp / "src-g", "alpha"))
    world.install_global(_source(world.tmp / "src-h", "bravo"))
    project_alpha = world.install_project(_source(world.tmp / "src-p", "alpha", "2.0.0"))
    global_alpha = PackageInfo.live_source(world.global_store.package_path("alpha", V1))
    layers = context_layers(home=world.home, proj_dir=world.project, env=world.env)

    assert activating_store(layers, project_alpha) == world.project_store
    assert activating_store(layers, world.select()["bravo"]) == world.global_store
    assert activating_store(layers, global_alpha) is None
    assert activating_store(layers, PackageInfo.live_source(world.tmp / "src-g")) is None


def test_resolution_memo_distinguishes_stores_holding_the_same_names(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-g", "alpha"))
    world.install_project(_source(world.tmp / "src-p", "alpha"))
    selection = ActivationIndex({"alpha": ActivePackage(semver.Version.parse("1.0.0"))})

    global_alpha = resolve_indexed_packages(world.global_store, selection)[0]
    project_alpha = resolve_indexed_packages(world.project_store, selection)[0]

    assert global_alpha.root != project_alpha.root
    assert global_alpha.store_scope is StoreScope.GLOBAL
    assert project_alpha.store_scope is StoreScope.PROJECT


def test_a_project_install_uses_the_global_selection_for_dependencies(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-d", "base"))
    deps = '\n[dependencies]\nbase = "1.0"\n'

    package = world.install_project(_source(world.tmp / "src-p", "app", dependencies=deps))

    assert package.store_scope is StoreScope.PROJECT
    assert set(load_activation_index(world.project_store).packages) == {"app"}
    assert set(world.select()) == {"app", "base"}
    assert not (world.project_store.root / "base").exists()


def test_a_project_install_prefers_a_project_dependency(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-d", "base", "1.0.0"))
    world.install_project(_source(world.tmp / "src-e", "base", "2.0.0"))
    deps = '\n[dependencies]\nbase = "1.0"\n'

    world.install_project(_source(world.tmp / "src-p", "app", dependencies=deps))

    assert str(world.select()["base"].manifest.version) == "2.0.0"


def test_a_project_install_places_a_missing_path_dependency_in_the_project_store(
    world: _World,
) -> None:
    _source(world.tmp / "base-source", "base")
    deps = '\n[dependencies]\nbase = { version = "1.0.0", path = "../base-source" }\n'

    world.install_project(_source(world.tmp / "src-p", "app", dependencies=deps))

    assert set(load_activation_index(world.project_store).packages) == {"app", "base"}
    assert not world.global_store.root.exists()


def test_a_project_install_replaces_a_global_dependency_that_is_too_old(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-d", "base", "1.0.0"))
    _source(world.tmp / "base-source", "base", "2.0.0")
    deps = '\n[dependencies]\nbase = { version = "2.0.0", path = "../base-source" }\n'

    world.install_project(_source(world.tmp / "src-p", "app", dependencies=deps))

    assert str(world.select()["base"].manifest.version) == "2.0.0"
    assert str(world.select(inside=False)["base"].manifest.version) == "1.0.0"


def test_a_project_selection_shadows_the_global_one_for_dependency_installs(
    world: _World,
) -> None:
    world.install_global(_source(world.tmp / "src-g", "base", "2.0.0"))
    world.install_project(_source(world.tmp / "src-o", "base", "1.0.0"))
    _source(world.tmp / "base-2", "base", "2.0.0")
    deps = '\n[dependencies]\nbase = { version = "2.0.0", path = "../base-2" }\n'

    world.install_project(_source(world.tmp / "src-p", "app", dependencies=deps))

    assert str(world.select()["base"].manifest.version) == "2.0.0"
    assert world.select()["base"].store_scope is StoreScope.PROJECT


def test_a_project_install_with_an_unsatisfied_dependency_fails(world: _World) -> None:
    deps = '\n[dependencies]\nbase = "1.0"\n'

    with pytest.raises(PackageInstallError, match="base"):
        world.install_project(_source(world.tmp / "src-p", "app", dependencies=deps))


def test_a_global_install_never_sees_the_project_store(world: _World) -> None:
    world.install_project(_source(world.tmp / "src-d", "base"))
    deps = '\n[dependencies]\nbase = "1.0"\n'

    with pytest.raises(PackageInstallError, match="base"):
        world.install_global(_source(world.tmp / "src-p", "app", dependencies=deps))


def test_a_project_install_checks_the_effective_selection_against_global_packages(
    world: _World,
) -> None:
    world.install_global(_source(world.tmp / "src-d", "base", "2.0.0"))
    deps = '\n[dependencies]\nbase = "2.0"\n'
    world.install_global(_source(world.tmp / "src-a", "app", dependencies=deps))

    with pytest.raises(PackageInstallError, match="base"):
        world.install_project(_source(world.tmp / "src-o", "base", "1.0.0"))

    assert not load_activation_index(world.project_store).packages


def test_a_project_install_rejects_the_managed_std_package(world: _World) -> None:
    std = _source(world.tmp / "src-std", "std", AGM_VERSION)

    with pytest.raises(PackageInstallError, match="std"):
        world.install_project(std)

    assert not world.project_store.index_path.exists()


def test_a_project_archive_install_is_project_scoped(world: _World) -> None:
    archive = world.tmp / "alpha.agmpkg"
    write_archive(_source(world.tmp / "src-a", "alpha"), archive)

    package = install_archive_with_plan(world.layers, archive).package

    assert package.root.is_relative_to(world.project_store.root)
    assert world.select(inside=False) == {}


def test_a_project_reinstall_replaces_the_project_tree(world: _World) -> None:
    source = _source(world.tmp / "src-a", "alpha")
    world.install_project(source)
    (source / MODULE_TREE_DIRNAME / "main.agl").write_text("let z = 1\n", encoding="utf-8")

    package = world.install_project(source, reinstall=True)

    assert (package.root / MODULE_TREE_DIRNAME / "main.agl").read_text() == "let z = 1\n"


def test_project_switch_activates_a_stored_project_version(world: _World) -> None:
    world.install_project(_source(world.tmp / "src-a", "alpha", "1.0.0"))
    world.install_project(_source(world.tmp / "src-b", "alpha", "2.0.0"))

    activate_installed_package_with_plan(world.layers, "alpha", semver.Version.parse("1.0.0"))

    assert str(world.select()["alpha"].manifest.version) == "1.0.0"


def test_project_uninstall_restores_the_global_selection(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-g", "alpha", "1.0.0"))
    world.install_project(_source(world.tmp / "src-p", "alpha", "2.0.0"))

    uninstall_package(world.layers, "alpha")

    assert str(world.select()["alpha"].manifest.version) == "1.0.0"
    assert world.select()["alpha"].store_scope is StoreScope.GLOBAL


def test_project_uninstall_checks_the_effective_selection(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-d", "base", "1.0.0"))
    deps = '\n[dependencies]\nbase = "2.0"\n'
    world.install_project(_source(world.tmp / "src-e", "base", "2.0.0"))
    world.install_project(_source(world.tmp / "src-p", "app", dependencies=deps))

    with pytest.raises(PackageInstallError, match="base"):
        uninstall_package(world.layers, "base")

    assert set(world.select()) == {"app", "base"}


def test_dependency_validation_inherits_the_global_selection(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-d", "base"))
    package = PackageInfo.live_source(
        _source(world.tmp / "src-p", "app", dependencies='\n[dependencies]\nbase = "1.0"\n')
    )

    resolved = validate_dependencies(world.layers, package)

    assert [dependency.manifest.name for dependency in resolved] == ["base"]
    assert resolved[0].root.is_relative_to(world.global_store.root)


def test_dependency_validation_ignores_a_global_package_the_project_shadows(
    world: _World,
) -> None:
    world.install_global(_source(world.tmp / "src-g", "base", "2.0.0"))
    world.install_project(_source(world.tmp / "src-o", "base", "1.0.0"))
    package = PackageInfo.live_source(
        _source(world.tmp / "src-p", "app", dependencies='\n[dependencies]\nbase = "2.0"\n')
    )

    with pytest.raises(DependencyError, match="base"):
        validate_dependencies(world.layers, package)


def _corrupt_global_index(world: _World) -> None:
    world.global_store.root.mkdir(parents=True)
    world.global_store.index_path.write_text("[packages\n", encoding="utf-8")


def test_a_project_install_reports_an_unreadable_global_selection(world: _World) -> None:
    _corrupt_global_index(world)

    with pytest.raises(PackageInstallError, match="activation"):
        world.install_project(_source(world.tmp / "src-a", "alpha"))


def test_dependency_validation_reports_an_unreadable_global_selection(world: _World) -> None:
    _corrupt_global_index(world)
    package = PackageInfo.live_source(_source(world.tmp / "src-a", "alpha"))

    with pytest.raises(DependencyError):
        validate_dependencies(world.layers, package)


def test_pkg_target_selects_the_project_store_only_on_request(world: _World) -> None:
    inside = ConfigContext(home=world.home, proj_dir=world.project, cwd=world.project)
    outside = ConfigContext(home=world.home, proj_dir=None, cwd=world.tmp)
    layers = context_layers(home=world.home, proj_dir=world.project)

    assert pkg_target(inside, project=False).stores == (layers.global_store,)
    assert pkg_target(inside, project=True).stores == (layers.project, layers.global_store)
    with pytest.raises(PackageInstallError):
        pkg_target(outside, project=True)


def test_pkg_target_rejects_project_when_the_home_is_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGM_HOME", str(tmp_path))
    home_is_project = ConfigContext(home=tmp_path, proj_dir=tmp_path, cwd=tmp_path)
    no_project = ConfigContext(home=tmp_path, proj_dir=None, cwd=tmp_path)

    with pytest.raises(PackageInstallError):
        pkg_target(home_is_project, project=True)
    with pytest.raises(PackageInstallError):
        pkg_target(no_project, project=True)


@pytest.fixture
def shown(world: _World, monkeypatch: pytest.MonkeyPatch) -> ConfigContext:
    """Both stores populated: a shadowed global and editable package, and a project dependent."""
    monkeypatch.setenv("AGM_HOME", world.env["AGM_HOME"])
    world.install_global(_source(world.tmp / "src-a", "alpha", "1.0.0"))
    world.install_global(_source(world.tmp / "src-b", "bravo", "1.0.0"))
    world.install_global(_source(world.tmp / "src-e", "echo", "1.0.0"), editable=True)
    world.install_project(_source(world.tmp / "src-a2", "alpha", "2.0.0"))
    world.install_project(_source(world.tmp / "src-e2", "echo", "2.0.0"), editable=True)
    deps = '\n[dependencies]\nbravo = "1"\n'
    world.install_project(_source(world.tmp / "src-c", "carol", dependencies=deps))
    context = ConfigContext(home=world.home, proj_dir=world.project, cwd=world.project)
    monkeypatch.setattr(list_command, "current_config_context", lambda: context)
    monkeypatch.setattr(info_command, "current_config_context", lambda: context)
    return context


def test_list_inside_a_project_labels_both_stores_and_marks_shadowed_packages(
    shown: ConfigContext, capsys: pytest.CaptureFixture[str]
) -> None:
    list_command.run(PkgListArgs())

    assert capsys.readouterr().out.splitlines() == [
        "alpha 1.0.0 active global (shadowed)",
        "bravo 1.0.0 active global",
        "echo 1.0.0 editable global (shadowed)",
        "alpha 2.0.0 active project",
        "carol 1.0.0 active project",
        "echo 2.0.0 editable project",
    ]


def test_list_outside_a_project_shows_the_global_store_unlabelled(
    shown: ConfigContext, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = ConfigContext(home=shown.home, proj_dir=None, cwd=shown.cwd)
    monkeypatch.setattr(list_command, "current_config_context", lambda: outside)

    list_command.run(PkgListArgs())

    assert capsys.readouterr().out.splitlines() == [
        "alpha 1.0.0 active",
        "bravo 1.0.0 active",
        "echo 1.0.0 editable",
    ]


def test_info_inside_a_project_names_the_store_and_the_global_package_it_shadows(
    shown: ConfigContext, capsys: pytest.CaptureFixture[str]
) -> None:
    info_command.run(PkgInfoArgs("alpha"))
    info_command.run(PkgInfoArgs("bravo"))
    info_command.run(PkgInfoArgs("carol"))

    lines = capsys.readouterr().out.splitlines()
    assert "store: project (shadows global 1.0.0)" in lines
    assert "store: global" in lines
    assert "store: project" in lines
    assert "requires bravo >= 1.0.0: active 1.0.0" in lines


def test_info_of_a_project_package_tolerates_a_shadowed_editable_whose_source_is_gone(
    shown: ConfigContext, world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.rmtree(world.tmp / "src-e")

    info_command.run(PkgInfoArgs("echo"))

    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == ["echo 2.0.0", "store: project (shadows global 1.0.0)"]


def test_info_outside_a_project_ignores_project_packages(
    shown: ConfigContext, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = ConfigContext(home=shown.home, proj_dir=None, cwd=shown.cwd)
    monkeypatch.setattr(info_command, "current_config_context", lambda: outside)

    info_command.run(PkgInfoArgs("alpha"))
    with pytest.raises(SystemExit):
        info_command.run(PkgInfoArgs("carol"))

    assert capsys.readouterr().out == "alpha 1.0.0\n"


@pytest.fixture
def pin_context(world: _World, monkeypatch: pytest.MonkeyPatch) -> ConfigContext:
    """Global alpha 1.0.0 active; project alpha 2.0.0 stored, not in the project index."""
    monkeypatch.setenv("AGM_HOME", world.env["AGM_HOME"])
    world.install_global(_source(world.tmp / "src-a", "alpha", "1.0.0"))
    world.install_project(_source(world.tmp / "src-a2", "alpha", "2.0.0"))
    write_activation_index(world.project_store, ActivationIndex({}))
    context = ConfigContext(home=world.home, proj_dir=world.project, cwd=world.project)
    monkeypatch.setattr(list_command, "current_config_context", lambda: context)
    monkeypatch.setattr(info_command, "current_config_context", lambda: context)
    return context


def test_list_shows_a_project_pin_selecting_a_project_package_as_shadowing_the_global_one(
    pin_context: ConfigContext, world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.pin(alpha="2.0.0")

    list_command.run(PkgListArgs())

    assert capsys.readouterr().out.splitlines() == [
        "alpha 1.0.0 active global (shadowed)",
        "alpha 2.0.0 active project",
    ]


def test_list_keeps_an_editable_entry_a_same_store_pin_replaces_as_shadowed(
    pin_context: ConfigContext, world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install_project(_source(world.tmp / "src-a3", "alpha", "3.0.0"), editable=True)
    world.pin(alpha="2.0.0")

    list_command.run(PkgListArgs())

    assert capsys.readouterr().out.splitlines() == [
        "alpha 1.0.0 active global (shadowed)",
        "alpha 2.0.0 active project",
        "alpha 3.0.0 editable project (shadowed)",
    ]


def test_list_marks_a_project_entry_a_pin_to_a_global_version_replaces_as_shadowed(
    pin_context: ConfigContext, world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install_global(_source(world.tmp / "src-g2", "alpha", "4.0.0"))
    world.install_project(_source(world.tmp / "src-a3", "alpha", "3.0.0"))
    world.pin(alpha="1.0.0")

    list_command.run(PkgListArgs())

    assert capsys.readouterr().out.splitlines() == [
        "alpha 1.0.0 active global",
        "alpha 4.0.0 active global (shadowed)",
        "alpha 2.0.0 installed project",
        "alpha 3.0.0 active project (shadowed)",
    ]


def test_list_and_info_accept_a_replaced_editable_entry_whose_source_is_gone(
    pin_context: ConfigContext, world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install_project(_source(world.tmp / "src-a3", "alpha", "3.0.0"), editable=True)
    world.pin(alpha="2.0.0")
    shutil.rmtree(world.tmp / "src-a3")

    list_command.run(PkgListArgs())
    info_command.run(PkgInfoArgs("alpha"))

    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [
        "alpha 1.0.0 active global (shadowed)",
        "alpha 2.0.0 active project",
        "alpha 3.0.0 editable project (shadowed)",
    ]
    assert "store: project (shadows project 3.0.0, global 1.0.0)" in out


def test_info_names_every_index_selection_a_pin_replaces(
    pin_context: ConfigContext, world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install_global(_source(world.tmp / "src-g2", "alpha", "4.0.0"))
    world.install_project(_source(world.tmp / "src-a3", "alpha", "3.0.0"))
    world.pin(alpha="1.0.0")

    info_command.run(PkgInfoArgs("alpha"))

    assert "store: global (shadows project 3.0.0, global 4.0.0)" in capsys.readouterr().out


def test_list_fails_on_a_pin_to_a_version_that_is_not_installed(
    pin_context: ConfigContext, world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.pin(alpha="9.0.0")

    with pytest.raises(SystemExit):
        list_command.run(PkgListArgs())

    assert capsys.readouterr().out == ""


def test_info_reports_the_project_package_a_project_pin_selects(
    pin_context: ConfigContext, world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.pin(alpha="2.0.0")

    info_command.run(PkgInfoArgs("alpha"))

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "alpha 2.0.0"
    assert "store: project (shadows global 1.0.0)" in lines


def test_list_and_info_follow_a_global_pin_selecting_a_version_the_index_does_not(
    world: _World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGM_HOME", world.env["AGM_HOME"])
    world.install_global(_source(world.tmp / "src-a", "alpha", "1.0.0"))
    world.install_global(_source(world.tmp / "src-a2", "alpha", "2.0.0"))
    (world.home / ".agm" / "config.toml").write_text(
        '[packages]\nalpha = "1.0.0"\n', encoding="utf-8"
    )
    outside = ConfigContext(home=world.home, proj_dir=None, cwd=world.project)
    monkeypatch.setattr(list_command, "current_config_context", lambda: outside)
    monkeypatch.setattr(info_command, "current_config_context", lambda: outside)

    list_command.run(PkgListArgs())
    info_command.run(PkgInfoArgs("alpha"))

    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["alpha 1.0.0 active", "alpha 2.0.0 active (shadowed)"]
    assert out[2:4] == ["alpha 1.0.0", "store: global (shadows global 2.0.0)"]


def test_operations_on_an_absent_store_create_nothing(world: _World) -> None:
    version = semver.Version.parse("1.0.0")

    assert sync_active_python_dependencies(world.project_store) == ()
    with pytest.raises(PackageInstallError, match="alpha"):
        uninstall_package(world.layers, "alpha")
    with pytest.raises(PackageInstallError, match="alpha"):
        activate_installed_package_with_plan(world.layers, "alpha", version)
    assert not world.project_store.root.exists()


@pytest.fixture
def broken_project(world: _World, monkeypatch: pytest.MonkeyPatch) -> ConfigContext:
    """A project whose package depends on a global package that is then uninstalled."""
    monkeypatch.setenv("AGM_HOME", world.env["AGM_HOME"])
    world.install_global(_source(world.tmp / "src-d", "base"))
    deps = '\n[dependencies]\nbase = "1.0"\n'
    world.install_project(_source(world.tmp / "src-p", "app", dependencies=deps))
    uninstall_package(StoreLayers(world.global_store), "base")
    return ConfigContext(home=world.home, proj_dir=world.project, cwd=world.project)


def test_a_broken_project_selection_is_warned_about(
    broken_project: ConfigContext, capsys: pytest.CaptureFixture[str]
) -> None:
    warn_if_project_selection_broken(broken_project, project=False)

    assert "base" in capsys.readouterr().err


def test_a_global_install_conflicting_with_a_pinned_version_is_warned_about(
    world: _World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pin selects a global version the global index lacks, so only the layered view conflicts."""
    monkeypatch.setenv("AGM_HOME", world.env["AGM_HOME"])
    world.install_global(_go_source(world, "alpha", "1.0.0", command="other"))
    world.install_global(_go_source(world, "alpha", "2.0.0"))
    activate_installed_package_with_plan(StoreLayers(world.global_store), "alpha", V1)
    world.pin(alpha="2.0.0")
    world.install_global(_go_source(world, "bravo"))

    warn_if_project_selection_broken(
        ConfigContext(home=world.home, proj_dir=world.project, cwd=world.project), project=False
    )

    assert "go" in capsys.readouterr().err


def test_no_warning_for_a_project_operation_a_dry_run_or_no_project(
    broken_project: ConfigContext, capsys: pytest.CaptureFixture[str]
) -> None:
    warn_if_project_selection_broken(broken_project, project=True)
    warn_if_project_selection_broken(
        ConfigContext(home=broken_project.home, proj_dir=None, cwd=broken_project.cwd),
        project=False,
    )
    dry_run.set_enabled(True)
    try:
        warn_if_project_selection_broken(broken_project, project=False)
    finally:
        dry_run.set_enabled(False)

    assert capsys.readouterr().err == ""
