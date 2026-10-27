"""A package's unsatisfied Python requirements fail its companions' loading with a diagnostic.

The fixture package declares a requirement no environment satisfies; tests make
it satisfied by standing in for the installed-distribution lookup.
"""

from __future__ import annotations

import shutil
import sys
from importlib import metadata
from pathlib import Path

import pytest

from agnette.agl.modules.roots import RootSet, assemble_roots
from agnette.agl.pipeline import PipelineDriver, RunResult
from agnette.agl.repl.entry import EntryResult
from agnette.agl.repl.session import ReplSession
from agnette.agl.semantics.values import IntValue
from agnette.config.context import ConfigContext, current_config_context
from agnette.packages import python_deps
from agnette.packages.activation import ActivationIndex, ActivePackage, write_activation_index
from agnette.packages.layout import StoreScope
from agnette.packages.manifest import load_manifest
from agnette.packages.model import PackageInfo
from agnette.packages.store import global_store, project_store
from tests._agl_helpers import REPO_STDLIB_ROOT, run_inline_code
from tests._package_helpers import write_distribution, write_python_package
from tests.agl.ir_harness import write_companion_file, write_module_file

_PACKAGE = Path(__file__).parent / "agl" / "packages" / "python_requirement"
_REQUIREMENT = "agm-test-absent-distribution>=1.0"
_SYNC = "agm pkg sync"
_SYNC_PROJECT = "agm pkg sync --project"
_INSTALL = "agm pkg install"


def _package(root: Path = _PACKAGE, scope: StoreScope | None = None) -> PackageInfo:
    return PackageInfo(root, load_manifest(root / "package.toml"), store_scope=scope)


def _roots(
    *,
    loose: Path | None = None,
    package: Path = _PACKAGE,
    scope: StoreScope | None = None,
    mounted: tuple[PackageInfo, ...] = (),
) -> RootSet:
    return assemble_roots(
        invocation_root=None,
        stdlib_root=REPO_STDLIB_ROOT,
        lib_root=None,
        configured=[],
        cli=() if loose is None else (str(loose),),
        cwd=_PACKAGE,
        package_roots=(_package(package, scope), *mounted),
    )


def _run(
    source: str,
    *,
    loose: Path | None = None,
    package: Path = _PACKAGE,
    scope: StoreScope | None = None,
    mounted: tuple[PackageInfo, ...] = (),
) -> RunResult:
    return run_inline_code(
        PipelineDriver(resolve_agent_spec=None, get_sandbox_context=None),
        source,
        roots=_roots(loose=loose, package=package, scope=scope, mounted=mounted),
        default_stdlib=False,
    )


def _install_requirement(patch: pytest.MonkeyPatch) -> None:
    """Report the fixture's required distribution as installed at a matching version."""
    real_version = metadata.version

    def version(name: str) -> str:
        return "1.0" if name == "agm-test-absent-distribution" else real_version(name)

    patch.setattr(metadata, "version", version)


@pytest.fixture()
def requirement_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_requirement(monkeypatch)


@pytest.fixture()
def package_active() -> None:
    """Activate the fixture package, editable at its own root, in the test's AgM home."""
    active = ActivePackage(_package().manifest.version, editable=_PACKAGE)
    write_activation_index(
        global_store(home=Path.home()), ActivationIndex({"python_requirement": active})
    )


def _assert_requirement_diagnostic(result: RunResult | EntryResult, *, repair: str = _SYNC) -> None:
    assert result.ok is False
    assert len(result.diagnostics) == 1
    message = result.diagnostics[0].message
    assert "python_requirement" in message
    assert _REQUIREMENT in message
    assert repair in message
    assert (_SYNC if repair == _INSTALL else _INSTALL) not in message


@pytest.mark.usefixtures("package_active")
def test_unsatisfied_requirement_is_a_diagnostic_naming_package_spec_and_repair() -> None:
    result = _run("import python_requirement/double\npython_requirement/double::double(2)")

    _assert_requirement_diagnostic(result)


def test_unsatisfied_requirement_of_an_inactive_mounted_package_suggests_installing_it() -> None:
    result = _run("import python_requirement/double\npython_requirement/double::double(2)")

    _assert_requirement_diagnostic(result, repair=_INSTALL)


