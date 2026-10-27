"""``@inherit`` validation against dependencies whose commands are source-registered."""

from __future__ import annotations

from pathlib import Path

import pytest

import agnette.agm.commands.pkg.check as check_command
from agnette.agm.cli_support.args import PkgCheckArgs
from agnette.config.context import ConfigContext
from agnette.packages.archive import write_archive
from agnette.packages.layout import MODULE_TREE_DIRNAME
from tests._package_helpers import install_archive, install_directory


def _write(root: Path, manifest: str, modules: dict[str, str]) -> Path:
    (root / MODULE_TREE_DIRNAME).mkdir(parents=True)
    (root / "package.toml").write_text(manifest, encoding="utf-8")
    for module, source in modules.items():
        (root / MODULE_TREE_DIRNAME / f"{module}.agl").write_text(source, encoding="utf-8")
    return root


def _tools(tmp_path: Path) -> Path:
    """A package registering ``tools review`` only through ``@command`` in source."""
    return _write(
        tmp_path / "tools-pkg",
        '[package]\nname = "tools-pkg"\nversion = "1.0.0"\n',
        {"target": '@command("tools review")\nprogram def review(limit: int = 1) -> unit = ()\n'},
    )


def _consumer(tmp_path: Path, leaf: str = "limit", *, path: bool = True) -> Path:
    source = '{ version = "1.0.0", path = "../tools-pkg" }' if path else '"1.0.0"'
    return _write(
        tmp_path / "consumer",
        '[package]\nname = "consumer"\nversion = "1.0.0"\n\n'
        f"[dependencies]\ntools-pkg = {source}\n\n"
        '[commands]\nstart = { program = "consumer/main::start" }\n',
        {"main": f'program def start(@inherit("tools.review.{leaf}") v: int = 1) -> unit = ()\n'},
    )


def _check(consumer: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_command,
        "current_config_context",
        lambda: ConfigContext(home=home, proj_dir=None, cwd=consumer),
    )
    check_command.run(PkgCheckArgs(directory=str(consumer)))


def test_pkg_check_sees_a_not_installed_path_dependencys_registered_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tools(tmp_path)

    _check(_consumer(tmp_path), tmp_path / "home", monkeypatch)


def test_pkg_check_rejects_a_bad_leaf_of_a_path_dependencys_registered_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tools(tmp_path)

    with pytest.raises(SystemExit):
        _check(_consumer(tmp_path, "bogus"), tmp_path / "home", monkeypatch)


def test_editable_dependencys_registered_command_is_visible_to_check_and_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    install_directory(_tools(tmp_path), home=home, env={}, editable=True)
    consumer = _consumer(tmp_path, path=False)

    _check(consumer, home, monkeypatch)
    install_directory(consumer, home=home, env={})


def test_archive_install_sees_an_editable_dependencys_registered_command(tmp_path: Path) -> None:
    home = tmp_path / "home"
    install_directory(_tools(tmp_path), home=home, env={}, editable=True)
    archive = tmp_path / "consumer.agmpkg"
    write_archive(_consumer(tmp_path, path=False), archive)

    install_archive(archive, home=home, env={})
