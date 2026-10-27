"""Display metadata for an active AgM package."""

from __future__ import annotations

import sys
import textwrap

from agnette.agm.cli_support.args import PkgInfoArgs
from agnette.agm.cli_support.pkg_store import (
    pkg_selections,
    replaced_entries,
    replaced_version,
)
from agnette.config.context import current_config_context
from agnette.core.env import help_width
from agnette.packages.activation import (
    ActivePackage,
    PackageActivationError,
    active_package_version,
    resolve_active_package,
)
from agnette.packages.layout import StoreScope
from agnette.packages.manifest import ManifestError
from agnette.packages.model import (
    is_std_package_name,
    std_compatibility_upper_bound,
    unmet_std_requirement,
)
from agnette.packages.python_deps import overlay_for, requirement_status
from agnette.util.text import first_paragraph
from agnette.version import AGM_VERSION


def run(args: PkgInfoArgs) -> None:
    """Print manifest metadata and dependency status for one active package."""

    try:
        layers, selections = pkg_selections(current_config_context())
        if args.name not in selections:
            raise PackageActivationError(f"package {args.name!r} is not installed")
        store, active = selections[args.name]
        package = resolve_active_package(store, args.name, active)
        manifest = package.manifest
        active_versions = {
            name: active_package_version(selections[name][1])
            for name in manifest.dependencies
            if name in selections
        }
        replaced = [
            (owner.scope, entry)
            for owner in layers.stores
            for name, entry in replaced_entries(owner, selections)
            if name == args.name
        ]
    except (ManifestError, PackageActivationError) as exc:
        print(f"pkg info: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"{manifest.name} {manifest.version}")
    if layers.project is not None or replaced:
        print(f"store: {store.scope.value}{_shadow_note(replaced)}")
    if manifest.description is not None:
        print(manifest.description)
    if manifest.license is not None:
        print(f"license: {manifest.license}")
    if manifest.authors:
        print(f"authors: {', '.join(manifest.authors)}")
    if manifest.repository is not None:
        print(f"repository: {manifest.repository}")
    if manifest.keywords:
        print(f"keywords: {', '.join(manifest.keywords)}")
    if manifest.aliases:
        print("aliases:")
        for alias, target in sorted(manifest.aliases.items()):
            print(f"  {alias}: {target}")
    if manifest.commands:
        print("commands:")
        width = help_width()
        for path, command in sorted(manifest.commands.items()):
            print(f"  {path}: {command.program or 'command group'}")
            if command.doc:
                print(
                    textwrap.fill(
                        first_paragraph(command.doc),
                        width=width,
                        initial_indent="      ",
                        subsequent_indent="      ",
                    )
                )
    for name, dependency in sorted(manifest.dependencies.items()):
        requirement = f">= {dependency.version}"
        if is_std_package_name(name):
            requirement += f", < {std_compatibility_upper_bound(dependency)}"
            status = f"running AgM {AGM_VERSION}"
            if unmet_std_requirement(dependency) is not None:
                status += " (unsatisfied)"
        else:
            selected = selections.get(name)
            if selected is None:
                status = "missing"
            elif active_versions[name] < dependency.version:
                status = f"active {active_versions[name]} (unsatisfied)"
            else:
                kind = "editable" if selected[1].editable is not None else "active"
                status = f"{kind} {active_versions[name]}"
        print(f"requires {name} {requirement}: {status}")
    overlay = overlay_for(package)
    for spec in manifest.python_dependencies:
        print(f"requires python package {spec}: {requirement_status(spec, overlay).describe()}")


def _shadow_note(replaced: list[tuple[StoreScope, ActivePackage]]) -> str:
    """The ``store:`` suffix naming the index selections the effective one replaces.

    The replaced entries are displayed only (see :func:`replaced_version`).
    """

    return (
        f" (shadows {', '.join(_describe_replaced(*entry) for entry in replaced)})"
        if replaced
        else ""
    )


def _describe_replaced(scope: StoreScope, entry: ActivePackage) -> str:
    return f"{scope.value} {replaced_version(entry)}"