def test_package_active_at_another_root_suggests_installing_the_mounted_one(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "python_requirement"
    elsewhere.mkdir()
    (elsewhere / "package.toml").write_bytes((_PACKAGE / "package.toml").read_bytes())
    active = ActivePackage(_package().manifest.version, editable=elsewhere)
    write_activation_index(
        global_store(home=Path.home()), ActivationIndex({"python_requirement": active})
    )

    result = _run("import python_requirement/double\npython_requirement/double::double(2)")

    _assert_requirement_diagnostic(result, repair=_INSTALL)


def test_unsatisfied_requirement_of_the_active_store_package_suggests_syncing() -> None:
    store_root = Path.home() / ".agm" / "packages" / "python_requirement" / "1.0.0"
    shutil.copytree(_PACKAGE, store_root)
    active = ActivePackage(_package().manifest.version)
    write_activation_index(
        global_store(home=Path.home()), ActivationIndex({"python_requirement": active})
    )

    result = _run(
        "import python_requirement/double\npython_requirement/double::double(2)",
        package=store_root,
    )

    _assert_requirement_diagnostic(result)


def test_unsatisfied_requirement_of_the_active_project_package_suggests_a_project_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    context = ConfigContext(home=current_config_context().home, proj_dir=project, cwd=tmp_path)
    monkeypatch.setattr("agnette.config.context.current_config_context", lambda: context)
    store = project_store(project)
    tree = store.package_path("python_requirement", _package().manifest.version)
    shutil.copytree(_PACKAGE, tree)
    active = ActivePackage(_package().manifest.version)
    write_activation_index(store, ActivationIndex({"python_requirement": active}))

    result = _run(
        "import python_requirement/double\npython_requirement/double::double(2)", package=tree
    )

    _assert_requirement_diagnostic(result, repair=_SYNC_PROJECT)


@pytest.fixture()
def project_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fixture package in the current project's store; return its overlay path."""
    project = tmp_path / "project"
    project.mkdir()
    context = ConfigContext(home=current_config_context().home, proj_dir=project, cwd=tmp_path)
    monkeypatch.setattr("agnette.config.context.current_config_context", lambda: context)
    monkeypatch.setattr(sys, "path", list(sys.path))
    store = project_store(project)
    shutil.copytree(_PACKAGE, store.package_path("python_requirement", _package().manifest.version))
    generation = store.root / ".python-test"
    generation.mkdir()
    (generation / python_deps.GENERATION_LOCK).touch()
    store.python_overlay_path.symlink_to(generation.name)
    write_activation_index(
        store, ActivationIndex({"python_requirement": ActivePackage(_package().manifest.version)})
    )
    return store.python_overlay_path


def _project_tree(overlay: Path) -> Path:
    return overlay.parent / "python_requirement" / str(_package().manifest.version)


def _run_project_package(overlay: Path) -> RunResult:
    tree = _project_tree(overlay)
    return _run(
        "import python_requirement/double\nlet r = python_requirement/double::double(21)",
        package=tree,
        scope=StoreScope.PROJECT,
    )


def test_a_requirement_installed_in_the_project_overlay_satisfies_a_project_package(
    project_package: Path,
) -> None:
    write_distribution(project_package, "agm-test-absent-distribution", "1.5")

    result = _run_project_package(project_package)

    assert result.ok, result.diagnostics
    assert result.bindings["r"] == IntValue(42)


def test_the_project_overlay_is_prepended_to_sys_path_once_when_wiring_externs(
    project_package: Path,
) -> None:
    write_distribution(project_package, "agm-test-absent-distribution", "1.5")

    _run_project_package(project_package)
    _run_project_package(project_package)

    assert sys.path.count(str(project_package.resolve())) == 1
    assert sys.path.index(str(project_package.resolve())) < sys.path.index(
        next(entry for entry in sys.path if "site-packages" in entry)
    )


def test_a_requirement_missing_from_the_project_overlay_suggests_a_project_sync(
    project_package: Path,
) -> None:
    result = _run_project_package(project_package)

    _assert_requirement_diagnostic(result, repair=_SYNC_PROJECT)


def test_a_stale_project_overlay_fails_the_check_and_stays_off_sys_path(
    project_package: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = tmp_path / "interpreter"
    write_distribution(interpreter, "agm-test-absent-distribution", "2.0")
    monkeypatch.syspath_prepend(interpreter)
    write_distribution(project_package, "agm-test-absent-distribution", "1.0")
    before = list(sys.path)

    result = _run_project_package(project_package)

    _assert_requirement_diagnostic(result, repair=_SYNC_PROJECT)
    assert sys.path == before


def test_a_global_package_never_uses_the_project_overlay(project_package: Path) -> None:
    write_distribution(project_package, "agm-test-absent-distribution", "1.5")
    before = list(sys.path)

    result = _run("import python_requirement/double\npython_requirement/double::double(2)")

    _assert_requirement_diagnostic(result, repair=_INSTALL)
    assert sys.path == before


def test_a_run_wiring_only_global_companions_leaves_sys_path_and_requirements_alone(
    project_package: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = tmp_path / "interpreter"
    write_distribution(interpreter, "agm-test-absent-distribution", "1.0")
    monkeypatch.syspath_prepend(interpreter)
    write_distribution(project_package, "agm-test-absent-distribution", "0.1")
    before = list(sys.path)
    mounted = _package(write_python_package(tmp_path / "other", "other"), StoreScope.PROJECT)

    result = _run(
        "import python_requirement/double\nlet r = python_requirement/double::double(2)",
        mounted=(mounted,),
    )

    assert result.ok, result.diagnostics
    assert result.bindings["r"] == IntValue(4)
    assert sys.path == before


@pytest.mark.usefixtures("package_active")
def test_requirements_are_reported_once_per_package_across_companion_modules() -> None:
    result = _run(
        "import python_requirement/double\nimport python_requirement/negate\n"
        "python_requirement/double::double(python_requirement/negate::negate(2))"
    )

    _assert_requirement_diagnostic(result)


@pytest.mark.usefixtures("requirement_installed")
def test_satisfied_requirement_loads_companions_normally() -> None:
    result = _run("import python_requirement/double\nlet r = python_requirement/double::double(21)")

    assert result.ok, result.diagnostics
    assert result.bindings["r"] == IntValue(42)


def test_package_without_reached_companions_is_not_checked() -> None:
    result = _run("import python_requirement/plain\nlet r = python_requirement/plain::plain(7)")

    assert result.ok, result.diagnostics
    assert result.bindings["r"] == IntValue(7)


def test_loose_module_companion_is_unaffected(tmp_path: Path) -> None:
    write_module_file(tmp_path, "loose", "extern def triple(x: int) -> int")
    write_companion_file(tmp_path, "loose", "def triple(x):\n    return 3 * x\n")

    result = _run("import loose\nlet r = loose::triple(3)", loose=tmp_path)

    assert result.ok, result.diagnostics
    assert result.bindings["r"] == IntValue(9)


@pytest.mark.usefixtures("package_active")
def test_fresh_driver_reusing_cached_artifacts_rechecks_the_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "import python_requirement/double\npython_requirement/double::double(1)"
    with monkeypatch.context() as patched:
        _install_requirement(patched)
        assert _run(source).ok

    _assert_requirement_diagnostic(_run(source))


def _repl() -> ReplSession:
    return ReplSession(package_roots=(_package(),), cwd=_PACKAGE, default_stdlib=False)


@pytest.mark.usefixtures("package_active")
def test_repl_entry_reports_an_unsatisfied_requirement() -> None:
    result = _repl().eval_entry("import python_requirement/double")

    _assert_requirement_diagnostic(result)


def test_repl_companion_loaded_while_satisfied_survives_the_requirement_lapsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _repl()
    with monkeypatch.context() as patched:
        _install_requirement(patched)
        assert session.eval_entry("import python_requirement/double").ok

    result = session.eval_entry("let r = python_requirement/double::double(4)")
    assert result.ok, result.diagnostics
    assert session.eval_entry("import python_requirement/negate").ok is False
