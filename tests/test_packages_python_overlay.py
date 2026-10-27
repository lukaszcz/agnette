"""Project Python requirement overlay: build, atomic swap, run-time lookup."""

from __future__ import annotations

import fcntl
import os
import site
import sys
from importlib import metadata
from pathlib import Path

import pytest
import semver

import agnette.agm.commands.pkg.info as info_command
import agnette.packages.install as install_module
from agnette.agm.cli_support.args import PkgInfoArgs
from agnette.config.context import ConfigContext, current_config_context
from agnette.core import dry_run
from agnette.core.pyenv import RequirementState
from agnette.packages import python_deps
from agnette.packages.activation import (
    PackageActivationError,
    load_activation_index,
    resolve_indexed_packages,
)
from agnette.packages.errors import PackageInstallError
from agnette.packages.install import (
    activate_installed_package_with_plan,
    install_directory_with_plan,
    sync_active_python_dependencies,
    uninstall_package,
)
from agnette.packages.layout import StoreScope
from agnette.packages.manifest import PackageManifest
from agnette.packages.model import PackageInfo
from agnette.packages.python_deps import (
    overlay_for,
    prepend_overlay_to_path,
    requirement_store,
    unsatisfied_python_dependencies,
)
from agnette.packages.store import StoreLayers, global_store, project_store
from tests._package_helpers import PythonInstaller, write_distribution, write_python_package

_A = "agm-test-absent-a>=1"
_B = "agm-test-absent-b"
_C = "agm-test-absent-c"


