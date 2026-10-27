"""Paths for the versioned installed-package store."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import semver

from agnette.agm.project.layout import packages_project_dir, project_packages_dir
from agnette.config.home import agm_home_dir
from agnette.packages.errors import PackageInstallError
from agnette.packages.layout import ACTIVATION_INDEX_FILENAME, StoreScope, store_root_path
from agnette.packages.manifest import DependencySpec, ManifestError, load_installed_manifest
from agnette.packages.model import (
    PackageInfo,
    VersionSelection,
    canonical_package_identity,
    select_satisfying,
)


class StorePathError(ValueError):
    """Raised when a package identity cannot safely name a store directory."""


class StoreIdentityError(ValueError):
    """Raised when an installed package version disagrees with its store location."""


@dataclass(frozen=True, slots=True)
class PackageStore:
    """One versioned package store: a root directory owning its index, lock, and trees.

    Every store path derives from :attr:`root`, so each store keeps its own
    activation index, operation lock, provenance sidecars, and ``RECORD`` files.
    :attr:`scope` says whether it is AgM's global store or a project's.
    """

    root: Path
    scope: StoreScope = StoreScope.GLOBAL

    @property
    def index_path(self) -> Path:
        """The store's activation index."""

        return self.root / ACTIVATION_INDEX_FILENAME

    @property
    def lock_path(self) -> Path:
        """The store's persistent advisory operation lock."""

        return self.root / ".lock"

    @property
    def python_overlay_path(self) -> Path:
        """The store's Python requirement overlay (see ``python_deps``)."""

        return self.root / ".python"

    @property
    def project_dir(self) -> Path:
        """The project owning a project store."""

        assert self.scope is StoreScope.PROJECT
        return packages_project_dir(self.root)

    def package_path(self, name: str, version: semver.Version) -> Path:
        """Return the logical extracted-tree path for one installed package version."""

        return (
            self.root
            / _store_component(name, "package name")
            / _store_component(str(version), "package version")
        )

    def provenance_path(self, name: str, version: semver.Version) -> Path:
        """Return the package-local sidecar path for activation provenance.

        The sidecar lives in its own namespace below the package name, keeping
        mutable activation history out of the payload covered by ``RECORD`` and
        avoiding collisions with semantic-version directory names.
        """

        package_path = self.package_path(name, version)
        return package_path.parent / ".provenance" / f"{package_path.name}.toml"

    def canonical_provenance_path(self, name: str, version: semver.Version) -> Path:
        """Return the canonical, unlinked provenance sidecar path."""

        return self._canonical_managed_path(self.provenance_path(name, version))

    def canonical_package_path(self, name: str, version: semver.Version) -> Path:
        """Return an installed-tree path only when it is an unlinked store tree.

        The logical package-name directory and version target must not be symbolic
        links, even if resolving them would remain in the store. The store root
        itself may be relocated by a link.
        """

        return self._canonical_managed_path(self.package_path(name, version))

    def is_package_root(self, root: Path, name: str, version: semver.Version) -> bool:
        """Return whether *root* is this store's immutable tree for an identity."""

        return root.resolve() == self.canonical_package_path(name, version)

    def _canonical_managed_path(self, logical_candidate: Path) -> Path:
        """Resolve a logical store path once its managed ancestry is known link-free."""

        candidate = logical_candidate
        while candidate != self.root:
            if candidate.is_symlink():
                raise StorePathError(f"package store path contains a symlink: {candidate}")
            candidate = candidate.parent
        return logical_candidate.resolve()


def global_store(*, home: Path, env: Mapping[str, str] | None = None) -> PackageStore:
    """Return the global package store: the one beneath the AgM home.

    The only place the global store is built from a home; callers never build
    one themselves. A context that may be inside a project resolves
    :func:`context_layers` instead.
    """

    return PackageStore(store_root_path(agm_home_dir(home=home, env=env)))


def project_store(proj_dir: Path) -> PackageStore:
    """Return *proj_dir*'s package store, which may not exist yet."""

    return PackageStore(project_packages_dir(proj_dir), StoreScope.PROJECT)


@dataclass(frozen=True, slots=True)
class StoreLayers:
    """The stores a context selects packages from: the global store and, in a project, its own.

    Project selections replace same-named global ones; ``std`` lives in the
    global store alone. A package operation takes layers with ``stores[0]`` as
    its target store and ``stores[1:]`` as the stores supplying the inherited
    selection: ``StoreLayers(global_store)`` alone targets the global store.
    """

    global_store: PackageStore
    project: PackageStore | None = None

    @property
    def stores(self) -> tuple[PackageStore, ...]:
        """The stores, most specific first."""

        return (self.global_store,) if self.project is None else (self.project, self.global_store)

    def is_package_root(self, root: Path, name: str, version: semver.Version) -> bool:
        """Return whether *root* is an immutable tree of this identity in any layer."""

        return any(store.is_package_root(root, name, version) for store in self.stores)


