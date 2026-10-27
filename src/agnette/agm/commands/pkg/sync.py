"""Sync the Python environment of the active packages: AgM's, or the project overlay."""

from __future__ import annotations

import sys

from agnette.agm.cli_support.args import PkgSyncArgs
from agnette.agm.cli_support.pkg_store import pkg_target
from agnette.config.context import current_config_context
from agnette.core import dry_run
from agnette.packages.install import PackageInstallError, sync_active_python_dependencies


def run(args: PkgSyncArgs) -> None:
    """Install the unsatisfied requirements globally, or rebuild the project overlay."""

    try:
        layers = pkg_target(current_config_context(), project=args.project)
        installed = sync_active_python_dependencies(layers.stores[0])
    except PackageInstallError as exc:
        print(f"pkg sync: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not installed:
        print("python requirements satisfied")
    verb = "would install" if dry_run.enabled() else "installed"
    for spec in installed:
        print(f"{verb} python requirement {spec}")