class _World:
    """A global store beneath ``installer.home`` and a project store beside it."""

    def __init__(
        self, tmp_path: Path, installer: PythonInstaller, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.tmp = tmp_path
        self.installer = installer
        self.global_store = global_store(home=installer.home, env={})
        self.project_dir = tmp_path / "project"
        self.project_dir.mkdir()
        self.project = project_store(self.project_dir)
        self.layers = StoreLayers(self.global_store, self.project)
        monkeypatch.setenv("AGM_HOME", str(installer.home))
        monkeypatch.setenv("PROJ_DIR", str(self.project_dir))
        monkeypatch.chdir(self.project_dir)

    @property
    def overlay(self) -> Path:
        return self.project.python_overlay_path

    def install(
        self, name: str, *specs: str, version: str = "1.0.0", editable: bool = False
    ) -> PackageInfo:
        source = write_python_package(self.tmp / f"{name}-{version}", name, *specs)
        manifest = source / "package.toml"
        manifest.write_text(manifest.read_text().replace("1.0.0", version), encoding="utf-8")
        return install_directory_with_plan(self.layers, source, editable=editable).package

    def installed_specs(self) -> list[str]:
        return (self.overlay / "installed.txt").read_text(encoding="utf-8").split()

    def generations(self) -> list[Path]:
        """The generation directories beside the overlay link, whether current or not."""
        return sorted(
            entry for entry in self.project.root.iterdir() if entry.name.startswith(".python-")
        )

    def current(self) -> Path:
        return self.overlay.resolve()

    def pin(self, **pins: str) -> None:
        config = self.project_dir / "config" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(f'{name} = "{version}"\n' for name, version in pins.items())
        config.write_text(f"[packages]\n{lines}", encoding="utf-8")


@pytest.fixture
def world(
    tmp_path: Path, python_installer: PythonInstaller, monkeypatch: pytest.MonkeyPatch
) -> _World:
    return _World(tmp_path, python_installer, monkeypatch)


def test_a_project_install_builds_the_overlay_from_the_project_selection(world: _World) -> None:
    world.install("alpha", _A)
    world.install("bravo", _B, _A)

    assert world.installed_specs() == [_A, _B]
    assert world.installer.specs[-1] == [_A, _B]


def test_the_overlay_is_a_link_to_a_generation_built_beside_it(world: _World) -> None:
    world.install("alpha", _A)

    (target,) = world.installer.targets
    assert world.overlay.is_symlink()
    assert target == world.current()
    assert target.parent == world.project.root.resolve()
    assert target.name.startswith(".python-")
    assert world.generations() == [target]


def test_the_overlay_pins_every_distribution_of_agms_interpreter(
    world: _World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    write_distribution(site, "agm-test-interpreter-dist", "0.5")
    monkeypatch.syspath_prepend(site)

    world.install("alpha", _A)

    pins = world.installer.constraints[0]
    assert "agm-test-interpreter-dist==0.5" in pins
    assert f"agnette=={metadata.version('agnette')}" in pins
    assert f"packaging=={metadata.version('packaging')}" in pins


def test_the_overlay_never_pins_its_own_distributions(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.install("alpha", _A)
    write_distribution(world.current(), "agm-test-overlay-only", "1.5")
    monkeypatch.syspath_prepend(world.current())

    world.install("bravo", _B)

    assert not any("agm-test-overlay-only" in pin for pin in world.installer.constraints[1])


def test_global_requirements_stay_out_of_the_overlay(world: _World) -> None:
    source = write_python_package(world.tmp / "gamma", "gamma", _C)
    install_directory_with_plan(StoreLayers(world.global_store), source)
    world.installer.runs.clear()
    world.installer.targets.clear()

    world.install("alpha", _A)

    assert world.installed_specs() == [_A]
    assert world.installer.specs == [[_A]]
    assert not world.global_store.python_overlay_path.exists()


def test_a_global_install_syncs_the_interpreter_and_builds_no_overlay(
    world: _World,
) -> None:
    source = write_python_package(world.tmp / "gamma", "gamma", _C)

    install_directory_with_plan(StoreLayers(world.global_store), source)

    assert world.installer.specs == [[_C]]
    assert world.installer.targets == []
    assert not world.global_store.python_overlay_path.exists()


def test_an_install_rebuilds_the_overlay_to_the_new_union(world: _World) -> None:
    world.install("alpha", _A)
    world.install("bravo", _B)

    assert world.installed_specs() == [_A, _B]
    assert len(world.installer.targets) == 2


def test_switching_versions_rebuilds_the_overlay(world: _World) -> None:
    world.install("alpha", _A, version="1.0.0")
    world.install("alpha", _B, version="2.0.0")
    assert world.installed_specs() == [_B]

    activate_installed_package_with_plan(world.layers, "alpha", semver.Version.parse("1.0.0"))

    assert world.installed_specs() == [_A]


def test_uninstalling_rebuilds_the_overlay_without_the_package(world: _World) -> None:
    world.install("alpha", _A)
    world.install("bravo", _B)

    uninstall_package(world.layers, "alpha")

    assert world.installed_specs() == [_B]


def test_uninstalling_the_last_requirement_removes_the_overlay(world: _World) -> None:
    world.install("alpha", _A)
    world.install("bravo")
    runs = len(world.installer.runs)

    uninstall_package(world.layers, "alpha")

    assert not world.overlay.exists()
    assert len(world.installer.runs) == runs


def test_an_editable_project_package_contributes_to_the_overlay(world: _World) -> None:
    world.install("alpha", _A, editable=True)

    assert world.installed_specs() == [_A]


def test_old_generations_are_kept_until_the_next_rebuild(world: _World) -> None:
    world.install("alpha", _A)
    (first,) = world.generations()

    world.install("bravo", _B)
    assert first in world.generations()
    assert len(world.generations()) == 2

    world.install("charlie", _C)
    assert first not in world.generations()
    assert len(world.generations()) == 2
    assert world.current() in world.generations()


def test_a_failed_build_leaves_the_previous_overlay_and_the_activation(world: _World) -> None:
    world.install("alpha", _A)
    previous = world.current()
    world.installer.returncode = 1

    with pytest.raises(PackageInstallError):
        world.install("bravo", _B)

    assert world.current() == previous
    assert world.installed_specs() == [_A]
    assert set(load_activation_index(world.project).packages) == {"alpha"}
    assert world.generations() == [previous]


def test_an_interrupted_build_leaves_the_previous_overlay(world: _World) -> None:
    world.install("alpha", _A)
    previous = world.current()
    world.installer.interrupt = True

    with pytest.raises(KeyboardInterrupt):
        world.install("bravo", _B)

    assert world.current() == previous
    assert world.installed_specs() == [_A]
    assert world.generations() == [previous]


def _fail_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*_args: object, **_kwargs: object) -> None:
        raise PackageActivationError("publication failed")

    monkeypatch.setattr(install_module, "_publish_activation", failing)


def test_a_failed_publication_discards_the_new_overlay(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.install("alpha", _A)
    world.install("bravo", _B)
    previous = world.current()
    _fail_publication(monkeypatch)

    with pytest.raises(PackageInstallError):
        world.install("charlie", _C)
    with pytest.raises(PackageInstallError):
        uninstall_package(world.layers, "bravo")

    assert world.current() == previous
    assert world.installed_specs() == [_A, _B]
    assert set(load_activation_index(world.project).packages) == {"alpha", "bravo"}
    assert world.generations() == [previous]


def test_a_failed_publication_without_requirements_changes_nothing(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.install("alpha")
    _fail_publication(monkeypatch)

    with pytest.raises(PackageInstallError):
        world.install("bravo")

    assert not world.overlay.is_symlink()
    assert set(load_activation_index(world.project).packages) == {"alpha"}


def test_the_overlay_link_is_replaced_atomically(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.install("alpha", _A)
    present: list[bool] = []
    replace = os.replace

    def watching(source: str | Path, target: str | Path) -> None:
        if Path(target) == world.overlay:
            present.append(world.overlay.exists())
        replace(source, target)

    monkeypatch.setattr(os, "replace", watching)

    world.install("bravo", _B)

    assert present == [True]
    assert world.installed_specs() == [_A, _B]


def test_a_link_that_cannot_be_created_aborts_before_publication(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refusing(*_args: object, **_kwargs: object) -> None:
        raise OSError("no symlinks")

    with monkeypatch.context() as patch:
        patch.setattr(os, "symlink", refusing)
        with pytest.raises(PackageInstallError):
            world.install("alpha", _A)

    assert not world.overlay.is_symlink()
    assert world.generations() == []
    assert not load_activation_index(world.project).packages


def test_a_switch_failing_after_publication_keeps_the_install_and_warns(
    world: _World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("alpha", _A)
    previous = world.current()

    replace = os.replace

    def refusing(source: str | Path, target: str | Path) -> None:
        if Path(target) == world.overlay:
            raise OSError("cannot switch")
        replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", refusing)
        world.install("bravo", _B)

    assert set(load_activation_index(world.project).packages) == {"alpha", "bravo"}
    assert world.current() == previous
    assert "sync --project" in capsys.readouterr().err
    world.install("charlie", _C)
    assert world.installed_specs() == [_A, _B, _C]
    assert len(world.generations()) == 2


def test_a_rebuild_clears_generations_and_links_left_by_a_crash(world: _World) -> None:
    world.install("alpha", _A)
    (world.project.root / ".python-crashed").mkdir()
    (world.project.root / ".python.new").symlink_to(".python-crashed")

    world.install("bravo", _B)

    assert not (world.project.root / ".python.new").is_symlink()
    assert len(world.generations()) == 2
    assert world.project.root / ".python-crashed" not in world.generations()


def test_sync_rebuilds_the_overlay_even_when_it_is_current(world: _World) -> None:
    world.install("alpha", _A)
    (world.overlay / "stray.txt").write_text("x", encoding="utf-8")
    builds = len(world.installer.targets)

    assert sync_active_python_dependencies(world.project) == (_A,)

    assert len(world.installer.targets) == builds + 1
    assert not (world.overlay / "stray.txt").exists()
    assert world.installed_specs() == [_A]


def test_sync_of_a_project_without_requirements_removes_the_overlay_and_leftovers(
    world: _World,
) -> None:
    world.install("alpha")
    (world.project.root / ".python-crashed").mkdir()
    world.overlay.symlink_to(".python-crashed")

    assert sync_active_python_dependencies(world.project) == ()

    assert not world.overlay.is_symlink()
    assert world.generations() == []


def test_dry_run_reports_the_installer_and_changes_nothing(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("alpha", _A)
    world.install("bravo", _B)
    runs, generations, current = len(world.installer.runs), world.generations(), world.current()
    dry_run.set_enabled(True)

    assert sync_active_python_dependencies(world.project) == (_A, _B)
    uninstall_package(world.layers, "bravo")

    assert "dry-run" in capsys.readouterr().out
    assert len(world.installer.runs) == runs
    assert world.installed_specs() == [_A, _B]
    assert world.generations() == generations
    assert world.current() == current


def test_dry_run_of_an_empty_union_changes_nothing(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("alpha")
    (world.project.root / ".python-old").mkdir()
    world.overlay.symlink_to(".python-old")
    generations = world.generations()
    dry_run.set_enabled(True)

    sync_active_python_dependencies(world.project)

    assert capsys.readouterr().out
    assert world.overlay.is_symlink()
    assert world.generations() == generations


def test_pinned_project_packages_add_to_the_selection_in_the_overlay(world: _World) -> None:
    world.install("alpha", _A, version="1.0.0")
    world.install("alpha", _B, version="2.0.0")
    world.install("bravo", _C)
    world.pin(alpha="1.0.0")

    assert sync_active_python_dependencies(world.project) == (_B, _C, _A)

    assert world.installed_specs() == [_B, _C, _A]


def test_a_pin_in_the_invocation_directory_does_not_reach_the_overlay(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.install("alpha", _A, version="1.0.0")
    world.install("alpha", _B, version="2.0.0")
    nested = world.project_dir / "nested"
    (nested / ".agm").mkdir(parents=True)
    (nested / ".agm" / "config.toml").write_text('[packages]\nalpha = "1.0.0"\n', encoding="utf-8")
    monkeypatch.chdir(nested)

    sync_active_python_dependencies(world.project)

    assert world.installed_specs() == [_B]


def test_a_pin_in_the_home_layer_adds_to_the_overlay(world: _World) -> None:
    world.install("alpha", _A, version="1.0.0")
    world.install("alpha", _B, version="2.0.0")
    config = world.installer.home / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[packages]\nalpha = "1.0.0"\n', encoding="utf-8")

    sync_active_python_dependencies(world.project)

    assert world.installed_specs() == [_B, _A]


def test_pins_resolving_to_the_global_store_add_nothing_to_the_overlay(world: _World) -> None:
    source = write_python_package(world.tmp / "gamma", "gamma", _C)
    install_directory_with_plan(StoreLayers(world.global_store), source)
    world.install("alpha", _A)
    world.pin(gamma="1.0.0")

    sync_active_python_dependencies(world.project)

    assert world.installed_specs() == [_A]


def test_packages_resolved_from_the_project_store_are_project_scoped(world: _World) -> None:
    world.install("alpha", _A)

    packages = resolve_indexed_packages(world.project, load_activation_index(world.project))

    assert {package.store_scope for package in packages} == {StoreScope.PROJECT}


def test_info_reports_a_project_package_requirement_status_from_its_overlay(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.install("alpha", _A)
    write_distribution(world.current(), "agm-test-absent-a", "1.5")

    info_command.run(PkgInfoArgs("alpha"))

    assert f"requires python package {_A}: installed 1.5" in capsys.readouterr().out


class TestRunTime:
    def _package(self, scope: StoreScope | None) -> PackageInfo:
        manifest = PackageManifest(name="alpha", version=semver.Version.parse("1.0.0"))
        return PackageInfo(Path("/unused"), manifest, store_scope=scope)

    def test_only_project_store_packages_use_the_projects_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = project_store(tmp_path)
        generation = store.root / ".python-1"
        generation.mkdir(parents=True)
        (generation / python_deps.GENERATION_LOCK).touch()
        store.python_overlay_path.symlink_to(generation.name)
        context = ConfigContext(home=tmp_path, proj_dir=tmp_path, cwd=tmp_path)
        monkeypatch.setattr("agnette.config.context.current_config_context", lambda: context)

        assert overlay_for(self._package(StoreScope.PROJECT)) == generation.resolve()
        assert overlay_for(self._package(StoreScope.GLOBAL)) is None
        assert overlay_for(self._package(None)) is None

    def test_a_project_package_outside_a_project_has_no_overlay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = ConfigContext(home=tmp_path, proj_dir=None, cwd=tmp_path)
        monkeypatch.setattr("agnette.config.context.current_config_context", lambda: context)

        assert overlay_for(self._package(StoreScope.PROJECT)) is None

    def test_the_overlay_is_the_generation_its_link_names(self, world: _World) -> None:
        world.install("alpha", _A)

        package = self._package(StoreScope.PROJECT)

        assert overlay_for(package) == world.current()
        assert overlay_for(package) != world.overlay

    def test_a_checkout_of_an_active_editable_project_package_uses_the_overlay(
        self, world: _World
    ) -> None:
        source = world.install("alpha", _A, editable=True).root
        other = write_python_package(world.tmp / "other", "other", _B)

        assert overlay_for(PackageInfo.live_source(source)) == world.current()
        assert overlay_for(PackageInfo.live_source(other)) is None

    def test_a_checkout_of_an_active_editable_global_package_has_no_overlay(
        self, world: _World
    ) -> None:
        source = write_python_package(world.tmp / "gamma", "gamma", _C)
        install_directory_with_plan(StoreLayers(world.global_store), source, editable=True)
        world.install("alpha", _A)

        assert overlay_for(PackageInfo.live_source(source)) is None

    def test_the_overlay_goes_ahead_of_the_libraries_but_not_the_stdlib(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "path", ["/stdlib", "/lib", "/site", "/other"])
        monkeypatch.setattr(site, "getsitepackages", lambda: ["/site"])
        monkeypatch.setattr(site, "getusersitepackages", lambda: "/user")
        overlay = tmp_path / ".python"
        overlay.mkdir()

        prepend_overlay_to_path(overlay)
        prepend_overlay_to_path(overlay)

        assert sys.path == ["/stdlib", "/lib", str(overlay), "/site", "/other"]

    def test_the_overlay_goes_last_when_no_library_directory_is_on_sys_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "path", ["/stdlib"])
        overlay = tmp_path / ".python"
        overlay.mkdir()

        prepend_overlay_to_path(overlay)

        assert sys.path == ["/stdlib", str(overlay)]

    def test_the_overlay_link_is_resolved_to_its_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "path", [])
        generation = tmp_path / ".python-1"
        generation.mkdir()
        (tmp_path / ".python").symlink_to(generation.name)

        prepend_overlay_to_path(tmp_path / ".python")

        assert sys.path == [str(generation)]

    def test_path_files_of_the_overlay_are_processed_in_its_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "path", ["/stdlib", "/site"])
        monkeypatch.setattr(site, "getsitepackages", lambda: ["/site"])
        overlay = tmp_path / ".python"
        (overlay / "vendored").mkdir(parents=True)
        (overlay / "extra.pth").write_text("vendored\n", encoding="utf-8")

        prepend_overlay_to_path(overlay)

        assert sys.path == ["/stdlib", str(overlay), str(overlay / "vendored"), "/site"]

    def test_a_path_file_inserting_at_the_front_does_not_displace_the_overlay_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "path", ["/stdlib", "/site"])
        monkeypatch.setattr(site, "getsitepackages", lambda: ["/site"])
        overlay = tmp_path / ".python"
        overlay.mkdir()
        (overlay / "front.pth").write_text(
            "import sys; sys.path.insert(0, '/front')\n", encoding="utf-8"
        )

        prepend_overlay_to_path(overlay)

        assert sys.path == ["/stdlib", "/front", str(overlay), "/site"]

    def test_satisfaction_consults_the_overlay_before_the_interpreter(self, tmp_path: Path) -> None:
        write_distribution(tmp_path, "agm-test-absent-a", "1.5")
        write_distribution(tmp_path, "agm-test-absent-c", "0.1")

        unsatisfied = unsatisfied_python_dependencies(
            (_A, _B, "agm-test-absent-c>=1", "semver>=3"), tmp_path
        )

        assert [spec for spec, _ in unsatisfied] == [_B, "agm-test-absent-c>=1"]

    def test_without_an_overlay_only_the_interpreter_is_consulted(self, tmp_path: Path) -> None:
        write_distribution(tmp_path, "agm-test-absent-a", "1.5")

        unsatisfied = unsatisfied_python_dependencies((_A,))

        assert [spec for spec, _ in unsatisfied] == [_A]


def _hold(generation: Path, operation: int) -> int:
    """Take a lock on *generation*, as another process using or deleting it would."""
    fd = os.open(generation / python_deps.GENERATION_LOCK, os.O_RDONLY)
    fcntl.flock(fd, operation | fcntl.LOCK_NB)
    return fd


def _project_package() -> PackageInfo:
    manifest = PackageManifest(name="alpha", version=semver.Version.parse("1.0.0"))
    return PackageInfo(Path("/unused"), manifest, store_scope=StoreScope.PROJECT)


class TestStaleOverlay:
    """An overlay is stale when it holds another version of a distribution the interpreter has."""

    def test_a_different_interpreter_version_makes_it_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay, interpreter = tmp_path / "overlay", tmp_path / "interpreter"
        write_distribution(overlay, "agm-test-x", "1.0")
        write_distribution(interpreter, "agm-test-x", "2.0")
        monkeypatch.syspath_prepend(interpreter)

        assert python_deps.overlay_is_stale(overlay)

    def test_matching_overlay_only_and_interpreter_only_distributions_are_not_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay, interpreter = tmp_path / "overlay", tmp_path / "interpreter"
        write_distribution(overlay, "agm-test-x", "1.0")
        write_distribution(overlay, "agm-test-only-overlay", "3.0")
        write_distribution(interpreter, "agm-test-x", "1.0")
        write_distribution(interpreter, "agm-test-only-interpreter", "4.0")
        monkeypatch.syspath_prepend(interpreter)

        assert not python_deps.overlay_is_stale(overlay)

    def test_the_overlay_on_sys_path_is_not_the_interpreter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        generation = tmp_path / ".python-1"
        write_distribution(generation, "agm-test-x", "1.0")
        monkeypatch.syspath_prepend(generation)

        assert not python_deps.overlay_is_stale(generation)

    def test_the_verdict_is_fixed_for_the_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay, interpreter = tmp_path / "overlay", tmp_path / "interpreter"
        write_distribution(overlay, "agm-test-x", "1.0")
        assert not python_deps.overlay_is_stale(overlay)
        write_distribution(interpreter, "agm-test-x", "2.0")
        monkeypatch.syspath_prepend(interpreter)

        assert not python_deps.overlay_is_stale(overlay)

    def test_every_requirement_of_a_stale_overlay_fails_even_if_the_interpreter_holds_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay, interpreter = tmp_path / "overlay", tmp_path / "interpreter"
        write_distribution(overlay, "agm-test-x", "1.0")
        write_distribution(interpreter, "agm-test-x", "2.0")
        monkeypatch.syspath_prepend(interpreter)

        unsatisfied = unsatisfied_python_dependencies(("agm-test-x>=1", "packaging"), overlay)

        assert [(spec, status.state) for spec, status in unsatisfied] == [
            ("agm-test-x>=1", RequirementState.STALE),
            ("packaging", RequirementState.STALE),
        ]

    def test_info_reports_the_stale_overlay(
        self,
        world: _World,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        world.install("alpha", _A)
        write_distribution(world.current(), "agm-test-x", "1.0")
        interpreter = tmp_path / "interpreter"
        write_distribution(interpreter, "agm-test-x", "2.0")
        monkeypatch.syspath_prepend(interpreter)

        info_command.run(PkgInfoArgs("alpha"))

        assert f"requires python package {_A}: stale" in capsys.readouterr().out


class TestProcessGeneration:
    """A process uses one generation per store, protected from deletion while it runs."""

    def test_lookups_keep_the_generation_first_used_after_the_link_moves(
        self, world: _World
    ) -> None:
        world.install("alpha", _A)
        first = overlay_for(_project_package())
        moved = world.project.root / ".python-moved"
        moved.mkdir()
        (moved / python_deps.GENERATION_LOCK).touch()
        (world.project.root / ".python.new").symlink_to(moved.name)
        os.replace(world.project.root / ".python.new", world.overlay)

        assert first == overlay_for(_project_package()) != moved

    def test_a_generation_already_on_sys_path_is_used_even_when_the_link_moved(
        self, world: _World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.install("alpha", _A)
        monkeypatch.syspath_prepend(world.current())
        used = world.current()
        moved = world.project.root / ".python-moved"
        moved.mkdir()
        (moved / python_deps.GENERATION_LOCK).touch()
        (world.project.root / ".python.new").symlink_to(moved.name)
        os.replace(world.project.root / ".python.new", world.overlay)

        assert overlay_for(_project_package()) == used

    def test_the_generation_in_use_is_protected_by_a_shared_lock(self, world: _World) -> None:
        world.install("alpha", _A)

        used = overlay_for(_project_package())

        assert used == world.current()
        with pytest.raises(BlockingIOError):
            _hold(used, fcntl.LOCK_EX)

    def test_a_generation_being_deleted_is_not_used(self, world: _World) -> None:
        world.install("alpha", _A)
        deleting = _hold(world.current(), fcntl.LOCK_EX)
        try:
            assert overlay_for(_project_package()) is None
        finally:
            os.close(deleting)

    def test_an_absent_overlay_is_not_used(self, world: _World) -> None:
        assert overlay_for(_project_package()) is None

    def test_a_rebuild_keeps_a_generation_a_process_still_holds(self, world: _World) -> None:
        world.install("alpha", _A)
        (first,) = world.generations()
        holder = _hold(first, fcntl.LOCK_SH)
        world.install("bravo", _B)
        world.install("charlie", _C)

        assert first in world.generations()
        os.close(holder)
        world.install("delta", "agm-test-absent-d")

        assert first not in world.generations()

    def test_an_empty_union_keeps_a_generation_a_process_still_holds(self, world: _World) -> None:
        world.install("alpha", _A)
        (first,) = world.generations()
        holder = _hold(first, fcntl.LOCK_SH)

        uninstall_package(world.layers, "alpha")

        assert not world.overlay.exists()
        assert world.generations() == [first]
        os.close(holder)


class TestUnchangedUnion:
    """An activation change that leaves the requirement union as built does not rebuild."""

    def test_uninstalling_a_package_without_requirements_keeps_the_overlay(
        self, world: _World
    ) -> None:
        world.install("alpha", _A)
        world.install("bravo")
        runs, current = len(world.installer.runs), world.current()

        uninstall_package(world.layers, "bravo")
        world.install("charlie")

        assert len(world.installer.runs) == runs
        assert world.current() == current
        assert world.generations() == [current]

    def test_sync_rebuilds_regardless(self, world: _World) -> None:
        world.install("alpha", _A)
        runs = len(world.installer.runs)

        sync_active_python_dependencies(world.project)

        assert len(world.installer.runs) == runs + 1

    def test_a_changed_interpreter_rebuilds(
        self, world: _World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.install("alpha", _A)
        runs = len(world.installer.runs)
        interpreter = tmp_path / "interpreter"
        write_distribution(interpreter, "agm-test-new-library", "1.0")
        monkeypatch.syspath_prepend(interpreter)

        world.install("bravo")

        assert len(world.installer.runs) == runs + 1
        assert "agm-test-new-library==1.0" in world.installer.constraints[-1]

    def test_a_failed_publication_after_the_skip_keeps_the_overlay(
        self, world: _World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.install("alpha", _A)
        current = world.current()
        _fail_publication(monkeypatch)

        with pytest.raises(PackageInstallError):
            world.install("bravo")

        assert world.current() == current
        assert world.generations() == [current]


def test_a_global_package_requirement_store_is_the_global_store_without_an_index(
    world: _World,
) -> None:
    package = PackageInfo(
        Path("/unused"), _project_package().manifest, store_scope=StoreScope.GLOBAL
    )

    assert requirement_store(package) == global_store(home=current_config_context().home)


def test_a_development_checkout_belongs_to_the_store_selecting_it(world: _World) -> None:
    source = world.install("alpha", _A, editable=True).root

    assert requirement_store(PackageInfo.live_source(source)) == world.project
    other = write_python_package(world.tmp / "other", "other", _B)
    assert requirement_store(PackageInfo.live_source(other)) is None
