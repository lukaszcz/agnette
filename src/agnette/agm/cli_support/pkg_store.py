"""Store selection shared by the ``agm pkg`` commands."""

from __future__ import annotations

from agnette.config.context import ConfigContext
from agnette.packages.store import PackageStore, context_store


def pkg_store(context: ConfigContext) -> PackageStore:
    """Return the store a ``pkg`` command operates on in *context*."""

    return context_store(home=context.home)
