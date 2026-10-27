"""Remove an active package from AgM's store."""

from __future__ import annotations

import sys

from agnette.agm.cli_support.args import PkgUninstallArgs
from agnette.agm.cli_support.pkg_store import pkg_store
from agnette.config.context import current_config_context
from agnette.packages.install import PackageInstallError, uninstall_package


def run(args: PkgUninstallArgs) -> None:
    """Uninstall the named active package."""

    context = current_config_context()
    try:
        uninstall_package(pkg_store(context), args.name)
    except PackageInstallError as exc:
        print(f"pkg uninstall: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"uninstalled {args.name}")
