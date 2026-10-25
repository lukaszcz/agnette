"""``:info`` documentation helpers: ``@doc`` rendering, module routes, and module regions."""

from __future__ import annotations

from collections.abc import Collection, Iterable

from agnette.agl.modules.ids import ModuleId
from agnette.agl.scope.attributes import region_paths
from agnette.agl.scope.symbols import ModuleResolution
from agnette.agl.syntax.nodes import ImportDecl
from agnette.agl.value_syntax.lexical import quote_text


def module_order(module: ModuleId) -> tuple[str, ...]:
    """Sort key putting modules in route order."""
    return module.segments


def format_doc_attribute(doc: str, *, inner: bool = False) -> str:
    """Render *doc* as a ``@doc`` (``@!doc`` if *inner*) attribute, triple-quoted if multi-line."""
    name = "@!doc" if inner else "@doc"
    if "\n" not in doc:
        return f"{name}({quote_text(doc)})"
    body = "\n".join(f"  {quote_text(line)[1:-1]}" if line else "" for line in doc.split("\n"))
    return f'{name}("""\n{body}\n""")'


def module_spellings(
    imports: Iterable[ImportDecl], known: Collection[ModuleId]
) -> dict[tuple[str, ...], tuple[ModuleId, ...]]:
    """Map each route a session import makes a qualifier of to the loaded modules it names.

    An aliased import is spelled by its alias alone; any other, by every trailing
    segment sequence of its route (``pkg/lib`` as ``lib`` or ``pkg/lib``). A
    wildcard names each loaded module beneath its prefix.
    """
    spelled: dict[tuple[str, ...], set[ModuleId]] = {}
    for decl in imports:
        path = tuple(decl.module_path)
        modules = (
            [m for m in known if m.segments[: len(path)] == path and len(m.segments) > len(path)]
            if decl.wildcard
            else [m for m in known if m.segments == path]
        )
        for module in modules:
            routes = (
                [(decl.alias,)]
                if decl.alias is not None and not decl.wildcard
                else [module.segments[start:] for start in range(len(module.segments))]
            )
            for route in routes:
                spelled.setdefault(route, set()).add(module)
    return {route: tuple(sorted(found, key=module_order)) for route, found in spelled.items()}


def module_regions(resolved: ModuleResolution) -> dict[tuple[str, ...], str | None]:
    """Map each scope region *resolved* declares, by full path, to its ``@!doc`` text if any."""
    docs = resolved.attributes.region_docs
    return {path: docs.get(path) for _, path in region_paths(resolved.program.body.items, ())}
