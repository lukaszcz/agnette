"""Architectural dependency contracts for the AgL execution pipeline."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).parents[1] / "src"
AGL_ROOT = SRC_ROOT / "agnette" / "agl"


def _agl_imports(package: str) -> list[tuple[Path, str]]:
    """Return every ``agnette.agl`` dependency the package declares, relatives resolved.

    A bare ``from agnette.agl import x`` (or a relative resolving to the same
    package) names the submodule ``agnette.agl.x``, not the package ``agnette.agl``
    itself, so it is reported qualified with each imported name.
    """
    imports: list[tuple[Path, str]] = []
    for path in sorted((AGL_ROOT / package).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module in _imported_modules(path, node):
                if module.startswith("agnette.agl."):
                    imports.append((path, module))
    return imports


def _imported_modules(path: Path, node: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    """Return the absolute module names one import node in ``path`` names.

    A relative import resolves against the importing module's own package, so
    ``from . import zones`` cannot hide a dependency from a contract that reads
    module names. Imports from namespace and language roots name their child modules.
    """
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if not node.level:
        if node.module is None:
            return ()
        module = node.module
    else:
        module_parts = path.relative_to(SRC_ROOT).with_suffix("").parts
        package_parts = module_parts[:-1]
        keep = len(package_parts) - (node.level - 1)
        imported_parts = () if node.module is None else tuple(node.module.split("."))
        module = ".".join((*package_parts[:keep], *imported_parts))
    if module in {"agnette", "agnette.agl"}:
        return tuple(f"{module}.{alias.name}" for alias in node.names)
    return (module,)


def _is_agnette(module: str) -> bool:
    return module == "agnette" or module.startswith("agnette.")


def _agnette_imports(package: str) -> list[tuple[Path, str]]:
    """Return every internal ``agnette`` dependency, including relative imports."""
    return [
        (path, module)
        for path in sorted((AGL_ROOT / package).rglob("*.py"))
        for module in _agnette_imports_of_file(path)
    ]


def _is_allowed(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)


@pytest.mark.parametrize(
    ("package", "allowed"),
    [
        (
            "ir",
            (
                "agnette.agl.constraints",
                "agnette.agl.ir",
                "agnette.agl.modules.ids",
                "agnette.agl.zones",
            ),
        ),
        (
            "lower",
            (
                "agnette.agl.artifact_cache",
                "agnette.agl.artifact_serialization",
                "agnette.agl.ir",
                "agnette.agl.lower",
                "agnette.agl.matchcompile",
                "agnette.agl.modules.ids",
                "agnette.agl.scope",
                "agnette.agl.self_validation",
                "agnette.agl.semantics",
                "agnette.agl.syntax",
                "agnette.agl.type_schema",
                "agnette.agl.typecheck",
            ),
        ),
        (
            "matchcompile",
            (
                "agnette.agl.diagnostics",
                "agnette.agl.matchcompile",
                "agnette.agl.modules.ids",
                "agnette.agl.scope",
                "agnette.agl.self_validation",
                "agnette.agl.semantics",
                "agnette.agl.syntax",
                "agnette.agl.typecheck",
                "agnette.agl.value_syntax",
            ),
        ),
        (
            "scope",
            (
                "agnette.agl.attributes",
                "agnette.agl.constraints",
                "agnette.agl.diagnostics",
                "agnette.agl.artifact_cache",
                "agnette.agl.infix",
                "agnette.agl.modules",
                "agnette.agl.operator_names",
                "agnette.agl.scope",
                "agnette.agl.semantics",
                "agnette.agl.syntax",
                "agnette.agl.zones",
            ),
        ),
        (
            "typecheck",
            (
                "agnette.agl.attributes",
                "agnette.agl.constraints",
                "agnette.agl.diagnostics",
                "agnette.agl.ir.ids",
                "agnette.agl.ir.reserved_nominals",
                "agnette.agl.artifact_cache",
                "agnette.agl.modules.ids",
                "agnette.agl.scope",
                "agnette.agl.self_validation",
                "agnette.agl.semantics",
                "agnette.agl.syntax",
                "agnette.agl.typecheck",
                "agnette.agl.zones",
            ),
        ),
        (
            "semantics",
            (
                "agnette.agl.constraints",
                "agnette.agl.ir",
                "agnette.agl.modules.ids",
                "agnette.agl.self_validation",
                "agnette.agl.semantics",
                "agnette.agl.zones",
            ),
        ),
        (
            "syntax",
            (
                "agnette.agl.constraints",
                "agnette.agl.modules.ids",
                "agnette.agl.syntax",
                "agnette.agl.value_syntax",
            ),
        ),
        (
            "eval",
            (
                "agnette.agl.eval",
                "agnette.agl.ir",
                "agnette.agl.modules.ids",
                "agnette.agl.runtime",
                "agnette.agl.semantics",
            ),
        ),
        (
            "runtime",
            (
                "agnette.agl.artifact_storage",
                "agnette.agl.attributes",
                "agnette.agl.diagnostics",
                "agnette.agl.ir",
                "agnette.agl.modules.ids",
                "agnette.agl.runtime",
                "agnette.agl.self_validation",
                "agnette.agl.semantics",
                "agnette.agl.syntax.spans",
                "agnette.agl.typecheck.env",
                "agnette.agl.type_schema",
                "agnette.agl.value_syntax",
                "agnette.agl.zones",
            ),
        ),
    ],
)
def test_execution_package_dependency_contract(package: str, allowed: tuple[str, ...]) -> None:
    """Keep each pass on the layers below it.

    ``agnette.agl.artifact_cache`` is admitted into the scope and type-check passes
    because it is a leaf: it reaches only data leaves and the storage envelope,
    never another pass, so consulting it couples a pass to no other layer.
    """
    violations = [
        f"{path.relative_to(AGL_ROOT)} imports {module}"
        for path, module in _agl_imports(package)
        if not _is_allowed(module, allowed)
    ]

    assert violations == []


def _agnette_imports_of_file(path: Path) -> list[str]:
    """Return every internal ``agnette`` module one file imports, relatives resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        module
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for module in _imported_modules(path, node)
        if _is_agnette(module)
    ]


