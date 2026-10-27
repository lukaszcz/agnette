"""Select an installed package version as the active version of its store."""

from __future__ import annotations

import sys

from agnette.agm.cli_support.args import PkgSwitchArgs
from agnette.agm.cli_support.pkg_store import pkg_target, warn_if_project_selection_broken
from agnette.config.context import current_config_context
from agnette.packages.install import (
    PackageInstallError,
    activate_installed_package_with_plan,
    parse_versioned_package_target,
)


def run(args: PkgSwitchArgs) -> None:
    """Activate the specified installed package version."""

    context = current_config_context()
    try:
        name, version = parse_versioned_package_target(args.target)
        target = pkg_target(context, project=args.project)
        activate_installed_package_with_plan(target.store, name, version, fallback=target.fallback)
    except PackageInstallError as exc:
        print(f"pkg switch: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    warn_if_project_selection_broken(context, project=args.project)
    print(f"activated {name} {version}")
