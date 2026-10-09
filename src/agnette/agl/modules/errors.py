"""Module-system errors for the AgL module loader.

All error classes subclass :class:`~agnette.agl.diagnostics.AglError` and carry a
``SourceSpan`` where the trigger is an import declaration — enabling the
diagnostics machinery to report the originating file and source location.
"""

from __future__ import annotations

from pathlib import Path

from agnette.agl.diagnostics import AglError
from agnette.agl.modules.ids import ModuleId
from agnette.agl.syntax.spans import SourceSpan


class ModuleNotFound(AglError):
    """A module id could not be found in any searched root.

    ``module_id`` is the id that was not found.  ``searched_roots`` lists the
    loose roots searched, followed by the module trees mounted for the id's
    leading segment, each group sorted for deterministic diagnostics.
    ``span`` is the :class:`~agnette.agl.syntax.spans.SourceSpan` of the
    ``import`` declaration that triggered the lookup, when available.
    """

    def __init__(
        self,
        module_id: ModuleId,
        searched_roots: tuple[Path, ...],
        *,
        span: SourceSpan | None = None,
    ) -> None:
        roots_str = ", ".join(str(r) for r in searched_roots)
        msg = f"module '{module_id.display()}' not found; searched roots: [{roots_str}]"
        super().__init__(msg, span=span)
        self.module_id = module_id
        self.searched_roots = searched_roots


class AmbiguousModule(AglError):
    """A module id resolves to ≥2 distinct canonical files.

    ``candidates`` is a sorted tuple of canonical :class:`~pathlib.Path`
    objects, one per distinct file the id resolved to.  ``span`` is the
    originating import declaration span, when available.
    """

    def __init__(
        self,
        module_id: ModuleId,
        candidates: tuple[Path, ...],
        *,
        span: SourceSpan | None = None,
    ) -> None:
        candidates_str = ", ".join(str(c) for c in candidates)
        msg = (
            f"module '{module_id.display()}' is ambiguous; "
            f"found in multiple roots: [{candidates_str}]"
        )
        super().__init__(msg, span=span)
        self.candidates = candidates


class ModulePrefixNotFound(AglError):
    """A wildcard import prefix (``foo/*``) matched no module.

    ``span`` is the originating import declaration span, when available.
    """

    def __init__(
        self,
        prefix: tuple[str, ...],
        *,
        span: SourceSpan | None = None,
    ) -> None:
        slash_prefix = "/".join(prefix)
        msg = f"wildcard prefix '{slash_prefix}/*' matched no module"
        super().__init__(msg, span=span)


class ModuleReadError(AglError):
    """A module file could not be read as UTF-8 text."""

    def __init__(self, module_id: ModuleId, path: Path, reason: str) -> None:
        super().__init__(f"cannot read module '{module_id.display()}' at '{path}': {reason}")


class MissingExternCompanion(AglError):
    """A module declaring at least one ``extern def`` has no companion ``.py`` file.

    The missing companion is the module's own file with its suffix replaced by
    ``.py``.  ``span`` is the span of the first ``extern def`` declaration in
    the module, when available.
    """

    def __init__(
        self,
        module_id: ModuleId,
        companion_path: Path,
        *,
        span: SourceSpan,
    ) -> None:
        msg = (
            f"module '{module_id.display()}' declares an extern function but its "
            f"companion file '{companion_path}' does not exist"
        )
        super().__init__(msg, span=span)


class PackageImportVisibilityError(AglError):
    """A package module imports a package it did not declare.

    ``importer`` and ``target`` identify the mounted packages on either side
    of the rejected edge. ``target`` is the required key in the importer's
    manifest ``[dependencies]`` table.
    """

    def __init__(
        self,
        importer: str,
        target: str,
        *,
        span: SourceSpan,
    ) -> None:
        super().__init__(
            f"package {importer!r} imports package {target!r} without a "
            f"[dependencies] entry for {target!r}",
            span=span,
        )
        self.importer = importer
        self.target = target


class ImportEntryError(AglError):
    """An import declaration resolves to the entry file's canonical identity.

    Importing the entry program is rejected: the entry is a non-importable
    program root.  ``span`` is the originating import declaration span, when
    available.
    """

    def __init__(
        self,
        module_id: ModuleId,
        entry_path: Path,
        *,
        span: SourceSpan,
    ) -> None:
        msg = f"cannot import '{module_id.display()}': it resolves to the entry file '{entry_path}'"
        super().__init__(msg, span=span)