def context_layers(
    *, home: Path, proj_dir: Path | None, env: Mapping[str, str] | None = None
) -> StoreLayers:
    """Return the store layers a context selects packages from: the single resolver.

    The project store is a layer whenever there is a current project, even
    before its directory exists. A project store that is the global store
    itself (an AgM home that is a project) is not a second layer.
    """

    beneath_home = global_store(home=home, env=env)
    if proj_dir is None:
        return StoreLayers(beneath_home)
    project = project_store(proj_dir)
    if project.root.resolve() == beneath_home.root.resolve():
        return StoreLayers(beneath_home)
    return StoreLayers(beneath_home, project)


def iter_store_package_dirs(store: PackageStore) -> Iterator[Path]:
    """Yield each package-name directory in *store*, in name order.

    An absent store holds no packages. A linked name directory does not
    designate a managed tree, and dot-prefixed entries are the store's own
    bookkeeping, never package names.
    """

    if store.root.is_dir():
        yield from _managed_subdirs(store.root)


def iter_store_version_dirs(name_dir: Path) -> Iterator[Path]:
    """Yield one package's installed version directories, in directory order.

    Dot-prefixed entries are the store's own bookkeeping — staging trees,
    uninstall tombstones, and provenance sidecars — not installed versions.
    """

    yield from _managed_subdirs(name_dir)


def _managed_subdirs(directory: Path) -> Iterator[Path]:
    """Yield *directory*'s unlinked, non-dot-prefixed subdirectories, in name order."""
    for child in sorted(directory.iterdir()):
        if child.is_dir() and not child.is_symlink() and not child.name.startswith("."):
            yield child


def iter_installed_packages(store: PackageStore) -> Iterator[PackageInfo]:
    """Yield every installed package version of *store*, validated against its store identity.

    Iterates store name directories, then each package's version directories,
    loading and checking ``package.toml`` against the store's own naming.
    A manifest-loading failure raises :class:`PackageInstallError` naming the
    offending version directory. A version whose manifest identity disagrees
    with its store location raises :class:`StoreIdentityError`.
    """

    for name_dir in iter_store_package_dirs(store):
        for version_dir in iter_store_version_dirs(name_dir):
            try:
                manifest = load_installed_manifest(version_dir / "package.toml")
            except ManifestError as exc:
                raise PackageInstallError(
                    f"cannot load installed package at {version_dir}: {exc}"
                ) from exc
            if canonical_package_identity(manifest.name, manifest.version) != (
                name_dir.name,
                version_dir.name,
            ):
                raise StoreIdentityError(
                    f"installed package at {version_dir} does not match its store identity"
                )
            yield PackageInfo(version_dir, manifest, installed=True, store_scope=store.scope)


def satisfying_from_store(
    store_packages: Iterable[PackageInfo],
    name: str,
    requirement: DependencySpec,
    active: VersionSelection | None,
    *,
    extra_candidates: Iterable[PackageInfo] = (),
) -> PackageInfo | None:
    """Select the highest satisfying candidate for one dependency from store packages.

    Candidates are filtered from *store_packages* by name and minimum
    version, then chosen against *active* (see
    :func:`agnette.packages.model.select_satisfying`). *extra_candidates* adds
    packages that are not in the store yet, such as a dry-run install's
    planned trees.
    """

    def satisfies(package: PackageInfo) -> bool:
        return package.manifest.name == name and package.manifest.version >= requirement.version

    candidates = [package for package in (*store_packages, *extra_candidates) if satisfies(package)]
    return select_satisfying(candidates, active)


def satisfying_installed_package(
    store_packages: Iterable[PackageInfo],
    name: str,
    requirement: DependencySpec,
    active: VersionSelection | None,
    *,
    extra_candidates: Iterable[PackageInfo] = (),
) -> PackageInfo | None:
    """Select a satisfying stored package or the active editable source."""

    selected = satisfying_from_store(
        store_packages, name, requirement, active, extra_candidates=extra_candidates
    )
    if selected is not None or active is None or active.editable is None:
        return selected
    # Live source, selected on identity alone; its commands are not read here.
    editable = PackageInfo.live_source(active.editable, legacy_aliases=True)
    return (
        editable
        if editable.manifest.name == name and editable.manifest.version >= requirement.version
        else None
    )


def satisfying_inherited(
    inherited: Iterable[PackageInfo],
    name: str,
    requirement: DependencySpec,
    active: VersionSelection | None,
) -> PackageInfo | None:
    """Select a satisfying package of the *inherited* (global) selection beneath a project store.

    *active* is the project store's own selection of *name*: when set it
    replaces the inherited package, which then cannot satisfy anything.
    """

    if active is not None:
        return None
    return satisfying_from_store(inherited, name, requirement, None)


def _store_component(value: str, label: str) -> str:
    """Validate one portable, non-traversing store path component."""

    windows_path = PureWindowsPath(value)
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or windows_path.drive
        or windows_path.root
    ):
        raise StorePathError(f"{label} must be one relative path component")
    return value


def installed_packages(store: PackageStore) -> tuple[PackageInfo, ...]:
    """Return all installed immutable package versions of *store*, sorted by identity."""

    try:
        return tuple(iter_installed_packages(store))
    except StoreIdentityError as exc:
        raise PackageInstallError(str(exc)) from exc