def test_import_collectors_find_language_and_shared_dependencies() -> None:
    """Ensure the relocated roots and namespace filters cannot make contracts vacuous."""
    language_imports = {module for _, module in _agl_imports("value_syntax")}
    internal_imports = {module for _, module in _agnette_imports("value_syntax")}

    assert "agnette.agl.keywords" in language_imports
    assert "agnette.agl.keywords" in internal_imports
    assert "agnette.util.ident" in internal_imports
    assert "agnette.util.ident" in _agnette_imports_of_file(AGL_ROOT / "keywords.py")
    assert "agnette.core.fs" in _agnette_imports_of_file(SRC_ROOT / "agnette/core/dotenv.py")
    assert "agnette.util.ident" in _agnette_imports_of_file(SRC_ROOT / "agnette/util/interp.py")


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("from . import fs", ("agnette.core",)),
        ("from ..util import ident", ("agnette.util",)),
        ("from .. import agl, agm", ("agnette.agl", "agnette.agm")),
        ("from agnette import agl, agm", ("agnette.agl", "agnette.agm")),
        ("from ..agl import zones", ("agnette.agl.zones",)),
        ("from agnette.agl import zones", ("agnette.agl.zones",)),
    ],
)
def test_internal_imports_resolve_relative_and_namespace_imports(
    statement: str, expected: tuple[str, ...]
) -> None:
    node = ast.parse(statement).body[0]
    assert isinstance(node, ast.ImportFrom)
    assert _imported_modules(SRC_ROOT / "agnette/core/dotenv.py", node) == expected


@pytest.mark.parametrize("package", ("core", "util"))
def test_shared_primitives_do_not_import_manager_or_language(package: str) -> None:
    """Keep shared primitives below both application subsystems."""
    paths = sorted((SRC_ROOT / "agnette" / package).rglob("*.py"))
    assert paths
    violations = [
        f"{path.relative_to(SRC_ROOT)} imports {module}"
        for path in paths
        for module in _agnette_imports_of_file(path)
        if _is_allowed(module, ("agnette.agm", "agnette.agl"))
    ]

    assert violations == []


@pytest.mark.parametrize(
    ("leaf", "allowed"),
    [
        ("zones.py", ()),
        ("attributes.py", ("agnette.agl.keywords", "agnette.agl.zones", "agnette.command_catalog")),
        ("artifact_storage.py", ()),
        ("keywords.py", ("agnette.util.ident",)),
        ("constraints.py", ()),
        ("highlight/categories.py", ()),
    ],
)
def test_shared_leaves_sit_below_every_pass(leaf: str, allowed: tuple[str, ...]) -> None:
    """Keep the shared vocabulary modules, plus the storage envelope leaf, below every pass.

    ``zones`` is the bottom leaf and imports nothing under ``agnette``;
    ``attributes`` names the zones its ``@arg-*`` entries select, the
    command-path rule its ``@command`` entry shares with a package manifest,
    and the plain-name predicate its ``@name`` argument rule shares, all pure
    data leaves, and nothing else; ``artifact_storage`` is the disk envelope
    every disk cache writes through and, like ``zones``, imports nothing
    under ``agnette``; ``keywords`` names only the identifier grammar leaf its
    plain-name predicate cross-checks.
    """
    violations = [
        f"{leaf} imports {module}"
        for module in _agnette_imports_of_file(AGL_ROOT / leaf)
        if module not in allowed
    ]

    assert violations == []


def test_ir_all_agnette_dependencies_are_explicit() -> None:
    """Keep the IR on its own data plus the canonical config-key data leaf."""
    allowed = (
        "agnette.agl.constraints",
        "agnette.agl.ir",
        "agnette.agl.modules.ids",
        "agnette.agl.zones",
        "agnette.config.engine_keys",
    )
    violations = [
        f"{path.relative_to(AGL_ROOT)} imports {module}"
        for path, module in _agnette_imports("ir")
        if not _is_allowed(module, allowed)
    ]

    assert violations == []


