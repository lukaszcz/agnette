"""List installed AgM packages and the active editable ones."""

from __future__ import annotations

import sys
from collections.abc import Mapping

from agnette.agm.cli_support.args import PkgListArgs
from agnette.agm.cli_support.pkg_store import (
    pkg_selections,
    replaced_entries,
    replaced_version,
)
from agnette.config.context import current_config_context
from agnette.packages.activation import (
    ActivePackage,
    PackageActivationError,
    active_package_version,
    resolve_selections,
)
from agnette.packages.errors import PackageInstallError
from agnette.packages.manifest import ManifestError
from agnette.packages.model import canonical_package_identity
from agnette.packages.store import PackageStore, installed_packages


def run(args: PkgListArgs) -> None:
    """Print every installed package version and every active editable package.

    The effective selection (the indexes with ``[packages]`` pins applied, as a
    run selects it, development checkouts aside) is listed as active or
    editable. Inside a project both stores are listed, each line ending in its
    store. An index selection that the effective selection replaces (by a pin,
    or by the other store's) stays listed with its index status, marked shadowed.
    A selection with no stored tree, such as a pin to an uninstalled version,
    fails as it does at run time.
    """

    del args
    try:
        layers, chosen = pkg_selections(current_config_context())
        resolve_selections(layers, chosen)
        lines = [
            line
            for store in reversed(layers.stores)
            for line in _store_lines(store, chosen, labelled=layers.project is not None)
        ]
    except (ManifestError, PackageActivationError, PackageInstallError) as exc:
        print(f"pkg list: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    for line in lines:
        print(line)


def _store_lines(
    store: PackageStore,
    chosen: Mapping[str, tuple[PackageStore, ActivePackage]],
    *,
    labelled: bool,
) -> list[str]:
    """The listing lines of one store: its stored versions, then its active editables.

    A store's selections are the *chosen* ones it owns, then its index entries
    that *chosen* replaces (shadowed).
    """

    selections: list[tuple[str, ActivePackage, bool]] = [
        (name, entry, False) for name, (owner, entry) in chosen.items() if owner == store
    ]
    selections.extend((name, entry, True) for name, entry in replaced_entries(store, chosen))
    stored = {
        canonical_package_identity(name, entry.version): shadowed
        for name, entry, shadowed in selections
        if entry.editable is None
    }
    entries = []
    for package in installed_packages(store):
        name, version = package.manifest.name, package.manifest.version
        shadowed = stored.get(canonical_package_identity(name, version))
        entries.append((name, version, "installed" if shadowed is None else "active", shadowed))
    entries.extend(
        (
            name,
            replaced_version(entry) if shadowed else active_package_version(entry),
            "editable",
            shadowed,
        )
        for name, entry, shadowed in sorted(selections, key=_selection_order)
        if entry.editable is not None
    )
    return [
        " ".join(
            (
                f"{name} {version} {kind}",
                *((store.scope.value,) if labelled else ()),
                *(("(shadowed)",) if shadowed else ()),
            )
        )
        for name, version, kind, shadowed in entries
    ]


def _selection_order(selection: tuple[str, ActivePackage, bool]) -> tuple[str, bool]:
    """Order selections by name, the effective one before the entry it replaced."""

    name, _, shadowed = selection
    return name, shadowed
