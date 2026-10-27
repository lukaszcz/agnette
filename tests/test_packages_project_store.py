"""Tests for the project-local package store: layering, selection, and installation."""

from __future__ import annotations

from pathlib import Path

import pytest
import semver

from agnette.agm.cli_support.pkg_store import (
    pkg_resolution_target,
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
    effective_command_index,
    load_activation_index,
    resolve_indexed_packages,
    select_active_packages,
    select_package_roots,
)
from agnette.packages.archive import write_archive
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

    def install_global(self, source: Path, **kwargs: bool) -> PackageInfo:
        return install_directory_with_plan(self.global_store, source, **kwargs).package

    def install_project(self, source: Path, **kwargs: bool) -> PackageInfo:
        return install_directory_with_plan(
            self.project_store, source, fallback=self.global_store, **kwargs
        ).package

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

    assert [package.manifest.name for package in packages] == ["alpha", "bravo"]


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


def test_command_registry_covers_global_packages_only(world: _World) -> None:
    world.install_global(
        _source(
            world.tmp / "src-g",
            "alpha",
            dependencies='\n[commands]\ngo = { program = "alpha/main::main" }\n',
        )
    )
    world.install_project(
        _source(
            world.tmp / "src-p",
            "alpha",
            "2.0.0",
            dependencies='\n[commands]\ngo = { program = "alpha/main::main" }\n',
        )
    )

    index = effective_command_index(
        home=world.home, proj_dir=world.project, cwd=world.project, env=world.env
    )

    assert index.commands == {}


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

    package = install_archive_with_plan(
        world.project_store, archive, fallback=world.global_store
    ).package

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

    activate_installed_package_with_plan(
        world.project_store, "alpha", semver.Version.parse("1.0.0"), fallback=world.global_store
    )

    assert str(world.select()["alpha"].manifest.version) == "1.0.0"


def test_project_uninstall_restores_the_global_selection(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-g", "alpha", "1.0.0"))
    world.install_project(_source(world.tmp / "src-p", "alpha", "2.0.0"))

    uninstall_package(world.project_store, "alpha", fallback=world.global_store)

    assert str(world.select()["alpha"].manifest.version) == "1.0.0"
    assert world.select()["alpha"].store_scope is StoreScope.GLOBAL


def test_project_uninstall_checks_the_effective_selection(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-d", "base", "1.0.0"))
    deps = '\n[dependencies]\nbase = "2.0"\n'
    world.install_project(_source(world.tmp / "src-e", "base", "2.0.0"))
    world.install_project(_source(world.tmp / "src-p", "app", dependencies=deps))

    with pytest.raises(PackageInstallError, match="base"):
        uninstall_package(world.project_store, "base", fallback=world.global_store)

    assert set(world.select()) == {"app", "base"}


def test_dependency_validation_falls_back_to_the_global_selection(world: _World) -> None:
    world.install_global(_source(world.tmp / "src-d", "base"))
    package = PackageInfo.live_source(
        _source(world.tmp / "src-p", "app", dependencies='\n[dependencies]\nbase = "1.0"\n')
    )

    resolved = validate_dependencies(world.project_store, package, fallback=world.global_store)

    assert [dependency.manifest.name for dependency in resolved] == ["base"]
    with pytest.raises(DependencyError, match="base"):
        validate_dependencies(world.project_store, package)


def test_dependency_validation_ignores_a_global_package_the_project_shadows(
    world: _World,
) -> None:
    world.install_global(_source(world.tmp / "src-g", "base", "2.0.0"))
    world.install_project(_source(world.tmp / "src-o", "base", "1.0.0"))
    package = PackageInfo.live_source(
        _source(world.tmp / "src-p", "app", dependencies='\n[dependencies]\nbase = "2.0"\n')
    )

    with pytest.raises(DependencyError, match="base"):
        validate_dependencies(world.project_store, package, fallback=world.global_store)


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
        validate_dependencies(world.project_store, package, fallback=world.global_store)


def test_pkg_target_selects_the_project_store_only_on_request(world: _World) -> None:
    inside = ConfigContext(home=world.home, proj_dir=world.project, cwd=world.project)
    outside = ConfigContext(home=world.home, proj_dir=None, cwd=world.tmp)
    layers = context_layers(home=world.home, proj_dir=world.project)

    assert pkg_target(inside, project=False).store == layers.global_store
    assert pkg_target(inside, project=False).fallback is None
    selected = pkg_target(inside, project=True)
    assert (selected.store, selected.fallback) == (layers.project, layers.global_store)
    with pytest.raises(PackageInstallError):
        pkg_target(outside, project=True)


def test_resolution_target_layers_the_project_store_over_the_global_one(world: _World) -> None:
    inside = ConfigContext(home=world.home, proj_dir=world.project, cwd=world.project)
    outside = ConfigContext(home=world.home, proj_dir=None, cwd=world.tmp)
    layers = context_layers(home=world.home, proj_dir=world.project)

    target = pkg_resolution_target(inside)
    assert (target.store, target.fallback) == (layers.project, layers.global_store)
    target = pkg_resolution_target(outside)
    assert (target.store, target.fallback) == (layers.global_store, None)


def test_resolution_target_is_the_global_store_when_the_project_store_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGM_HOME", str(tmp_path))
    context = ConfigContext(home=tmp_path, proj_dir=tmp_path, cwd=tmp_path)

    target = pkg_resolution_target(context)

    assert target.fallback is None
    assert target.store == context_layers(home=tmp_path, proj_dir=tmp_path).global_store


def test_operations_on_an_absent_store_create_nothing(world: _World) -> None:
    version = semver.Version.parse("1.0.0")

    assert sync_active_python_dependencies(world.project_store) == ()
    with pytest.raises(PackageInstallError, match="alpha"):
        uninstall_package(world.project_store, "alpha", fallback=world.global_store)
    with pytest.raises(PackageInstallError, match="alpha"):
        activate_installed_package_with_plan(
            world.project_store, "alpha", version, fallback=world.global_store
        )
    assert not world.project_store.root.exists()


@pytest.fixture
def broken_project(world: _World, monkeypatch: pytest.MonkeyPatch) -> ConfigContext:
    """A project whose package depends on a global package that is then uninstalled."""
    monkeypatch.setenv("AGM_HOME", world.env["AGM_HOME"])
    world.install_global(_source(world.tmp / "src-d", "base"))
    deps = '\n[dependencies]\nbase = "1.0"\n'
    world.install_project(_source(world.tmp / "src-p", "app", dependencies=deps))
    uninstall_package(world.global_store, "base")
    return ConfigContext(home=world.home, proj_dir=world.project, cwd=world.project)


def test_a_broken_project_selection_is_warned_about(
    broken_project: ConfigContext, capsys: pytest.CaptureFixture[str]
) -> None:
    warn_if_project_selection_broken(broken_project, project=False)

    assert "base" in capsys.readouterr().err


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
