"""Sync AgM's interpreter environment to the active packages' Python requirements."""

from __future__ import annotations

import sys

from agnette.agm.cli_support.args import PkgSyncArgs
from agnette.agm.cli_support.pkg_store import pkg_store
from agnette.config.context import current_config_context
from agnette.core import dry_run
from agnette.packages.install import PackageInstallError, sync_active_python_dependencies


def run(args: PkgSyncArgs) -> None:
    """Install the active packages' Python requirements when any is unsatisfied."""

    del args
    try:
        installed = sync_active_python_dependencies(pkg_store(current_config_context()))
    except PackageInstallError as exc:
        print(f"pkg sync: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not installed:
        print("python requirements satisfied")
    verb = "would install" if dry_run.enabled() else "installed"
    for spec in installed:
        print(f"{verb} python requirement {spec}")