def test_value_syntax_all_agnette_dependencies_are_explicit() -> None:
    """Keep the value-syntax leaf on shared vocabulary only, below the frontend."""
    allowed = ("agnette.util", "agnette.agl.keywords", "agnette.agl.value_syntax")
    violations = [
        f"{path.relative_to(AGL_ROOT)} imports {module}"
        for path, module in _agnette_imports("value_syntax")
        if not _is_allowed(module, allowed)
    ]

    assert violations == []


def test_lower_consumes_only_matchcompile_public_contract() -> None:
    violations = [
        f"{path.relative_to(AGL_ROOT)} imports {module}"
        for path, module in _agl_imports("lower")
        if module.startswith("agnette.agl.matchcompile.")
    ]

    assert violations == []


@pytest.mark.parametrize("package", ("lower", "ir", "eval", "runtime"))
def test_execution_packages_do_not_import_the_inference_engine(package: str) -> None:
    violations = [
        f"{path.relative_to(AGL_ROOT)} imports {module}"
        for path, module in _agl_imports(package)
        if module == "agnette.agl.typecheck.inference"
        or module.startswith("agnette.agl.typecheck.inference.")
    ]

    assert violations == []


def test_highlighting_dependency_fences() -> None:
    """Keep highlighting above the frontend and optional adapters above its pure tiers.

    Only the REPL and editor navigation, which reuses semantic name occurrences, sit above it.
    """
    highlight = AGL_ROOT / "highlight"
    assert highlight.is_dir()
    violations = [
        f"{path.relative_to(AGL_ROOT)} imports {module}"
        for path in AGL_ROOT.rglob("*.py")
        if path.relative_to(AGL_ROOT).parts[0] not in {"highlight", "repl", "navigation.py"}
        for module in _agnette_imports_of_file(path)
        if _is_allowed(module, ("agnette.agl.highlight",))
    ]
    for path in AGL_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module in _imported_modules(path, node):
                forbidden = path.is_relative_to(highlight) and _is_allowed(
                    module, ("agnette.agl.repl", "prompt_toolkit")
                )
                adapter = path in {highlight / "pygments_lexer.py", highlight / "markdown_ext.py"}
                if forbidden or (
                    not adapter and _is_allowed(module, ("pygments", "markdown", "pymdownx"))
                ):
                    violations.append(f"{path.relative_to(AGL_ROOT)} imports {module}")
    assert violations == []


def test_highlighting_context_stays_below_the_pipeline() -> None:
    allowed = (
        "agnette.agl.highlight.categories",
        "agnette.agl.highlight.lexical",
    )
    violations = [
        module
        for module in _agnette_imports_of_file(AGL_ROOT / "highlight/context.py")
        if not _is_allowed(module, allowed)
    ]
    assert violations == []


def test_lexical_highlighting_depends_only_on_recognition_and_vocabulary() -> None:
    allowed = (
        "agnette.agl.highlight.categories",
        "agnette.agl.keywords",
        "agnette.agl.lexer",
        "agnette.agl.scope.symbols",
        "agnette.agl.syntax",
        "agnette.agl.value_syntax",
        "agnette.util.text",
    )
    violations = [
        module
        for module in _agnette_imports_of_file(AGL_ROOT / "highlight/lexical.py")
        if not _is_allowed(module, allowed)
    ]
    assert violations == []


@pytest.mark.parametrize("module", ("grammar", "grammar_eval"))
def test_portable_grammar_depends_only_on_recognition_and_vocabulary(module: str) -> None:
    allowed = (
        "agnette.agl.highlight.categories",
        "agnette.agl.highlight.grammar",
        "agnette.agl.keywords",
        "agnette.agl.lexer",
        "agnette.agl.scope.symbols",
        "agnette.util.ident",
    )
    violations = [
        dependency
        for dependency in _agnette_imports_of_file(AGL_ROOT / f"highlight/{module}.py")
        if not _is_allowed(dependency, allowed)
    ]
    assert violations == []


#: Only prelude injection may key on the prelude module's identity: the host
#: recognizes a standard built-in declaration by the ``builtin`` keyword in any
#: standard-library module, never by that module being ``std/prelude``.
#: ``modules/ids.py`` defines the id and ``modules/loader.py`` injects the
#: synthetic import (and decides which module supersedes or forgoes it).
_PRELUDE_ID_ALLOWLIST: frozenset[str] = frozenset(
    {
        "modules/ids.py",
        "modules/loader.py",
    }
)


def test_prelude_module_identity_is_used_only_for_prelude_injection() -> None:
    """Nothing outside prelude injection may ask which module is the prelude."""
    violations = sorted(
        str(path.relative_to(AGL_ROOT))
        for path in AGL_ROOT.rglob("*.py")
        if "STD_PRELUDE_ID" in path.read_text(encoding="utf-8")
        and str(path.relative_to(AGL_ROOT)) not in _PRELUDE_ID_ALLOWLIST
    )

    assert violations == []
