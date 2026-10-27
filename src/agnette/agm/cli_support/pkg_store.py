"""Store selection shared by the ``agm pkg`` commands."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass

import semver

from agnette.config.context import ConfigContext
from agnette.core import dry_run
from agnette.packages.activation import (
    ActivePackage,
    active_package_version,
    effective_command_index,
    effective_selections,
    load_activation_index,
)
from agnette.packages.errors import PackageInstallError
from agnette.packages.manifest import ManifestError
from agnette.packages.store import PackageStore, StoreLayers, context_layers


@dataclass(frozen=True, slots=True)
class PkgTarget:
    """The store a ``pkg`` command operates on and the global store beneath a project one."""

    store: PackageStore
    fallback: PackageStore | None = None


def pkg_target(context: ConfigContext, *, project: bool) -> PkgTarget:
    """Return the target of a ``pkg`` command in *context*: the project store, else the global one.

    Raises :class:`PackageInstallError` when *project* is requested outside a project, or in
    one whose store is the global store (the AgM home is the project).
    """

    layers = context_layers(home=context.home, proj_dir=context.proj_dir)
    if not project:
        return PkgTarget(layers.global_store)
    if layers.project is None:
        if context.proj_dir is not None:
            raise PackageInstallError("this project's store is the global store; omit --project")
        raise PackageInstallError("--project requires a current project")
    return PkgTarget(*layers.stores)


def pkg_selections(
    context: ConfigContext,
) -> tuple[StoreLayers, dict[str, tuple[PackageStore, ActivePackage]]]:
    """Return the stores a read-only ``pkg`` command in *context* shows, and their selection.

    The stores are the project's over the global one; the selection is
    :func:`~agnette.packages.activation.effective_selections`.
    Raises :class:`PackageActivationError` for unreadable activation state or pins.
    """

    layers = context_layers(home=context.home, proj_dir=context.proj_dir)
    selections = effective_selections(
        layers, home=context.home, proj_dir=context.proj_dir, cwd=context.cwd
    )
    return layers, selections


def replaced_entries(
    store: PackageStore, chosen: Mapping[str, tuple[PackageStore, ActivePackage]]
) -> list[tuple[str, ActivePackage]]:
    """Return *store*'s index entries that the effective selection *chosen* replaces."""

    return [
        (name, entry)
        for name, entry in load_activation_index(store).packages.items()
        if not _is_chosen(chosen[name], store, entry)
    ]


def replaced_version(entry: ActivePackage) -> semver.Version:
    """The version to display for an index *entry* the effective selection replaces.

    Display only: an editable entry whose source can no longer be read shows the
    version its index recorded.
    """

    try:
        return active_package_version(entry)
    except ManifestError:
        return entry.version


def _is_chosen(
    chosen: tuple[PackageStore, ActivePackage], store: PackageStore, entry: ActivePackage
) -> bool:
    """Whether the effective selection of a name is *entry* of *store*."""

    owner, selected = chosen
    return owner == store and (selected.version, selected.editable) == (
        entry.version,
        entry.editable,
    )


def pkg_resolution_target(context: ConfigContext) -> PkgTarget:
    """Return the target a dependency-resolving command checks against in *context*.

    Inside a project that is the project store over the global one; otherwise,
    including a project whose store is the global store, the global store alone.
    """

    return PkgTarget(*context_layers(home=context.home, proj_dir=context.proj_dir).stores)


def warn_if_project_selection_broken(context: ConfigContext, *, project: bool) -> None:
    """Warn when a global operation left the current project's effective selection invalid.

    Global operations never see project stores, so they succeed regardless;
    only the layered view can show that a project package lost a dependency, or
    that a pinned version now conflicts with a command registered since.
    Nothing changed in a dry run, and a ``--project`` operation validates its
    own result.
    """

    if project or context.proj_dir is None or dry_run.enabled():
        return
    try:
        effective_command_index(home=context.home, proj_dir=context.proj_dir, cwd=context.cwd)
    except (OSError, ValueError) as exc:
        print(
            f"warning: the selection of the current project is no longer valid: {exc}",
            file=sys.stderr,
        )
