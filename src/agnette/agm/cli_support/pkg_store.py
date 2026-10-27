"""Store selection shared by the ``agm pkg`` commands."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from agnette.config.context import ConfigContext
from agnette.core import dry_run
from agnette.packages.activation import effective_command_index
from agnette.packages.errors import PackageInstallError
from agnette.packages.store import PackageStore, StoreLayers, context_layers


@dataclass(frozen=True, slots=True)
class PkgTarget:
    """The store a ``pkg`` command operates on and the global store beneath a project one."""

    store: PackageStore
    fallback: PackageStore | None = None


def pkg_target(context: ConfigContext, *, project: bool) -> PkgTarget:
    """Return the target of a ``pkg`` command in *context*: the project store, else the global one.

    Raises :class:`PackageInstallError` when *project* is requested outside a project.
    """

    layers = context_layers(home=context.home, proj_dir=context.proj_dir)
    if not project:
        return PkgTarget(layers.global_store)
    if layers.project is None:
        raise PackageInstallError("--project requires a current project")
    return PkgTarget(layers.project, layers.global_store)


def pkg_layers(context: ConfigContext) -> StoreLayers:
    """Return the stores a read-only ``pkg`` command in *context* shows: project over global."""

    return context_layers(home=context.home, proj_dir=context.proj_dir)


def pkg_resolution_target(context: ConfigContext) -> PkgTarget:
    """Return the target a dependency-resolving command checks against in *context*.

    Inside a project that is the project store over the global one; otherwise,
    including a project whose store is the global store, the global store alone.
    """

    layers = context_layers(home=context.home, proj_dir=context.proj_dir)
    return PkgTarget(
        layers.project or layers.global_store, layers.global_store if layers.project else None
    )


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
