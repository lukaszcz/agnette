"""``:info`` documentation helpers: ``@doc`` rendering and module regions."""

from __future__ import annotations

from agnette.agl.scope.attributes import region_paths
from agnette.agl.scope.symbols import ModuleResolution
from agnette.agl.value_syntax.lexical import quote_text


def format_doc_attribute(doc: str, *, inner: bool = False) -> str:
    """Render *doc* as a ``@doc`` (``@!doc`` if *inner*) attribute, triple-quoted if multi-line."""
    name = "@!doc" if inner else "@doc"
    if "\n" not in doc:
        return f"{name}({quote_text(doc)})"
    body = "\n".join(f"  {quote_text(line)[1:-1]}" if line else "" for line in doc.split("\n"))
    return f'{name}("""\n{body}\n""")'


def module_regions(resolved: ModuleResolution) -> dict[tuple[str, ...], str | None]:
    """Map each scope region *resolved* declares, by full path, to its ``@!doc`` text if any."""
    docs = resolved.attributes.region_docs
    return {path: docs.get(path) for _, path in region_paths(resolved.program.body.items, ())}
