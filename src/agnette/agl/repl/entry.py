"""Pure result data types for one REPL entry evaluation (`EntryResult`, `EntryKind`).

Shared by the session and its program collaborator.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Protocol

from agnette.agl.diagnostics import Diagnostic, RelatedDiagnostic
from agnette.agl.syntax.spans import UNKNOWN_SOURCE
from agnette.agl.warnings import CHARACTER_COLUMN_WARNINGS

if TYPE_CHECKING:
    from agnette.agl.diagnostics import AglError
    from agnette.agl.ir.program import ValueDescriptors
    from agnette.agl.pipeline import RunError
    from agnette.agl.semantics.type_table import TypeTable
    from agnette.agl.semantics.types import Type
    from agnette.agl.semantics.values import Value


EntryKind = Literal["expression", "binding", "declaration", "statement", "type"]


@dataclass(frozen=True, slots=True)
class EntryResult:
    """Outcome of evaluating one REPL entry (pure data, no styled strings).

    ``kind``
        Classified by the entry's LAST item: a bare ``Expr`` → ``"expression"``
        (``value``/``value_type`` set); ``let``/``var`` → ``"binding"``
        (``name``/``value_type``/``value``); ``record``/``enum``/``exception``/
        ``type``/``def`` and a ``scope … end`` region →
        ``"declaration"``; ``:=``, an ``import``/``export``/``use``/fixity
        declaration, or a side-effecting expr (``print``, etc.) →
        ``"statement"``; a REPL-only bare type expression (``int``, a declared
        type name, ``array[T]``) → ``"type"`` (``value_type`` set, no value, no
        state change).
    ``name``
        The bound/declared name, when meaningful (binding / declaration).  A
        scoped declaration carries its path-qualified spelling (``A::f``), and a
        scope region carries the path it opened.
    ``value``
        The echoed runtime value (expression value or new binding value); ``None``
        for declarations, statements, type entries, and failures.
    ``descriptors``
        The descriptor view for rendering the entry's compiled program (nominal
        display spellings, function labels). Set for every entry that reaches
        evaluation, regardless of ``kind``; ``None`` for type entries and
        failures, which never evaluate.
    ``value_type``
        The static type of the echoed value; ``None`` when not applicable.
    ``type_display``
        Pre-rendered type-focused display text for REPL-only type entries that
        are definitions rather than concrete semantic ``Type`` instances (for
        example a bare generic record or enum name).
    ``type_table``
        The shared ``TypeTable`` that resolves nominal record / enum shapes for
        ``value_type``; supplied whenever a type-focused echo may expand a
        record or enum handle to its field / constructor declarations.
    ``diagnostics``
        Pre-execution error diagnostics (parse/scope/typecheck/matchcompile/
        contract/unset program argument).  Empty on success.
    ``warnings``
        Advisory warnings from the lexer and static passes (for example a TAB
        or unused binding), surfaced on every non-parse/scope path.
    ``error``
        The uncaught AgL exception mapped to a ``RunError`` when the entry raised
        during evaluation; ``None`` otherwise.
    ``failure``
        The static (pre-execution) ``AglError`` that produced ``diagnostics``
        (parse, scope, typecheck, match-compile, lowering), for a
        programmatic caller that wants the structured error and its span
        rather than rendered text; ``None`` on success, for a runtime failure
        (``error``), and for a diagnostic with no single originating
        ``AglError`` (a lowering ``ResourceError``, a param/environment/
        extern diagnostic). The REPL
        renderer keeps using ``diagnostics``.
    ``scope_failed``
        Whether the entry never resolved (a parse, import or scope failure), so a
        host reporting what ``agm check`` does withholds the warnings of source
        beyond it.
    ``ok``
        ``True`` iff there are no error diagnostics AND no runtime error.
    ``installed``
        Names installed before a failed entry stopped. Empty for pre-execution
        failures and successful entries.
    ``quote_strings``
        Whether REPL echo should quote a top-level text value. This is normally
        ``True``. The only exception is a standalone ``ask`` builtin entry,
        whose response is echoed as display text rather than as an AgL string
        literal.
    """

    kind: EntryKind
    name: str | None
    value: "Value | None"
    value_type: "Type | None"
    diagnostics: list[Diagnostic]
    warnings: list[Diagnostic]
    error: "RunError | None"
    ok: bool
    installed: tuple[str, ...] = ()
    quote_strings: bool = True
    type_display: str | None = None
    type_table: "TypeTable | None" = None
    descriptors: "ValueDescriptors | None" = None
    failure: "AglError | None" = None
    scope_failed: bool = False


#: A source label with a 1-based line and column.
type Placement = tuple[str | None, int, int | None]


REPL_LABEL = "<repl>"
"""The source label users see for text typed at the prompt."""


class Place(Protocol):
    """Maps a location to its final one; *raw* when its column counts characters, not columns."""

    def __call__(self, location: Placement, /, *, raw: bool = False) -> Placement: ...


def relocate_diagnostic(diagnostic: Diagnostic, place: Place) -> Diagnostic:
    """Return *diagnostic* with its location and its notes' moved by *place*.

    The columns of a character-column warning (:data:`CHARACTER_COLUMN_WARNINGS`) count characters.
    """
    raw = diagnostic.warning_name in CHARACTER_COLUMN_WARNINGS

    def moved[D: (Diagnostic, RelatedDiagnostic)](located: D) -> D:
        label, line, column = place((located.source_label, located.line, located.column), raw=raw)
        end_line, end_column = located.end_line, located.end_column
        if end_line is not None:
            _, end_line, end_column = place((located.source_label, end_line, end_column), raw=raw)
        return replace(
            located,
            line=line,
            column=column,
            end_line=end_line,
            end_column=end_column,
            source_label=label,
        )

    return replace(moved(diagnostic), related=tuple(moved(note) for note in diagnostic.related))


def relocate(
    result: EntryResult,
    place: Place,
    *,
    unplaced: str = UNKNOWN_SOURCE.label,
) -> EntryResult:
    """Return *result* with every diagnostic, note and runtime error location moved by *place*.

    *place* maps a location to its final one; a location it returns unchanged stays put.
    A runtime error *place* leaves without a file is labelled *unplaced*.
    """
    error = result.error
    if error is not None and error.line is not None:
        label, line, column = place((error.source, error.line, error.col))
        error = replace(error, source=unplaced if label is None else label, line=line, col=column)
    return replace(
        result,
        diagnostics=[relocate_diagnostic(d, place) for d in result.diagnostics],
        warnings=[relocate_diagnostic(w, place) for w in result.warnings],
        error=error,
    )


@dataclass(frozen=True, slots=True)
class FileOrigin:
    """Where an entry's text starts inside a file: its 1-based *line* and *column*.

    *raw_column* is the position in characters, which differs from *column* by the tab
    expansion of the indentation before the entry on its line (default: *column*).
    """

    label: str
    line: int
    column: int
    raw_column: int | None = None

    def place(self, location: Placement, /, *, raw: bool = False) -> Placement:
        """Move an entry-relative location into the file; columns shift on the first line only.

        A location anchored in another source stays put. A *raw* location counts characters.
        """
        label, line, column = location
        if label not in (None, UNKNOWN_SOURCE.label):
            return location
        origin = self.raw_column if raw and self.raw_column is not None else self.column
        shifted = column if column is None or line != 1 else column + origin - 1
        return self.label, line + self.line - 1, shifted
