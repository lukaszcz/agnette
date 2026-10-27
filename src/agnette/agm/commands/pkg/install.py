"""Install a directory package into AgM's versioned store."""

from __future__ import annotations

import sys
from pathlib import Path

from agnette.agm.cli_support.args import PkgInstallArgs
from agnette.agm.cli_support.pkg_store import pkg_target, warn_if_project_selection_broken
from agnette.config.context import current_config_context
from agnette.packages.install import (
    PackageInstallError,
    install_archive_with_plan,
    install_directory_with_plan,
)


def run(args: PkgInstallArgs) -> None:
    """Install the selected package directory or archive."""

    context = current_config_context()
    try:
        if args.editable and args.reinstall:
            raise PackageInstallError("--reinstall cannot be used with --editable")
        source = Path(args.source)
        target = pkg_target(context, project=args.project)
        if source.is_file() and not args.editable:
            plan = install_archive_with_plan(
                target.store,
                source,
                shadow=args.shadow,
                reinstall=args.reinstall,
                fallback=target.fallback,
            )
        else:
            plan = install_directory_with_plan(
                target.store,
                source,
                editable=args.editable,
                shadow=args.shadow,
                reinstall=args.reinstall,
                fallback=target.fallback,
            )
    # A user-supplied source path can fail at the operating-system boundary
    # before any package rule applies; report it like every other bad source.
    except (OSError, PackageInstallError) as exc:
        print(f"pkg install: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    warn_if_project_selection_broken(context, project=args.project)
    package = plan.package
    kind = "editable" if args.editable else "installed"
    print(f"{kind} {package.manifest.name} {package.manifest.version}")
    if args.shadow:
        if plan.command_shadows:
            for shadow in plan.command_shadows:
                owners = ", ".join(shadow.displaced_packages)
                print(f"shadowed command {shadow.path_name} from {owners}")
        else:
            print("shadow requested; no active package commands were displaced")
