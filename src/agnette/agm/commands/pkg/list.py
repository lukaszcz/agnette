"""List active AgM packages."""

from __future__ import annotations

import sys
from collections.abc import Container

from agnette.agm.cli_support.args import PkgListArgs
from agnette.agm.cli_support.pkg_store import pkg_layers
from agnette.config.context import current_config_context
from agnette.packages.activation import (
    PackageActivationError,
    active_package_version,
    load_activation_index,
)
from agnette.packages.errors import PackageInstallError
from agnette.packages.layout import StoreScope
from agnette.packages.manifest import ManifestError
from agnette.packages.model import canonical_package_identity
from agnette.packages.store import PackageStore, installed_packages


def run(args: PkgListArgs) -> None:
    """Print every installed package version and every active editable package.

    Inside a project both stores are listed, each line ending in its store; a
    global package the project's own selection replaces is marked shadowed.
    """

    del args
    layers = pkg_layers(current_config_context())
    shadowing: frozenset[str] = frozenset()
    try:
        if layers.project is not None:
            shadowing = frozenset(load_activation_index(layers.project).packages)
        lines = [
            line
            for store in reversed(layers.stores)
            for line in _store_lines(
                store,
                labelled=layers.project is not None,
                shadowed=shadowing if store.scope is StoreScope.GLOBAL else (),
            )
        ]
    except (ManifestError, PackageActivationError, PackageInstallError) as exc:
        print(f"pkg list: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    for line in lines:
        print(line)


def _store_lines(store: PackageStore, *, labelled: bool, shadowed: Container[str]) -> list[str]:
    """The listing lines of one store: its stored versions, then its active editables."""

    index = load_activation_index(store)
    active_identities = {
        canonical_package_identity(name, active.version)
        for name, active in index.packages.items()
        if active.editable is None
    }
    entries = []
    for package in installed_packages(store):
        name, version = package.manifest.name, package.manifest.version
        identity = canonical_package_identity(name, version)
        entries.append((name, version, "active" if identity in active_identities else "installed"))
    entries.extend(
        (name, active_package_version(active), "editable")
        for name, active in sorted(index.packages.items())
        if active.editable is not None
    )
    return [
        " ".join(
            (
                f"{name} {version} {kind}",
                *((store.scope.value,) if labelled else ()),
                *(("(shadowed)",) if kind != "installed" and name in shadowed else ()),
            )
        )
        for name, version, kind in entries
    ]
