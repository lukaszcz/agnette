"""Create a portable archive from a checked package directory."""

from __future__ import annotations

import sys
from pathlib import Path

from agnette.agm.cli_support.args import PkgCreateArgs
from agnette.config.context import current_config_context
from agnette.core import dry_run
from agnette.packages.archive import (
    ArchiveError,
    archive_source_manifest,
    validate_archive_source,
    write_archive,
)
from agnette.packages.dependencies import DependencyError, validate_dependencies
from agnette.packages.errors import DisciplineError
from agnette.packages.model import PackageInfo


def run(args: PkgCreateArgs) -> None:
    """Validate a package and write its distribution archive."""

    root = Path.cwd() if args.directory is None else Path(args.directory)
    try:
        # The identity only: validating the archive source validates the tree
        # it is built from, so that is the one validation this command runs.
        package = PackageInfo(root=root, manifest=archive_source_manifest(root))
        context = current_config_context()
        dependencies = validate_dependencies(package, home=context.home)
        validate_archive_source(root, dependency_packages=dependencies)
        destination = (
            package.root.parent / f"{package.manifest.name}-{package.manifest.version}.agmpkg"
            if args.output is None
            else Path(args.output)
        )
        if dry_run.enabled():
            dry_run.print_operation("create-package-archive", str(destination))
        else:
            write_archive(package.root, destination)
    # A user-supplied source path can fail at the operating-system boundary
    # before any package rule applies; report it like every other bad source.
    except (ArchiveError, DependencyError, DisciplineError, OSError) as exc:
        print(f"pkg create: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(destination)
