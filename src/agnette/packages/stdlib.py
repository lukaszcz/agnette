"""Anchor-aware selection of the AgL standard-library package root.

The choice between a development ``std`` checkout containing the anchored
path, the active managed store package, and AgM's shipped tree is
package-domain work — development discovery, activation lookup, canonical
identity, store paths, and manifest loading — so it lives here rather than in
the configuration layer. ``agnette.config.module_roots`` handles only the
``AGM_STDLIB`` environment override and delegates everything else to
:func:`resolve_std_package_root`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import semver

from agnette.packages.activation import PackageActivationError, load_activation_index
from agnette.packages.development import containing_development_package
from agnette.packages.manifest import ManifestError, load_manifest
from agnette.packages.model import (
    STD_PACKAGE_NAME,
    canonical_package_identity,
    is_std_package_name,
    owning_package,
)
from agnette.packages.store import canonical_package_store_path
from agnette.stdlib_locator import shipped_stdlib_root
from agnette.version import AGM_VERSION


class StdlibResolutionError(RuntimeError):
    """Raised when the selected managed standard library cannot be used."""


class StdlibVersionMismatchError(StdlibResolutionError):
    """Raised when the active store ``std`` version differs from AgM's version."""

    def __init__(self, installed_version: str, running_version: str) -> None:
        super().__init__(
            f"Active std package version {installed_version} does not match running AgM version "
            f"{running_version}. Re-run `just install` to install the matching std package."
        )


def resolve_std_package_root(
    *, home: Path, env: Mapping[str, str] | None = None, anchor: Path | None = None
) -> Path:
    """Return the selected AgL standard-library module root.

    A development ``std`` checkout whose module tree contains *anchor* wins
    outright, so working inside the standard library resolves it from the tree
    being edited rather than from a possibly stale installed copy. Otherwise an
    active immutable store ``std`` package wins, but only when its version
    exactly matches the running AgM binary. Without an active store package,
    AgM uses the stdlib bundled in an installed wheel or the repository
    ``packages/stdlib/`` tree in a source checkout.
    """
    if anchor is not None:
        checkout = _anchoring_std_checkout(anchor, home=home, env=env)
        if checkout is not None:
            return checkout
    try:
        active = load_activation_index(home=home, env=env).packages.get(STD_PACKAGE_NAME)
    except PackageActivationError as exc:
        raise StdlibResolutionError(f"cannot resolve active std package: {exc}") from exc
    if active is not None:
        if active.editable is not None:
            raise StdlibResolutionError("the managed std package cannot be editable")
        installed_version = str(active.version)
        if canonical_package_identity(
            STD_PACKAGE_NAME, active.version
        ) != canonical_package_identity(STD_PACKAGE_NAME, semver.Version.parse(AGM_VERSION)):
            raise StdlibVersionMismatchError(installed_version, AGM_VERSION)
        try:
            store_stdlib = canonical_package_store_path(
                STD_PACKAGE_NAME, active.version, home=home, env=env
            )
        except ValueError as exc:
            raise StdlibResolutionError(f"cannot resolve active std package: {exc}") from exc
        if store_stdlib.exists():
            if not store_stdlib.is_dir():
                raise StdlibResolutionError(
                    f"active std package at {store_stdlib} is not a directory"
                )
            try:
                manifest = load_manifest(store_stdlib / "package.toml")
                if canonical_package_identity(
                    manifest.name, manifest.version
                ) != canonical_package_identity(STD_PACKAGE_NAME, active.version):
                    raise StdlibResolutionError(
                        f"active std package at {store_stdlib} does not match its activation"
                    )
            except ManifestError as exc:
                raise StdlibResolutionError(
                    f"cannot read active std package manifest at {store_stdlib}: {exc}"
                ) from exc
            return store_stdlib

    shipped = shipped_stdlib_root()
    if not shipped.is_dir():
        raise StdlibResolutionError(f"shipped standard library is missing at {shipped}")
    return shipped


def _anchoring_std_checkout(
    anchor: Path, *, home: Path, env: Mapping[str, str] | None
) -> Path | None:
    """Return the development ``std`` checkout whose modules contain *anchor*.

    *anchor* is a file being compiled or a working directory. It selects a
    checkout only from inside that checkout's own module tree, so an unrelated
    package that happens to be named ``std`` stays inert for the files beside
    it: a standard-library module resolves the library it belongs to, and
    nothing else does.

    An unusable manifest or store layout leaves the anchor unclassified: the
    fault is reported where packages are discovered, so stdlib selection
    simply falls through to the store and shipped trees.
    """

    try:
        package = containing_development_package(anchor, home=home, env=env)
    except ValueError:
        return None
    if package is None or not is_std_package_name(package.manifest.name):
        return None
    if owning_package(anchor, (package,)) is None:
        return None
    return package.root
