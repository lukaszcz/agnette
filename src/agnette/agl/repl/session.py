"""UI-free incremental session core for the AgL REPL (``ReplSession``).

``ReplSession`` keeps a **persistent incremental environment**: each entry is
parsed → resolved → typechecked → match-compiled → lowered → evaluated
**exactly once** against accumulated session state (symbols, types,
declarations, runtime values). Agent calls fire exactly once and are never
replayed, because each entry executes ONLY its own statements — references to
earlier bindings read stored runtime ``Value``s.

The driver reproduces ``PipelineDriver.run``'s IR pipeline incrementally. A
persistent link image and base frame retain IDs, metadata, closures, values, and
cells across entries. Runtime failure is non-transactional: every initializer
completed before the failure remains visible, while unreached initializers do not.

This module is intentionally UI-free — it returns plain ``EntryResult`` data;
rendering, meta-commands, and the prompt_toolkit console live in sibling modules.
"""

from __future__ import annotations

import uuid
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, NamedTuple, cast

from agnette.agl.diagnostics import AglError, Diagnostic, HiddenMemberError
from agnette.agl.repl.entry import (
    REPL_LABEL,
    EntryKind,
    EntryResult,
    FileOrigin,
    Placement,
    relocate,
)
from agnette.agl.repl.entry_pipeline import EntryPipeline, unused_module_doc_warnings
from agnette.agl.repl.info_docs import format_doc_attribute, module_regions
from agnette.agl.repl.info_sections import SectionLabel, sourced_label
from agnette.agl.runtime.externs import ExternRuntimeState
from agnette.agl.runtime.sessions import AgentDispatcherSessionHost
from agnette.agl.self_validation import self_validation_enabled
from agnette.agl.syntax import NULLARY_BUILTIN_TYPE_NAMES
from agnette.agl.syntax.spans import UNKNOWN_SOURCE
from agnette.agl.warning_policy import apply_program_policy

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from agnette.agent.spec_defaults import AgentSpecResolver
    from agnette.agl.eval.ir_interpreter import IrInterpreter
    from agnette.agl.infix import Fixity
    from agnette.agl.ir.builtin_vars import BuiltinVarKey
    from agnette.agl.ir.ids import SymbolId
    from agnette.agl.ir.program import ValueDescriptors
    from agnette.agl.ir.static_keys import StaticBindingKey
    from agnette.agl.lexer import LexedSource
    from agnette.agl.matchcompile import MatchCompiledProgram
    from agnette.agl.modules.ids import ModuleId
    from agnette.agl.modules.loader import LoadedModule
    from agnette.agl.modules.roots import RootSet
    from agnette.agl.runtime.agents import AgentFn
    from agnette.agl.runtime.host_settings import HostSettingsPolicy
    from agnette.agl.runtime.sessions import SessionHost
    from agnette.agl.runtime.types import ParamBindingInfo
    from agnette.agl.scope.program import ResolvedModule, ResolvedProgram
    from agnette.agl.scope.symbols import (
        BindingRef,
        ConstructorRef,
        DeclarationKey,
        QName,
        ScopeNode,
        TypeOwner,
    )
    from agnette.agl.semantics.type_table import NominalOwner, TypeTable
    from agnette.agl.semantics.types import ExceptionType, Type
    from agnette.agl.semantics.values import BoolValue, Frame, RecordValue, Value
    from agnette.agl.syntax.advisories import SpacedQualifier
    from agnette.agl.syntax.nodes import (
        Attribute,
        ExportDecl,
        ImportDecl,
        Program,
        QualifierChain,
        ScopeRegion,
        TypeAlias,
        VarRef,
    )
    from agnette.agl.syntax.spans import SourceId, SourceSpan
    from agnette.agl.syntax.types import TypeExpr
    from agnette.agl.typecheck.env import (
        CheckedModule,
        ConstructorSignature,
        FunctionSignature,
        TypeEnvironment,
    )
    from agnette.agl.warning_policy import WarningOutcome, WarningPolicy
    from agnette.packages.model import PackageInfo
    from agnette.sandbox.prepare import SandboxContext


# Layout-only token types that carry no statement to evaluate.
_TRIVIAL_TOKENS: frozenset[str] = frozenset({"_NEWLINE", "_INDENT", "_DEDENT"})
_TRANSCRIPT_HEADER = "# agm:repl-transcript:v1\n"
_TRANSCRIPT_ENTRY_PREFIX = "# agm:entry:"


_TRANSCRIPT_ALLOW_MARK = " allow="


class _SavedEntry(NamedTuple):
    """One transcript entry: its *text*, file offset and the ``@!allow`` names it ran under."""

    text: str
    offset: int
    allows: frozenset[str]


def _decode_transcript(text: str) -> tuple[_SavedEntry, ...] | None:
    """Decode a framed ``:save`` transcript into its entries.

    An entry header is ``# agm:entry:LENGTH``, followed by `` allow=NAME,...`` when a loaded
    file's leading ``@!allow`` governed it. Returns ``None`` for an ordinary file.
    """
    if not text.startswith(_TRANSCRIPT_HEADER):
        return None
    entries: list[_SavedEntry] = []
    offset = len(_TRANSCRIPT_HEADER)
    while offset < len(text):
        if not text.startswith(_TRANSCRIPT_ENTRY_PREFIX, offset):
            return None
        length_end = text.find("\n", offset)
        if length_end < 0:
            return None
        rendered_length, _, rendered_allows = text[
            offset + len(_TRANSCRIPT_ENTRY_PREFIX) : length_end
        ].partition(_TRANSCRIPT_ALLOW_MARK)
        if not rendered_length.isdecimal():
            return None
        entry_start = length_end + 1
        entry_end = entry_start + int(rendered_length)
        if entry_end >= len(text) or text[entry_end] != "\n":
            return None
        allows = frozenset(filter(None, rendered_allows.split(",")))
        entries.append(_SavedEntry(text[entry_start:entry_end], entry_start, allows))
        offset = entry_end + 1
    return tuple(entries)


def _origin_at(label: str, text: str, offset: int) -> FileOrigin:
    """The origin of the *text* slice starting at *offset*, in the file named *label*."""
    return FileOrigin(label, text.count("\n", 0, offset) + 1, offset - text.rfind("\n", 0, offset))


@dataclass(frozen=True, slots=True)
class _BootstrapSnapshot:
    """Reusable checked image for an unchanged default-standard-library graph."""

    modules: dict["ModuleId", "LoadedModule"]
    warnings: tuple[Diagnostic, ...]
    type_env: "TypeEnvironment"
    next_node_id: int
    source_texts: tuple[tuple["Path", str], ...]
    wildcard_matches: tuple[tuple[tuple[str, ...], tuple[tuple["ModuleId", "Path"], ...]], ...]
    resolved_modules: dict["ModuleId", "ResolvedModule"]
    checked_modules: dict["ModuleId", "CheckedModule"]


class DiagnosedError(AglError):
    """An error that carries its diagnostics as already built."""

    def __init__(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        super().__init__(diagnostics[0].message)
        self._diagnostics = diagnostics

    def to_diagnostic(self) -> Diagnostic:
        return self._diagnostics[0]

    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return self._diagnostics


class SessionOpenError(DiagnosedError):
    """The library image was rejected; carries every :meth:`ReplSession.open` diagnostic."""


@dataclass(frozen=True, slots=True)
class _InfoName:
    """An ``:info`` name parsed once as an ad-hoc entry."""

    program: "Program"
    next_node_id: int
    spaced_qualifiers: "tuple[SpacedQualifier, ...]"
    reference: "VarRef | None"


@dataclass(frozen=True, slots=True)
class _InfoReference:
    """Binding, constructor, and type declaration one ``:info`` name selects, and its spelling."""

    binding: "BindingRef | None"
    constructor: "ConstructorRef | None"
    type_key: "DeclarationKey | None"
    reference: "VarRef"
    # Query-scoped view carrying this reference's own ad-hoc qualifier
    # identities; read only by :meth:`ReplSession._constructor_signature`.
    type_env: "TypeEnvironment"
    # Whether the name selects a builtin type, which has no declaration to select.
    builtin_type: bool = False


# Bootstrap images retain complete frontend graphs, so keep only a small working
# set per process. LRU preserves the images active projects are most likely to
# reopen without allowing one-off roots to grow the
# resident cache indefinitely.
BOOTSTRAP_CACHE_MAX_ENTRIES = 16
_bootstrap_cache: OrderedDict[tuple[object, ...], _BootstrapSnapshot] = OrderedDict()
_bootstrap_cache_lock = Lock()


def _module_match_sort_key(match: tuple["ModuleId", "Path"]) -> tuple[str, ...]:
    """Return the deterministic module-id key for a discovery match."""
    return match[0].segments


def has_runnable_statements(source: "str | LexedSource") -> bool:
    """Return ``True`` when *source* contains at least one statement to evaluate.

    Blank, whitespace-only, and comment-only entries (AgL comments run from a
    ``#`` to end of line) have nothing to run.  The check tokenizes *source* with
    the real AgL lexer and looks for any non-trivial token — the lexer skips
    whitespace and comments entirely and emits no tokens for blank/comment-only
    input, while synthetic layout tokens (``_NEWLINE`` / ``_INDENT`` /
    ``_DEDENT``) carry no statement, so they are ignored.  A lexically invalid
    entry is treated as *runnable* so the entry flows on to ``eval_entry`` and
    surfaces a real diagnostic rather than being silently dropped.

    Shared by the interactive console (blank-line handling) and ``load_file``
    (an empty / comment-only file loads as a benign no-op).  Such source parses
    to an empty module, so this only spares the caller a pointless pipeline run.
    """
    from agnette.agl.lexer import lexed_source

    lexed = lexed_source(source)
    return lexed.failure is not None or any(
        token.type not in _TRIVIAL_TOKENS for token in lexed.tokens
    )


def _region_path(region: "ScopeRegion") -> tuple[str, ...]:
    """Return the scope path *region* opens, as the entry spelled it.

    A multi-segment header parses into nested single-segment regions, so a
    region whose only item is another region is spelled back as the equivalent
    multi-segment path; a region that also declares members ends the path.
    """
    from agnette.agl.syntax.nodes import ScopeRegion

    path = [region.segment.name]
    current = region
    while len(current.items) == 1 and isinstance(current.items[0], ScopeRegion):
        current = current.items[0]
        path.append(current.segment.name)
    return tuple(path)


def _static_verdict[T](probe: Callable[[], T]) -> T | AglError:
    """Run one static REPL probe, returning its rejection as a value instead of raising it.

    The one boundary where an introspection probe's diagnostic becomes data:
    a caller weighing two readings of one spelling inspects both verdicts.
    """
    try:
        return probe()
    except AglError as exc:
        return exc


def _declared_spelling(scope_path: tuple[str, ...], decl_name: str) -> str:
    """How ``:info`` names a declaration: its path *scope_path*/*decl_name* in its module.

    Never the queried spelling nor module-qualified (``def Base::h`` for
    ``G::h`` with ``type G = Base``, ``def print`` for ``log::print``).
    """
    return "::".join((*scope_path, decl_name))


def _format_repl_signature(signature: "FunctionSignature") -> str:
    """Render the AgL source surface of a function signature for ``:info``."""
    params = [f"{param.name}: {param.type!r}" for param in signature.params]
    generic = f"[{', '.join(signature.type_params)}]" if signature.type_params else ""
    return f"{generic}({', '.join(params)}) -> {signature.result!r}"


def _indefinite(kind: str) -> str:
    """Prefix a type *kind* (``record``, ``enum``, ``exception``) with its indefinite article."""
    return f"{'an' if kind[0] in 'aeiou' else 'a'} {kind}"


def _format_info_section(
    label: SectionLabel,
    code: str,
    location: str | None = None,
    doc: str | None = None,
    source: str | None = None,
) -> str:
    """Format one labelled ``:info`` code section, headed by its ``@doc``, and a location.

    A *source* qualifies the label (``Module (lib):``).
    """
    if doc is not None:
        code = f"{format_doc_attribute(doc)}\n{code}"
    indented_code = "\n".join(f"  {line}" if line else "" for line in code.splitlines())
    location_line = "" if location is None else f"\nLocation: {location}"
    heading = label if source is None else sourced_label(label, source)
    return f"{heading}:\n{indented_code}{location_line}"


def _qname_order(qname: "QName") -> tuple[str, str]:
    """Order scope origins by module route, then scope path."""
    module, atom = qname
    return module.path_str(), str(atom)


def _count_phrase(kinds: Sequence[str]) -> str:
    """Phrase the *kinds* counted in order of first appearance: a module and two scopes."""
    counts = Counter(kinds)
    words = ("a", "two", "three", "four", "five", "six", "seven", "eight", "nine")
    parts = [
        f"a {kind}" if count == 1 else f"{words[count - 1] if count <= 9 else count} {kind}s"
        for kind, count in counts.items()
    ]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"


def _documented_header(header: str, doc: str | None) -> str:
    """Return *header*, headed by its inner ``@!doc`` attribute when *doc* is given."""
    return header if doc is None else f"{format_doc_attribute(doc, inner=True)}\n{header}"


def _format_constructor_signature(signature: "ConstructorSignature") -> str:
    """Render one constructor's AgL call signature for ``:info``, headed by its declared path.

    The path is the constructed record's or exception's (``Src::Mem`` for
    ``C::Mem`` with ``type C = Src[int]``).
    """
    result = signature.result_template
    name = _declared_spelling(result.scope_path, result.name)
    generic = f"[{', '.join(signature.type_params)}]" if signature.type_params else ""
    params = ", ".join(
        f"{field}: {typ!r}" for field, typ in zip(signature.field_names, signature.field_templates)
    )
    return f"{name}{generic}({params}) -> {result!r}"


# ---------------------------------------------------------------------------
# ReplSession — the persistent incremental driver
# ---------------------------------------------------------------------------


class ReplSession:
    """Persistent incremental AgL evaluation session (UI-free core).

    Constructor parameters mirror ``PipelineDriver`` so a host can wire the same
    value-driven agent dispatcher.

    Each entry is incrementally linked and executed against a persistent IR base
    frame. Completed effects survive a later runtime failure in the same entry.
    """

    def __init__(
        self,
        *,
        default_strict_json: bool = False,
        default_call_depth_limit: int | None = None,
        agent_dispatcher: "AgentFn | None" = None,
        session_host: "SessionHost | None" = None,
        get_sandbox_context: "Callable[[], SandboxContext] | None" = None,
        resolve_agent_spec: "AgentSpecResolver | None" = None,
        shell_exec_timeout: float | None = None,
        trace_path: "Path | None" = None,
        echo_agent_output: bool = False,
        engine_base: "Mapping[str, Value] | None" = None,
        builtin_var_seeds: "Mapping[BuiltinVarKey, Value] | None" = None,
        param_seed_resolver: (
            "Callable[[ModuleId, tuple[ParamBindingInfo, ...]], Mapping[StaticBindingKey, object]]"
            " | None"
        ) = None,
        process_environment: "Mapping[str, str] | None" = None,
        host_settings_policy: "HostSettingsPolicy | None" = None,
        cwd: "Path | None" = None,
        stdlib_root: "Path | None" = None,
        stdlib_installed: bool = False,
        lib_root: "Path | None" = None,
        configured_roots: "Iterable[tuple[str, Path]]" = (),
        package_roots: "Iterable[PackageInfo]" = (),
        default_stdlib: bool = True,
        warning_policy: "WarningPolicy | None" = None,
    ) -> None:
        from agnette.agl.ir.builtin_nominals import NO_BUILTIN_DECLARATIONS
        from agnette.agl.lower import LinkImage
        from agnette.agl.pipeline import PipelineDriver
        from agnette.agl.runtime.option import some_value
        from agnette.agl.scope.symbols import ScopeNode
        from agnette.agl.semantics.values import TextValue
        from agnette.agl.typecheck.env import TypeEnvironment
        from agnette.core.parse import format_timeout

        self._default_stdlib = default_stdlib
        self._process_environment = (
            dict(process_environment) if process_environment is not None else None
        )
        self._builtin_var_seed: dict[BuiltinVarKey, Value] = {}
        self._builtin_var_values: dict[BuiltinVarKey, Value] = {}
        # Called with the first new module declaring parameters and all new parameters;
        # the host reports a configuration failure as an ``AglError``.
        self._param_seed_resolver = param_seed_resolver
        # Decoded parameter values survive in the session alongside host-backed
        # builtin values. They are passed to each fresh interpreter, which only
        # consumes a seed while its static initializer first runs.
        self._param_seed_values: dict[StaticBindingKey, Value] = {}
        # ``open()`` checks modules before an entry lowers them. Keep its raw
        # resolver output until the first executable that initializes those
        # modules can decode it with its own parameter decoder table.
        self._pending_param_raw_values: dict[StaticBindingKey, object] = {}
        # ``strict-json`` is the one engine key whose host argument cannot be
        # folded into the seed map below: it is a plain ``bool`` with no way to
        # spell "the host did not specify a value", so an unseeded ``False``
        # would be indistinguishable from an explicit host control and would
        # permanently block a later ``std/config`` declared default from ever
        # taking effect (see the ``_default_strict_json`` property). It is
        # instead kept as the lowest-priority floor, consulted only when
        # neither a host seed nor a declared default exists for the key.
        self._strict_json_floor = default_strict_json
        # ONE seed map for all engine keys: this session's constructor- and
        # :reset-time baseline. A host control -- an explicit ``engine_base``
        # entry, or ``shell_exec_timeout`` folded in
        # below -- is set once here and never changes; a ``std/config``
        # declared default is recorded at most once, the first time an entry
        # loads it (see :meth:`_record_declared_engine_defaults`), and never
        # overwrites an existing entry, so a host control always wins. Adding
        # an engine key touches only this map and its ``Value`` conversion in
        # :meth:`_engine_snapshot`.
        self._engine_seed: dict[str, Value] = dict(engine_base) if engine_base is not None else {}
        if builtin_var_seeds is not None:
            from agnette.agl.ir.builtin_vars import is_engine_builtin_var_key

            for key, value in builtin_var_seeds.items():
                if is_engine_builtin_var_key(key):
                    self._engine_seed[key[2]] = value
                else:
                    self._builtin_var_seed[key] = value
        self._builtin_var_values = dict(self._builtin_var_seed)
        if "timeout" not in self._engine_seed and shell_exec_timeout is not None:
            self._engine_seed["timeout"] = some_value(
                TextValue(format_timeout(shell_exec_timeout)), nominals=NO_BUILTIN_DECLARATIONS
            )
        # shell-exec-timeout is backed by a plain scalar rather than derived on
        # every read from ``_current`` (unlike ``_default_strict_json`` below):
        # the interpreter already parses its own authoritative float from the
        # identical source text (``IrInterpreter.shell_exec_timeout``, kept in
        # lockstep with the ``timeout`` register it writes alongside), so
        # re-deriving it independently here would just be a second, and
        # possibly diverging, parse of the same text.
        # :meth:`_seeded_timeout_seconds` resolves the seed-derived value at
        # construction and :reset; :meth:`_update_engine_settings` overwrites
        # it from a completed entry's own resolved state.
        self._shell_exec_timeout: float | None = self._seeded_timeout_seconds()
        # ONE current-value map for the engine keys' ``Value`` registers
        # (strict-json, timeout, trace, trace-file, default-agent): the live
        # setting each entry's interpreter is built with and reads back after
        # a write. Starts equal to the seed -- nothing has run yet -- is
        # overwritten from a completed entry's own resolved state afterward
        # (:meth:`_update_engine_settings`), and is restored to the seed by
        # :meth:`reset`. ``_default_strict_json`` is a read-only view onto
        # this map rather than a separately stored copy.
        self._current: dict[str, Value] = dict(self._engine_seed)
        self._host_settings_policy = host_settings_policy
        # Trace destination: when set, each evaluated entry opens a fresh
        # ``TraceStore`` appending JSONL records to this one file. The COMMAND
        # validates/creates the path up front; the session assumes it is writable
        # but the no-op store tolerates failure (it disables itself).
        self._trace_path = trace_path
        self._initial_trace_path = trace_path
        self._echo_agent_output = echo_agent_output

        # Internal runtime owns the registrations + host-environment assembly.
        # It never runs an entry on the session's behalf, so it is given none
        # of the three live engine settings: the session owns those (above) and
        # threads them into each per-entry interpreter directly (see
        # :mod:`agnette.agl.repl.entry_pipeline`).
        effective_session_host = (
            session_host
            if session_host is not None
            else AgentDispatcherSessionHost(agent_dispatcher)
        )
        self._session_host = effective_session_host
        # Companion state (``std/fs`` temporary paths, ...) lives for the whole
        # session: every entry's interpreter shares this bag; :meth:`close` ends it.
        self._extern_runtime_state = ExternRuntimeState()
        self._runtime = PipelineDriver(
            default_call_depth_limit=default_call_depth_limit,
            agent_dispatcher=agent_dispatcher,
            session_host=effective_session_host,
            get_sandbox_context=get_sandbox_context,
            resolve_agent_spec=resolve_agent_spec,
            warning_policy=warning_policy,
        )
        # Applied to each entry's warnings, like the driver's policy to a program's.
        self._warning_policy = self._runtime.warning_policy
        # Reuse the driver's resolved (default-applied) limit for the per-entry
        # interpreters this session builds directly, so the canonical default
        # lives in exactly one place.
        self._default_call_depth_limit = self._runtime.default_call_depth_limit
        # Persistent session environment.
        self._session_scope: ScopeNode = ScopeNode(node_id=-1, parent=None)
        self._session_scope_nodes: dict[tuple[str, ...], ScopeNode] = {(): self._session_scope}
        # Each retained type-owned path maps to the owner it resolved to when
        # declared, so a retained alias keeps its target across entries.
        self._session_type_paths: dict[tuple[str, ...], TypeOwner] = {}
        # The empty-entry resolution of the one `info_of` query running, if made.
        self._info_probe: "ResolvedProgram | None" = None
        # Builtin type and def identities of the current declarations, keyed by
        # scoped name to the declaring module and its enclosing scope path. A
        # REPL entry's own source cannot see an earlier entry's builtin
        # declaration in its module graph (each entry recompiles a fresh entry
        # module), so ``validate_builtin_declaration_uniqueness`` reads this
        # instead to catch a later entry importing the same scoped builtin.
        self._session_builtin_declarations: dict[
            tuple[str, ...], tuple["ModuleId", tuple[str, ...]]
        ] = {}
        self._type_env: TypeEnvironment = TypeEnvironment()
        self._type_env.seal()
        self._link_image = LinkImage()
        self._ir_base_frame: Frame = {}
        self._next_node_id: int = 0
        self._open_failure: tuple[Diagnostic, ...] = ()
        # Source log of successfully-promoted entries (for dump_source / :save).
        self._source_log: list[tuple[str, frozenset[str]]] = []

        # Module roots configuration.
        # These are stored so _ensure_roots() can assemble the RootSet lazily.
        self._cwd: Path | None = cwd
        # Left unresolved when not given explicitly: resolving it here could
        # let a stdlib version mismatch escape the constructor, which must
        # stay total -- many callers construct a session with no surrounding
        # try/except. :meth:`_ensure_roots`
        # resolves it lazily on first use instead, inside the same try/except
        # every other module-loading failure already goes through
        # (:meth:`open`, :meth:`eval_entry`).
        self._stdlib_root: Path | None = stdlib_root
        self._stdlib_installed = stdlib_installed
        self._lib_root: Path | None = lib_root
        self._configured_roots: tuple[tuple[str, Path], ...] = tuple(configured_roots)
        self._package_roots: tuple[PackageInfo, ...] = tuple(package_roots)
        # Lazily assembled RootSet (set directly in tests via s._roots = ...).
        self._roots: RootSet | None = None
        # Cached lib modules from prior REPL program entries.
        self._loaded_lib_modules: dict[ModuleId, LoadedModule] = {}
        self._pending_bootstrap_warnings: list[Diagnostic] = []
        # Warning names a loaded file's leading ``@!allow`` silences in every entry of it.
        self._inherited_allows: frozenset[str] = frozenset()
        # Each entry's own source label, with the file place its text starts at (``None``
        # for a typed entry): locations in any entry's source map back to that entry.
        self._entry_origins: dict[str, FileOrigin | None] = {}
        self._entry_label: str | None = None
        # Compilation artifacts of the library modules this session has loaded,
        # retained across entries. Each pass reuses one only while the entry's
        # graph still holds the very AST object the artifact was derived from,
        # so a reparse or a redeclaration simply misses.
        self._retained_resolved_modules: dict[ModuleId, ResolvedModule] = {}
        self._retained_checked_modules: dict[ModuleId, CheckedModule] = {}
        self._last_match_compilation: MatchCompiledProgram | None = None
        # ``@doc`` prose of promoted entry declarations by node id, and the
        # promoted entry scope regions with their ``@!doc`` prose: what
        # ``:info`` shows for them (library modules keep theirs in their
        # retained attributes).
        self._session_docs: dict[int, str] = {}
        self._session_regions: dict[tuple[str, ...], str | None] = {}
        # Imports generally persist across successfully promoted program entries,
        # one retained generation per entry, kept as written so a wildcard keeps
        # tracking the module set. Declarations for a module named in a later
        # generation replace earlier ones; the rest are prepended in program
        # context for reuse. Resolved ``use`` contributions live in scope nodes.
        self._accumulated_imports: list[tuple["ImportDecl", ...]] = []
        self._accumulated_scoped_imports: list[tuple["ImportDecl | ScopeRegion", ...]] = []
        # The fixities prior promoted entries declared, by operator name: the
        # session is one module, so each entry declares them too.
        self._accumulated_infix: dict[str, Fixity] = {}
        self._entry_pipeline = EntryPipeline(self)

    # ------------------------------------------------------------------
    # Module roots
    # ------------------------------------------------------------------

    def _ensure_roots(self) -> "RootSet":
        """Build the ``RootSet`` lazily on first import use.

        Resolves ``_stdlib_root`` here too when the constructor was not given
        one explicitly, using the same AgM-home seam ``commands/repl.py`` uses
        (:func:`agnette.config.context.current_config_context`) rather than a
        hardcoded ``Path.home()``, so a relocated ``AGM_HOME`` is honored the
        same way here as everywhere else. A stdlib version mismatch therefore
        surfaces from inside the caller's own try/except
        (:meth:`open`, :meth:`eval_entry`) instead of from
        construction; it is raised as an ``AglError``. The resolved selection is
        cached back onto ``_stdlib_root`` and ``_stdlib_installed``, so a later :reset --
        which only clears ``_roots`` -- reuses it rather than re-resolving.
        """
        if self._roots is not None:
            return self._roots
        from pathlib import Path

        from agnette.agl.modules.roots import assemble_roots
        from agnette.config.context import current_config_context
        from agnette.config.module_roots import StdlibResolutionError, select_stdlib_root

        cwd = self._cwd if self._cwd is not None else Path.cwd()
        if self._stdlib_root is None:
            try:
                selection = select_stdlib_root(
                    home=current_config_context(cwd=cwd).home, anchor=cwd
                )
            except StdlibResolutionError as exc:
                raise AglError(str(exc)) from exc
            self._stdlib_root = selection.root
            self._stdlib_installed = selection.installed
        self._roots = assemble_roots(
            invocation_root=cwd,
            stdlib_root=self._stdlib_root,
            stdlib_installed=self._stdlib_installed,
            lib_root=self._lib_root,
            configured=self._configured_roots,
            cli=(),
            cwd=cwd,
            package_roots=self._package_roots,
        )
        return self._roots

    # ------------------------------------------------------------------
    # Session bootstrap
    # ------------------------------------------------------------------

    def open(self) -> tuple[Diagnostic, ...]:
        """Load the session's initial library image before any entry runs.

        The rejection, if any, is remembered until :meth:`reset` or the next call (see
        :meth:`info_of`).

        Builds the module graph a first entry would build (honoring
        ``default_stdlib``), then resolves and type-checks the result.

        On success, the loaded library modules, the node-id counter, and the
        session's type environment are promoted into the session's caches
        the same way a successful entry promotes its own ``new_modules`` (see
        ``EntryPipeline._evaluate_ir_program``), so the first real entry finds
        them already cached rather than recompiling them — this method never
        lowers or evaluates anything, so a module is marked ``linked`` (and
        the standard-library companion, if any, imported) only once an entry
        actually executes, exactly as today.

        Returns the rejection diagnostics, or an empty tuple on success. Bootstrap
        warnings are governed before promotion; reported warnings accompany the next
        entry once. A host calls this once, right after constructing the session and before
        it accepts any entry or prints a banner. Every source, module-loading,
        stdlib-root, or configuration failure is an ``AglError`` and becomes a
        diagnostic, exactly as :meth:`EntryPipeline.eval_entry` reports it for
        an ordinary entry.
        """
        diagnostics = self._open_image()
        self._open_failure = diagnostics
        return diagnostics

    def _open_image(self) -> tuple[Diagnostic, ...]:
        """Load and promote the library image; return its rejection diagnostics."""
        from agnette.agl.lexer import tab_warning_collector
        from agnette.agl.parser import parse_program_seeded

        try:
            roots = self._ensure_roots()
        except AglError as exc:
            return (exc.to_diagnostic(),)
        cache_key = self._bootstrap_cache_key(roots)
        with _bootstrap_cache_lock:
            if cache_key is not None:
                cached = _bootstrap_cache.get(cache_key)
                if cached is not None:
                    if self._bootstrap_loader_inputs_match(
                        cached
                    ) and self._bootstrap_discovery_matches(cached, roots):
                        _bootstrap_cache.move_to_end(cache_key)
                        outcome = self._govern_warnings(
                            cached.warnings,
                            cached.resolved_modules.values(),
                            scope_complete=True,
                        )
                        if outcome.denied:
                            return outcome.denied
                        self._restore_bootstrap(cached)
                        self._pending_bootstrap_warnings.extend(outcome.reported)
                        return ()
                    # A source edit invalidates this image permanently; retaining it
                    # until ordinary LRU eviction would waste one of the few slots.
                    _bootstrap_cache.pop(cache_key)

            # A throwaway unit expression: side-effect-free and never a Binder or
            # Declaration, so it never becomes a scope member. Its own node ids
            # are consumed from the counter like any other entry's and never
            # referenced again once ``_next_node_id`` moves past them.
            program, next_start_id = parse_program_seeded("()", start_id=self._next_node_id)

            resolutions: dict[ModuleId, ResolvedModule] = {}
            with tab_warning_collector() as tab_sink:
                try:
                    loaded = self._entry_pipeline.load_and_check_program(
                        pipeline_program=program,
                        next_start_id=next_start_id,
                        source_text="()",
                        resolutions=resolutions,
                    )
                except AglError as exc:
                    outcome = self._govern_warnings(
                        tab_sink, resolutions.values(), scope_complete=False
                    )
                    return (exc.to_diagnostic(), *outcome.denied)

            bootstrap_warnings = [
                *tab_sink,
                *(
                    warning
                    for module_id, module in loaded.checked_program.modules.items()
                    if module_id == loaded.checked_program.entry_id
                    or module_id in loaded.new_modules
                    for warning in module.warnings
                ),
            ]
            outcome = self._govern_warnings(
                bootstrap_warnings,
                loaded.resolved_program.modules.values(),
                scope_complete=True,
            )
            if outcome.denied:
                return outcome.denied

            self._loaded_lib_modules.update(loaded.new_modules)
            self._pending_param_raw_values.update(loaded.raw_param_values)
            self._next_node_id = loaded.new_next_id
            checked_program = loaded.checked_program
            self._type_env = checked_program.modules[checked_program.entry_id].type_env
            if cache_key is not None:
                _bootstrap_cache[cache_key] = _BootstrapSnapshot(
                    modules=dict(loaded.new_modules),
                    warnings=tuple(bootstrap_warnings),
                    type_env=self._type_env,
                    next_node_id=self._next_node_id,
                    source_texts=tuple(
                        (module.path, module.source_text)
                        for module in loaded.new_modules.values()
                        if module.path is not None
                    ),
                    wildcard_matches=self._bootstrap_wildcard_matches(loaded.new_modules),
                    resolved_modules=dict(self._retained_resolved_modules),
                    checked_modules=dict(self._retained_checked_modules),
                )
                _bootstrap_cache.move_to_end(cache_key)
                while len(_bootstrap_cache) > BOOTSTRAP_CACHE_MAX_ENTRIES:
                    _bootstrap_cache.popitem(last=False)
            self._pending_bootstrap_warnings.extend(outcome.reported)
            return ()

    def _bootstrap_cache_key(self, roots: "RootSet") -> tuple[object, ...] | None:
        """Return a cache key only for a pristine, default-stdlib session.

        A bootstrap cache is safe only before an entry can contribute session
        state, and when module roots agree — typed engine seeds never touch
        the compiled module graph, so they do not disqualify reuse. A param-seed
        resolver does (so ``agm repl``, which always passes one, never uses the
        cached image). The cached type environment is
        sealed; later entry checks seed from it without mutating it.
        """
        if (
            not self._default_stdlib
            or self._param_seed_resolver is not None
            or self._next_node_id != 0
            or self._loaded_lib_modules
        ):
            return None
        packages = tuple(
            sorted(
                (package.root, package.manifest.name, str(package.manifest.version))
                for package in roots.packages
            )
        )
        return (roots.roots, roots.stdlib_roots, packages)

    @staticmethod
    def _bootstrap_loader_inputs_match(snapshot: _BootstrapSnapshot) -> bool:
        """Return whether cached source and required extern companions still satisfy loading."""
        from agnette.core import fs
        from agnette.util.text import normalize_newlines

        try:
            return all(
                normalize_newlines(path.read_text(encoding="utf-8")) == source
                for path, source in snapshot.source_texts
            ) and all(
                module.companion_path is None or fs.is_file(module.companion_path)
                for module in snapshot.modules.values()
            )
        except (OSError, UnicodeError):
            return False

    @staticmethod
    def _bootstrap_wildcard_matches(
        modules: Mapping["ModuleId", "LoadedModule"],
    ) -> tuple[tuple[tuple[str, ...], tuple[tuple["ModuleId", "Path"], ...]], ...]:
        """Capture each loaded wildcard's exact root-discovery result.

        Every module whose id is under a wildcard prefix was necessarily
        discovered by that wildcard: direct imports use the same roots and
        mount policy. Capturing those paths therefore avoids a second root
        traversal when a new snapshot is inserted, while later cache hits can
        compare them with the resolver's authoritative expansion.
        """
        prefixes: set[tuple[str, ...]] = set()

        def record_wildcards(declarations: Iterable["ImportDecl | ExportDecl"]) -> None:
            prefixes.update(tuple(decl.module_path) for decl in declarations if decl.wildcard)

        for module in modules.values():
            record_wildcards(module.imports)
            record_wildcards(module.export_decls)

        def matches_for(prefix: tuple[str, ...]) -> tuple[tuple["ModuleId", "Path"], ...]:
            matches: list[tuple[ModuleId, Path]] = []
            for module_id, module in modules.items():
                if module.path is not None and module_id.segments[: len(prefix)] == prefix:
                    matches.append((module_id, module.path))
            return tuple(sorted(matches, key=_module_match_sort_key))

        return tuple((prefix, matches_for(prefix)) for prefix in sorted(prefixes))

    @staticmethod
    def _bootstrap_discovery_matches(snapshot: _BootstrapSnapshot, roots: "RootSet") -> bool:
        """Return whether current root discovery still produces the cached graph.

        Source text alone cannot show a newly created competing module file.
        Re-resolving each cached id preserves the resolver's global-uniqueness
        rule, while replaying wildcard discovery also notices newly matched
        modules.
        """
        from agnette.agl.modules.resolver import expand_wildcard, resolve_module

        try:
            for module_id, module in snapshot.modules.items():
                if resolve_module(module_id, roots) != module.path:
                    return False
            return all(
                tuple(expand_wildcard(prefix, roots).items()) == matches
                for prefix, matches in snapshot.wildcard_matches
            )
        except (AglError, OSError):
            # A cache-validation failure must be retried through normal loading,
            # which turns it into the same user-facing diagnostic as a cold start.
            return False

    def _restore_bootstrap(self, snapshot: _BootstrapSnapshot) -> None:
        """Install an immutable bootstrap image into this otherwise fresh session."""
        self._loaded_lib_modules.update(snapshot.modules)
        self._next_node_id = snapshot.next_node_id
        self._type_env = snapshot.type_env
        self._retained_resolved_modules = dict(snapshot.resolved_modules)
        self._retained_checked_modules = dict(snapshot.checked_modules)

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def eval_entry(
        self,
        text: str,
        *,
        check_only: bool = False,
        origin: FileOrigin | None = None,
        lexed: "LexedSource | None" = None,
    ) -> EntryResult:
        """Parse → resolve → typecheck → matchcompile → lower/eval one entry.

        The entry has a source identity of its own. Locations in the result, whichever
        entry's source they lie in, are entry-relative, or file-relative for an entry
        evaluated with an *origin* (where its text starts in a file). A caller that already
        lexed *text* passes its *lexed* source, which every reading of the entry then
        shares; a source recorded for any other text is ignored.

        Completed runtime initializers are promoted even when a later initializer
        fails.

        REPL-only fallback: when the entry fails to evaluate as a program, the
        loop tries to read it as a bare type expression (e.g. ``int``, a declared
        enum/record name, a bare generic type definition, ``array[T]``).  If
        that resolves to a known type, a ``kind == "type"`` result echoing the
        type is returned instead of the original ``'X' is not defined`` error.
        Entries that evaluate
        successfully as values are never intercepted, so record constructors and
        bindings keep their normal echo.
        """
        source = self._begin_entry(origin)
        result = self._eval_entry(text, check_only=check_only, source=source, lexed=lexed)
        return relocate(result, self._place, unplaced=REPL_LABEL)

    def _next_entry_source(self) -> "SourceId":
        """The source identity :meth:`_begin_entry` gives the next entry."""
        from agnette.agl.syntax.spans import SourceId

        return SourceId(f"<repl {len(self._entry_origins)}>")

    def _begin_entry(self, origin: FileOrigin | None) -> "SourceId":
        """Give the next entry its source identity, placed by *origin* in its file if any."""
        source = self._next_entry_source()
        self._entry_origins[source.label] = origin
        self._entry_label = source.label
        return source

    def _place(self, location: Placement) -> Placement:
        """Move a location in an entry's source to where that entry's text lies; others stay."""
        label, line, column = location
        if label not in self._entry_origins:
            return location
        origin = self._entry_origins[label]
        return (None, line, column) if origin is None else origin.place((None, line, column))

    def user_place(
        self, label: str | None, line: int, column: int | None
    ) -> tuple[str, int, int | None]:
        """Place a location as users see it: in its file if loaded, else under ``<repl>``."""
        placed, line, column = self._place((label, line, column))
        if placed is None or placed == UNKNOWN_SOURCE.label:
            placed = REPL_LABEL
        return placed, line, column

    def _repl_location(self, span: "SourceSpan") -> str:
        """Render a declaration source location for REPL introspection."""
        label, line, column = self.user_place(span.source.label, span.start_line, span.start_col)
        return f"{label}:{line}:{column}"

    def _eval_entry(
        self, text: str, *, check_only: bool, source: "SourceId", lexed: "LexedSource | None"
    ) -> EntryResult:
        """Evaluate one entry parsed with the source identity *source* (see :meth:`eval_entry`)."""
        result, next_id, lexed = self._eval_entry_pipeline(
            text, check_only=check_only, source=source, lexed=lexed
        )
        return self._settle_entry(text, result, next_id=next_id, lexed=lexed, source=source)

    def _settle_entry(
        self,
        text: str,
        result: EntryResult,
        *,
        next_id: int,
        lexed: "LexedSource | None",
        source: "SourceId",
    ) -> EntryResult:
        """Finish the *result* of reading *text* as a program, or read it as a bare type.

        The type reading is for a static rejection of a text that lexed, and reuses the
        program reading's *lexed* source: no stage runs twice on the text.
        """
        # Whatever the verdict, the next ``info_of`` reopens the library image afresh.
        self._open_failure = ()
        if self._pending_bootstrap_warnings:
            result.warnings[:0] = self._pending_bootstrap_warnings
            self._pending_bootstrap_warnings.clear()
        if result.failure is not None and lexed is not None:
            type_result = self._try_type_entry(
                text, next_id=next_id, value_failure=result.failure, source=source, lexed=lexed
            )
            if type_result is not None:
                return type_result
        return result

    def _try_type_entry(
        self,
        text: str,
        *,
        next_id: int,
        value_failure: AglError | None,
        source: "SourceId",
        lexed: "LexedSource | None" = None,
        display: bool = True,
    ) -> EntryResult | None:
        """Attempt to interpret *text* as a bare type-expression entry.

        *lexed* is the lex of *text* when a program reading already lexed it: the type
        reading parses its tokens instead of scanning the text again.
        With *display* false the result carries no ``type_display`` (only ``value_type``),
        so formatting, which spells exception bases, is never entered.

        Returns a ``kind == "type"`` :class:`EntryResult` echoing the resolved
        type when *text* parses as inner attributes and a single type expression AND
        resolves to a known type; returns ``None`` otherwise so the caller keeps the
        original failure result.

        This is a REPL-only convenience (the language is unchanged): typing a
        type is not a value expression, so without it the entry would surface
        ``'X' is not defined.``.  *text* is parsed standalone as a type entry --
        node ids from *next_id*, spans in *source* -- and wrapped
        programmatically in a synthetic ``type <fresh> = type`` declaration under the
        entry's inner attributes, run through the same entry pipeline and session
        scope as any other entry: scope is the one place that decides whether an
        owner is hidden or referenced-only, for every position, so a bare type entry
        is rejected exactly as it is in ``fn(x: text) => 1``.

        The synthetic program is the *only* resolution attempted -- never
        re-resolved against some other environment. A rejected type reading
        stands in for the value reading's failure (*value_failure*) only when
        *text* is no value expression at all (it failed to parse as one): otherwise
        the value reading's verdict is the entry's, except for a rejection of the
        inner attributes themselves, which the value reading may have reached only
        after an earlier error. A bare unapplied generic (e.g. ``Option``) cannot be
        an alias body either, but its selected declaration is already known -- the
        rejection of the alias carries it directly -- so its definition is displayed
        from there instead, but only when the unapplied reference *is* the whole
        entry (its span equals the type's): a generic nested inside the entry (e.g. a
        type argument, as in ``Box[Option]``) is not what was asked for.

        Like :meth:`type_of`, this never evaluates, promotes, advances the
        node-id counter, or mutates session state: the synthetic declaration
        is discarded once its resolved type (or generic definition) is read.
        """
        from agnette.agl.lexer import lex_replay, spaced_qualifier_collector
        from agnette.agl.parser import AglSyntaxError, parse_type_entry_seeded
        from agnette.agl.repl.type_display import (
            format_generic_type_def_for_repl,
            format_type_for_repl,
        )
        from agnette.agl.semantics.types import ExceptionType
        from agnette.agl.typecheck import UnappliedGenericTypeError

        def parse() -> tuple[
            tuple["Attribute", ...], "TypeExpr", int, tuple["SpacedQualifier", ...]
        ]:
            with lex_replay(lexed), spaced_qualifier_collector() as spaced_sink:
                attributes, expr, ids = parse_type_entry_seeded(
                    text, start_id=next_id, source=source
                )
            return attributes, expr, ids, tuple(spaced_sink)

        parsed = _static_verdict(parse)
        if isinstance(parsed, AglError):
            return None
        attributes, type_expr, next_node_id, spaced_qualifiers = parsed
        probe, fresh_name, next_node_id = self._type_probe_program(
            type_expr, next_node_id, attributes
        )
        resolved = _static_verdict(
            lambda: self._entry_pipeline.resolve_program(
                probe, next_node_id, source_text=text, spaced_qualifiers=spaced_qualifiers
            )
        )
        checked = (
            resolved
            if isinstance(resolved, AglError)
            else _static_verdict(lambda: self._entry_pipeline.check_resolved_program(resolved))
        )
        if isinstance(checked, AglError):
            header_end = max((attribute.span.end_offset for attribute in attributes), default=0)
            if isinstance(checked, UnappliedGenericTypeError) and checked.span == type_expr.span:
                assert not isinstance(resolved, AglError)
                return self._type_entry_result(
                    probe,
                    resolved,
                    type_display=format_generic_type_def_for_repl(
                        checked.display_name,
                        checked.generic_def,
                        checked.type_table,
                        self._base_spelling,
                    )
                    if display
                    else None,
                )
            if checked.span is not None and checked.span.end_offset <= header_end:
                return self._fail_static(checked, [])
            return (
                self._fail_static(checked, [])
                if isinstance(value_failure, AglSyntaxError)
                else None
            )

        assert not isinstance(resolved, AglError)
        module = checked.modules[checked.entry_id]
        value_type = checked.program_type_table[(checked.entry_id, (), fresh_name)]
        table = module.type_env.type_table
        return self._type_entry_result(
            probe,
            resolved,
            value_type=value_type,
            type_display=(
                format_type_for_repl(value_type, table, self._base_spelling)
                if display and isinstance(value_type, ExceptionType)
                else None
            ),
            type_table=table,
        )

    def _base_spelling(self, base: "ExceptionType") -> str:
        """The bare name, read at the entry level, of exception *base*: ``extends`` takes no other.

        Its declared name where that names it here, else a top-level alias denoting it,
        each confirmed by one display-free probe; only an alias denoting *base* is probed.
        When nothing does, the declared name.
        """
        from agnette.agl.syntax.spans import SourceId

        names = (
            base.name,
            *(name for name in self._top_level_aliases() if self._denotes(name, base)),
        )
        for name in names:
            named = self._try_type_entry(
                name,
                next_id=self._next_node_id,
                value_failure=None,
                source=SourceId("<repl probe>"),
                display=False,
            )
            if named is not None and named.value_type == base:
                return name
        return base.name

    def _top_level_aliases(self) -> list[str]:
        """The names of the session's top-level type aliases."""
        return [
            path[0]
            for path, owner in self._session_type_paths.items()
            if len(path) == 1 and owner.alias is not None
        ]

    def _denotes(self, alias: str, base: "ExceptionType") -> bool:
        """Whether session alias *alias* is, as declared, exactly the exception *base*.

        Read from the session type environment, which keeps the type each alias was
        checked against, so a redeclared target is not mistaken for the one it names.
        """
        from agnette.agl.modules.ids import ENTRY_ID

        template = self._type_env.declared_type_template(ENTRY_ID, alias)
        return not template.type_params and template.template == base

    def _type_entry_result(
        self,
        probe: "Program",
        resolved: "ResolvedProgram",
        *,
        value_type: "Type | None" = None,
        type_display: str | None = None,
        type_table: "TypeTable | None" = None,
    ) -> EntryResult:
        """The ``kind == "type"`` result of a *probe* program, or the rejection of its warnings."""
        outcome = self._govern_warnings(
            unused_module_doc_warnings(probe), resolved.modules.values(), scope_complete=True
        )
        if outcome.denied:
            return self._fail(list(outcome.denied), list(outcome.reported))
        return EntryResult(
            kind="type",
            name=None,
            value=None,
            value_type=value_type,
            type_display=type_display,
            diagnostics=[],
            warnings=list(outcome.reported),
            error=None,
            ok=True,
            type_table=type_table,
        )

    @staticmethod
    def _type_probe_program(
        type_expr: "TypeExpr", start_id: int, attributes: tuple["Attribute", ...]
    ) -> tuple["Program", str, int]:
        """Wrap *type_expr* as the target of a fresh ``type <name> = ...`` program.

        A bare type has no program-level syntax of its own, so it is probed as
        a fresh alias's target -- the same position ``type X = <type-expr>``
        resolves through. The program carries the entry's inner *attributes*.
        Returns the program, the fresh alias name, and the next free node id.
        """
        from agnette.agl.syntax.nodes import Block, Program, TypeAlias

        fresh_name = f"ReplTypeEntry{uuid.uuid4().hex}"
        span = type_expr.span
        alias = TypeAlias(name=fresh_name, type_expr=type_expr, span=span, node_id=start_id)
        block = Block(items=(alias,), span=span, node_id=start_id + 1)
        program = Program(body=block, span=span, node_id=start_id + 2, attributes=attributes)
        return program, fresh_name, start_id + 3

    def _eval_entry_pipeline(
        self, text: str, *, check_only: bool, source: "SourceId", lexed: "LexedSource | None"
    ) -> tuple[EntryResult, int, "LexedSource | None"]:
        """Run the resolve → typecheck → matchcompile → lower/eval entry core.

        Completed runtime initializers are promoted even when a later initializer
        fails. Also returns the first node id past the entry's parse and its lexed source
        (``None`` if it did not lex), which is *lexed* when that records *text*. The entry
        is parsed in *source*.
        """
        from agnette.agl.lexer import (
            lex_replay,
            lex_source,
            spaced_qualifier_collector,
            tab_warning_collector,
        )
        from agnette.agl.parser import parse_program_seeded

        host_env = self._runtime.host_environment()
        if lexed is None or lexed.text != text:
            lexed = lex_source(text)
        lexed_ok = lexed if lexed.failure is None else None

        # TAB advisories come from the parse's single lex pass (no separate TAB
        # scan).  The collector is populated even on a failed parse; the warning
        # policy then filters them (and withholds them if the parse failed).
        # [1] Parse (seeded so node ids stay globally unique across entries).
        with (
            tab_warning_collector() as tab_sink,
            spaced_qualifier_collector() as spaced_sink,
            lex_replay(lexed),
        ):
            try:
                program, next_start_id = parse_program_seeded(
                    text, start_id=self._next_node_id, source=source
                )
            except AglError as exc:
                return (
                    self._fail_static_governed(exc, list(tab_sink)),
                    self._next_node_id,
                    lexed_ok,
                )
        tab_warnings: list[Diagnostic] = list(tab_sink)
        spaced_qualifiers = tuple(spaced_sink)
        # [1d] REPL entries use the program pipeline by default because that
        # is where the synthetic ``import std/prelude`` prelude is injected.  This
        # keeps the REPL aligned with ``agm exec``: stdlib names are bare unless
        # a host explicitly opts out.
        result = self._entry_pipeline.eval_entry(
            text=text,
            orig_program=program,
            pipeline_program=program,
            host_env=host_env,
            tab_warnings=tab_warnings,
            next_start_id=next_start_id,
            check_only=check_only,
            spaced_qualifiers=spaced_qualifiers,
        )
        return result, next_start_id, lexed_ok

    def _build_check_only_result(
        self,
        program: "Program",
        checked: "CheckedModule",
        warnings: list[Diagnostic],
    ) -> EntryResult:
        """Build the EntryResult for a ``check_only`` (type-only) run.

        No value, no evaluation, no promotion, no trace.  The value_type for an
        expression entry is the checked node type of the expression; for a binding
        it is the declared binding type.
        """
        kind, name = self._classify(checked.resolved.program)
        return EntryResult(
            kind=kind,
            name=name,
            value=None,
            value_type=self._value_type_of_last(checked),
            diagnostics=[],
            warnings=warnings,
            error=None,
            ok=True,
            quote_strings=self._quote_strings_for_entry(program),
            type_table=checked.type_env.type_table,
        )

    def _fail(
        self,
        diagnostics: list[Diagnostic],
        warnings: list[Diagnostic],
        *,
        failure: AglError | None = None,
    ) -> EntryResult:
        """Build a clean pre-execution failure result (no promotion).

        *failure* is the static ``AglError`` that produced *diagnostics*, when
        one exists (see ``EntryResult.failure``).
        """
        return EntryResult(
            kind="statement",
            name=None,
            value=None,
            value_type=None,
            diagnostics=diagnostics,
            warnings=warnings,
            error=None,
            ok=False,
            failure=failure,
        )

    def _govern_warnings(
        self,
        warnings: "Iterable[Diagnostic]",
        modules: "Iterable[ResolvedModule]",
        *,
        scope_complete: bool,
    ) -> "WarningOutcome":
        """Apply the session's warning policy to one entry's raw *warnings*.

        *modules* are those scope resolved for the entry, whichever pass then failed;
        *scope_complete* is whether it resolved the whole program.
        The one call site of the policy in the session; the dependency cap reads the
        session's roots (none yet if no entry has loaded a module).
        """
        return apply_program_policy(
            (w for w in warnings if not self._inherits_allow(w)),
            modules,
            self._warning_policy,
            scope_complete=scope_complete,
            roots=self._roots,
        )

    def _inherits_allow(self, warning: Diagnostic) -> bool:
        """Whether a loaded file's leading ``@!allow`` silences *warning*: only its own source's."""
        return warning.warning_name in self._inherited_allows and warning.source_label in (
            None,
            self._entry_label,
        )

    def _fail_static_governed(
        self,
        exc: AglError,
        warnings: list[Diagnostic],
        modules: "Iterable[ResolvedModule]" = (),
    ) -> EntryResult:
        """Build the :meth:`_fail_static` result with the policy applied to *warnings*.

        A denied warning follows *exc*'s diagnostic as an error; *modules* are those
        scope resolved before the failure (none if it never ran).
        """
        outcome = self._govern_warnings(warnings, modules, scope_complete=False)
        return self._fail(
            [exc.to_diagnostic(), *outcome.denied], list(outcome.reported), failure=exc
        )

    def _fail_static(self, exc: AglError, warnings: list[Diagnostic]) -> EntryResult:
        """Build a failure result whose single diagnostic is derived from *exc* itself.

        The common case at every static-rejection site that raises one
        ``AglError`` and reports exactly its own diagnostic: see
        ``EntryResult.failure``.
        """
        return self._fail([exc.to_diagnostic()], warnings, failure=exc)

    @property
    def _default_strict_json(self) -> bool:
        """The live strict-json setting: the current register, else the floor.

        A read-only view onto :attr:`_current` rather than a separately
        stored copy: the register is absent exactly when neither a host seed
        nor a declared default has been established yet (construction with
        neither, or an entry that has not touched ``std/config``), which is
        precisely when the floor (:attr:`_strict_json_floor`) is the right
        answer.
        """
        seed = self._current.get("strict-json")
        if seed is None:
            return self._strict_json_floor
        return cast("BoolValue", seed).value

    @staticmethod
    def _resolve_timeout_seconds(seed: "RecordValue | None") -> float | None:
        """Unwrap a ``timeout`` register value (``Option[text]``) into seconds, or ``None``.

        ``None`` covers both an absent register and an explicit ``None``
        variant (a host or declared "no timeout" control). ``seed`` always
        carries the reserved fallback ``Option`` identity (see
        :meth:`_engine_snapshot`), so no program-specific nominal table is
        needed to recognize its variant.
        """
        from agnette.agl.ir.builtin_nominals import NO_BUILTIN_DECLARATIONS
        from agnette.agl.runtime.option import option_text
        from agnette.core.parse import parse_timeout

        raw = None if seed is None else option_text(seed, nominals=NO_BUILTIN_DECLARATIONS)
        return None if raw is None else parse_timeout(raw)

    def _seeded_timeout_seconds(self) -> float | None:
        """Return the shell-exec timeout the unified seed map implies, in seconds.

        Reads ``_engine_seed`` directly -- a host control set at construction,
        or (once :meth:`_record_declared_engine_defaults` has run) a learned
        ``std/config`` declared default; a host control always wins. Shared by
        construction and :meth:`reset`; a completed entry instead overwrites
        the field with the interpreter's own already-parsed value
        (:meth:`_update_engine_settings`).
        """
        return self._resolve_timeout_seconds(
            cast("RecordValue | None", self._engine_seed.get("timeout"))
        )

    @staticmethod
    def _engine_snapshot(interp: "IrInterpreter") -> "dict[str, Value]":
        """Return *interp*'s current effective value for every engine key, as a ``Value``.

        Shared by :meth:`_record_declared_engine_defaults` (called before
        ``interp.run()``, so this reflects the interpreter's bootstrap state)
        and :meth:`_update_engine_settings` (called after, so this reflects
        whatever the entry wrote too) -- the one place that converts the
        interpreter's own native-typed fields into the ``Value`` shape the
        seed and current-value maps share.

        ``interp.builtin_host_settings``/``interp.timeout_setting`` already
        carry the reserved fallback identity for any ``Option``/``Agent``-backed
        key: these maps outlive *interp*'s own compiled program -- across
        entries, across :meth:`reset` -- so the REPL never needs that
        program's own nominal table to recognize such a value.
        """
        from agnette.agl.semantics.values import BoolValue

        return {
            **interp.builtin_host_settings,
            "strict-json": BoolValue(interp.strict_json),
            "timeout": interp.timeout_setting,
        }

    def _record_declared_engine_defaults(
        self, declared_keys: frozenset[str], interp: "IrInterpreter"
    ) -> None:
        """Remember unseeded declared defaults before an entry can write them.

        The REPL creates a fresh interpreter for each entry.  Its constructor
        is the one place that evaluates a ``builtin var`` initializer, so the
        entry pipeline calls this with *interp* right after construction,
        before ``interp.run()``.  A key already in ``_engine_seed`` (a host
        control, or a declared default a prior entry already recorded) is left
        untouched, so a declared default is recorded at most once and never
        outranks a host control -- the same precedence :meth:`reset` reapplies
        by reading ``_engine_seed`` directly.
        """
        if not declared_keys:
            return
        snapshot = self._engine_snapshot(interp)
        for key in declared_keys:
            if key not in self._engine_seed:
                self._engine_seed[key] = snapshot[key]

    def _update_engine_settings(self, interp: "IrInterpreter") -> None:
        """Persist the engine settings an entry ended with into the current-value map.

        Setting writes are non-transactional, so a failed or cancelled entry
        persists the writes it completed. The host-consumed keys are
        replaced unconditionally from ``interp.builtin_host_settings`` --
        already the session's own prior register re-merged with any fresh
        declared default this entry's own compilation introduced (see
        ``IrInterpreter.__init__``), so it is always the correct next value,
        and a key genuinely never seeded or declared (``default-agent`` has no
        host-side floor) is correctly left absent rather than fabricated.
        Timeout is likewise replaced unconditionally, since it always has a
        host-side floor and so is always present. Strict-json is replaced only
        once its register is already meaningful (an established current
        value, or a seed recorded by :meth:`_record_declared_engine_defaults`
        earlier in this same entry): otherwise ``interp.strict_json`` may be
        nothing more than the floor, and recording it as a register would
        wrongly stop a LATER entry that is the first to declare
        ``std/config`` from ever discovering its real default.
        Shell-exec-timeout is backed by a scalar field rather than the
        current-value map, so it is set straight from the interpreter's own
        already-resolved value rather than re-derived here.
        """
        snapshot = self._engine_snapshot(interp)
        self._current.update(interp.builtin_host_settings)
        self._current["timeout"] = snapshot["timeout"]
        if "strict-json" in self._current or "strict-json" in self._engine_seed:
            self._current["strict-json"] = snapshot["strict-json"]
        self._shell_exec_timeout = interp.shell_exec_timeout
        self._builtin_var_values = interp.builtin_vars

    def _advance_node_ids(self, next_start_id: int) -> None:
        """Consume node ids for an entry that failed after lowering began."""
        self._next_node_id = next_start_id

    def _promote_ir_state(
        self,
        *,
        text: str,
        program: "Program",
        checked: "CheckedModule",
        next_start_id: int,
        partial: bool,
        promoted_declaration_ids: frozenset[int],
        promoted_scope_region_paths: frozenset[tuple[str, ...]],
        promoted_use_declaration_ids: frozenset[int],
        retired_scopes: frozenset[tuple[str, ...]],
        entry_module_id: ModuleId,
    ) -> tuple[str, ...]:
        """Promote declarations whose IR initialization completed in this entry."""
        from agnette.agl.scope.symbols import ScopeNode
        from agnette.agl.scope.type_owners import beneath
        from agnette.agl.syntax.nodes import (
            EnumDef,
            ExceptionDef,
            FuncDef,
            InfixDecl,
            LetDecl,
            RecordDef,
            TypeAlias,
            VarDecl,
            VariantDef,
            static_items,
        )
        from agnette.agl.typecheck.declaration_validation import (
            bare_declaration_scoped_name,
            is_builtin_bare_declaration,
        )
        from agnette.agl.typecheck.env import TypeEnvironment, assert_checked_module_closed

        entry_declarations = tuple(static_items(program.body.items))
        if self_validation_enabled():
            assert_checked_module_closed(checked)
        entry_root = checked.resolved.root_scope
        named_declarations = (
            EnumDef,
            ExceptionDef,
            FuncDef,
            LetDecl,
            RecordDef,
            TypeAlias,
            VarDecl,
        )
        binding_items = (FuncDef, LetDecl, VarDecl)
        promotion_bindings = {
            item.name: entry_root.bindings[item.name]
            for item in program.body.items
            if isinstance(item, binding_items)
            and not item.scope_path
            and item.name in entry_root.bindings
        }
        entry_binding_node_ids = {ref.decl_node_id for ref in promotion_bindings.values()}
        promoted_binding_node_ids = entry_binding_node_ids & promoted_declaration_ids

        # Declarations this entry introduces, at the root and in its scope
        # regions. Anything outside this set is retained session state, which a
        # partial entry never demotes.
        entry_declaration_node_ids = {
            item.node_id for item in entry_declarations if isinstance(item, named_declarations)
        } | entry_binding_node_ids

        def _is_promoted(node_id: int) -> bool:
            return node_id not in entry_declaration_node_ids or node_id in promoted_declaration_ids

        def type_name_path(
            item: RecordDef | EnumDef | ExceptionDef | TypeAlias,
        ) -> tuple[tuple[str, ...], str]:
            return tuple(segment.name for segment in item.scope_path), item.name

        entry_type_items = tuple(
            item
            for item in entry_declarations
            if isinstance(item, (RecordDef, EnumDef, ExceptionDef, TypeAlias))
        )
        entry_type_name_paths = frozenset(type_name_path(item) for item in entry_type_items)
        promoted_type_name_paths = frozenset(
            type_name_path(item)
            for item in entry_type_items
            if item.node_id in promoted_declaration_ids
        )
        replaced_type_name_paths = promoted_type_name_paths if partial else entry_type_name_paths
        replaced_type_paths = frozenset((*path, name) for path, name in replaced_type_name_paths)
        # ``retired_scopes`` (from the checked program's resolution) reflects
        # every type path this entry's own module redeclares, not only the
        # ones this promotion actually promotes: a partial entry must retire
        # only the member scopes of a type path it actually replaced, or it
        # wrongly retires a redeclaration's scopes it never promoted.
        retired_scopes = frozenset(
            scope for scope in retired_scopes if scope[:-1] in replaced_type_paths
        )
        unpromoted_type_name_paths = entry_type_name_paths - promoted_type_name_paths
        unpromoted_type_scope_paths = frozenset(
            (*path, name) for path, name in unpromoted_type_name_paths
        )

        unpromoted_type_names = {
            "::".join((*path, name)) for path, name in unpromoted_type_name_paths
        }
        unpromoted_type_names.update(
            "::".join((*tuple(segment.name for segment in item.scope_path), item.name, member.name))
            for item in entry_type_items
            if isinstance(item, EnumDef) and item.node_id not in promoted_declaration_ids
            for member in item.members
            if isinstance(member, VariantDef)
        )
        # Redeclaring an enum clears every prior inline member from the fresh
        # environment before it registers the new members. Roll back the full
        # type-owned subtree, not only names the failed declaration introduced,
        # so an earlier ``Tree::Leaf`` remains a resolvable type after a failed
        # ``Tree`` redeclaration that declares only ``Tree::Node``.
        unpromoted_type_names.update(
            name
            for name in self._type_env.all_declared_type_names()
            if beneath(tuple(name.split("::")), unpromoted_type_scope_paths)
        )
        # Every replacement of an enum owner, including replacement by a
        # record or alias, retires its prior inline member scopes. Nested
        # standalone declarations remain and are copied back below.
        for scope_path in tuple(self._session_scope_nodes):
            if beneath(scope_path, retired_scopes):
                del self._session_scope_nodes[scope_path]
        for type_path in tuple(self._session_type_paths):
            if beneath(type_path, retired_scopes):
                del self._session_type_paths[type_path]
        for scoped_name in tuple(self._session_builtin_declarations):
            if beneath(scoped_name, retired_scopes):
                del self._session_builtin_declarations[scoped_name]
        # A promoted declaration at a scoped name always supersedes whatever
        # this session recorded there, builtin or not: drop the old entry
        # first, then re-record it only when the fresh declaration is itself
        # a ``builtin`` one.
        for item in entry_declarations:
            if item.node_id not in promoted_declaration_ids:
                continue
            declared_name = bare_declaration_scoped_name(item)
            if declared_name is None:
                continue
            self._session_builtin_declarations.pop(declared_name, None)
            if is_builtin_bare_declaration(item):
                self._session_builtin_declarations[declared_name] = (
                    entry_module_id,
                    declared_name[:-1],
                )

        for name, ref in promotion_bindings.items():
            if ref.decl_node_id not in promoted_binding_node_ids:
                continue
            self._session_scope.bindings[name] = ref
        installed = (
            self._installed_report(
                program,
                promoted_binding_node_ids=promoted_binding_node_ids,
                promoted_type_names=frozenset(
                    name for path, name in promoted_type_name_paths if not path
                ),
            )
            if partial
            else []
        )

        # Keep only namespace paths reached by execution or required by a
        # promoted declaration. A later empty region must not leak through a
        # failed entry merely because scope checking saw it.
        required_scope_paths = set(promoted_scope_region_paths)
        for item in entry_declarations:
            if item.node_id not in promoted_declaration_ids or not isinstance(
                item, (EnumDef, ExceptionDef, FuncDef, LetDecl, RecordDef, TypeAlias, VarDecl)
            ):
                continue
            item_path = tuple(segment.name for segment in item.scope_path)
            if isinstance(item, (EnumDef, ExceptionDef, RecordDef, TypeAlias)):
                item_path = (*item_path, item.name)
            required_scope_paths.update(
                item_path[:length] for length in range(1, len(item_path) + 1)
            )
        for path, node in checked.resolved.scope_nodes.items():
            for name, ref in node.members.items():
                if not _is_promoted(ref.decl_node_id):
                    continue
                member_path = (
                    (*path, name) if (*path, name) in checked.resolved.scope_nodes else path
                )
                required_scope_paths.update(
                    member_path[:length] for length in range(1, len(member_path) + 1)
                )
        # ``required_scope_paths`` is prefix-closed: its seed,
        # ``promoted_scope_region_paths``, is prefix-closed by construction
        # (`collect_regions` in ``lower/repl.py`` reaches every path above a
        # region's scope path where it reaches that), and both loops above
        # add every ancestor of the paths they contribute, so a retained
        # path's parent is always retained first.
        for path, node in checked.resolved.scope_nodes.items():
            if (
                path in self._session_scope_nodes
                or not path
                or path not in required_scope_paths
                or beneath(path, unpromoted_type_scope_paths)
                or beneath(path, retired_scopes)
            ):
                continue
            self._session_scope_nodes[path] = ScopeNode(
                node_id=node.node_id,
                parent=self._session_scope_nodes[path[:-1]],
                scope_path=path,
            )

        promoted_type_paths = {(*path, name) for path, name in promoted_type_name_paths}
        for path in promoted_type_paths:
            nested_scope_names = frozenset(
                nested_path[-1]
                for nested_path in self._session_scope_nodes
                if nested_path[:-1] == path
            )
            if path in self._session_scope_nodes:
                self._session_scope_nodes[path].clear_owned_constructor_members(nested_scope_names)

        for path, node in checked.resolved.scope_nodes.items():
            session_node = self._session_scope_nodes.get(path)
            if session_node is None:
                continue
            for name, ref in node.members.items():
                if (
                    not beneath(ref.scope_path, unpromoted_type_scope_paths)
                    and not beneath((*path, name), retired_scopes)
                    and _is_promoted(ref.decl_node_id)
                ):
                    session_node.register_member(name, ref)
            # A promoted use this entry writes replaces the retained ones scope decided.
            replaced = checked.resolved.replaced_uses
            session_node.uses = [
                use
                for use in session_node.uses
                if not replaced.get(use.node_id, frozenset()) & promoted_use_declaration_ids
            ] + [use for use in node.uses if use.node_id in promoted_use_declaration_ids]
        self._session_type_paths.update(
            (type_path, checked.resolved.type_owners[type_path])
            for type_path in promoted_type_paths
        )
        attributes = checked.resolved.attributes
        self._session_docs.update(
            (node_id, doc) for node_id, doc in attributes.docs.items() if _is_promoted(node_id)
        )
        self._prune_session_docs()
        for region in promoted_scope_region_paths:
            self._session_regions[region] = attributes.region_docs.get(
                region, self._session_regions.get(region)
            )

        if not partial:
            # The checked environment already includes the prior sealed session
            # state and is itself sealed at the checked-output boundary -- it
            # was seeded with the same ``retired_scopes`` this promotion
            # received, so a redeclared enum owner's retired inline members are
            # already gone from its type namespace. Reuse it directly instead
            # of copying the accumulated session a second time.
            self._type_env = checked.type_env
        else:
            # Build the replacement env in a local so a mid-promotion failure
            # leaves the session's still-sealed ``self._type_env`` untouched.
            # It carries its own fresh type table, so the rewind below reads
            # the session's accumulated state directly without a copy standing
            # between them.
            new_type_env = TypeEnvironment()
            new_type_env.seed_from(checked.type_env)
            new_type_env.rewind_from(
                self._type_env,
                type_names=unpromoted_type_names,
                binding_node_ids=entry_binding_node_ids - promoted_binding_node_ids,
                functions={
                    item.node_id: item.name
                    for item in entry_declarations
                    if isinstance(item, FuncDef) and item.node_id not in promoted_declaration_ids
                },
            )
            new_type_env.seal()
            self._type_env = new_type_env

        if not partial:
            self._source_log.append((text, self._inherited_allows))
        self._accumulated_infix.update(
            (item.name, checked.resolved.fixities[item.name])
            for item in program.body.items
            if isinstance(item, InfixDecl) and item.node_id in promoted_declaration_ids
        )
        self._next_node_id = next_start_id
        return tuple(installed)

    @staticmethod
    def _installed_report(
        program: "Program",
        *,
        promoted_binding_node_ids: set[int],
        promoted_type_names: frozenset[str],
    ) -> list[str]:
        """Return every promoted declaration name for a partial entry's report.

        Ordered by the entry's source items: a promoted value binding (function /
        var) or a promoted type declaration (record / enum /
        exception / type alias) contributes its declared name. An enum's
        variant names are never listed separately, only the enum's own declared name.
        """
        from agnette.agl.syntax.nodes import (
            EnumDef,
            ExceptionDef,
            FuncDef,
            LetDecl,
            RecordDef,
            TypeAlias,
            VarDecl,
        )

        binding_items = (FuncDef, LetDecl, VarDecl)
        type_items = (RecordDef, EnumDef, ExceptionDef, TypeAlias)
        installed: list[str] = []
        for item in program.body.items:
            if isinstance(item, binding_items):
                if item.node_id in promoted_binding_node_ids:
                    installed.append(item.name)
            elif isinstance(item, type_items) and item.name in promoted_type_names:
                installed.append(item.name)
        return installed

    def frame_value(self, symbol: "SymbolId | None") -> "Value | None":
        """Return the base-frame value bound to *symbol*, unwrapping a mutable cell."""
        from agnette.agl.semantics.values import Cell

        slot = self._ir_base_frame.get(symbol) if symbol is not None else None
        return slot.value if isinstance(slot, Cell) else slot

    def _declaration_value(self, decl_node_id: int) -> "Value | None":
        """Return the base-frame value of a promoted declaration."""
        return self.frame_value(self._link_image.symbol_for_decl(decl_node_id))

    def _echo_data_ir(
        self, program: "Program", checked: "CheckedModule", captured: "Value | None"
    ) -> tuple["Value | None", "Type | None"]:
        from agnette.agl.syntax.nodes import (
            Binder,
            Declaration,
            LetDecl,
            VarDecl,
        )

        if not program.body.items:
            return captured, None
        last = program.body.items[-1]
        value_type = self._value_type_of_last(checked)
        if not isinstance(last, (Binder, Declaration)):
            return captured, value_type
        if isinstance(last, LetDecl):
            if last.name == "_":
                return captured, value_type
            return self._declaration_value(last.node_id), value_type
        if isinstance(last, VarDecl):
            return self._declaration_value(last.node_id), value_type
        return None, None

    def _classify(self, program: "Program") -> tuple[EntryKind, str | None]:
        """Classify the entry by its last item; return (kind, name)."""
        from agnette.agl.modules.ids import spell_scope_path
        from agnette.agl.syntax.nodes import (
            AssignStmt,
            Binder,
            Declaration,
            EnumDef,
            ExceptionDef,
            FuncDef,
            LetDecl,
            RecordDef,
            ScopeRegion,
            TypeAlias,
            VarDecl,
            is_scoped_declaration,
            scoped_public_name,
        )

        if not program.body.items:
            return "statement", None
        last = program.body.items[-1]
        # Bare expression (not a binder or declaration) → "expression"
        if not isinstance(last, (Binder, Declaration, ScopeRegion)):
            return "expression", None
        if isinstance(last, LetDecl):
            if last.name == "_":
                return "statement", None
            # A shorthand binder path names the member it declares, so the
            # echo shows the member the way its scope makes it reachable.
            return "binding", scoped_public_name(last.scope_path, last.name)
        if isinstance(last, VarDecl):
            if last.name == "_":
                return "statement", None
            return "binding", scoped_public_name(last.scope_path, last.name)
        if isinstance(
            last,
            (
                RecordDef,
                EnumDef,
                ExceptionDef,
                TypeAlias,
                FuncDef,
            ),
        ):
            # A shorthand declaration path names the member it declares, so the
            # echo shows the member the way its scope makes it reachable.
            if is_scoped_declaration(last):
                return "declaration", scoped_public_name(last.scope_path, last.name)
            return "declaration", last.name
        if isinstance(last, ScopeRegion):
            return "declaration", spell_scope_path(_region_path(last))
        # AssignStmt → "statement"
        if isinstance(last, AssignStmt):
            return "statement", None
        # Import, export, use, infix, and builtin declarations name nothing the
        # echo can confirm, so they read as statements.
        return "statement", None

    def _quote_strings_for_entry(self, program: "Program") -> bool:
        """Return the top-level text quoting mode for REPL echo.

        Only a syntactically standalone ``ask`` builtin call gets unquoted text
        display. Stored ask results, variables, bindings, and all other
        expressions use normal REPL value display.
        """
        from agnette.agl.syntax.nodes import Call, VarRef

        last = program.body.items[-1] if program.body.items else None
        if isinstance(last, Call) and isinstance(last.callee, VarRef):
            return last.callee.name != "ask"
        return True

    def _value_type_of_last(self, checked: "CheckedModule") -> "Type | None":
        """Static type carried by the entry's final value, or ``None``.

        A bare expression retains its checked type. A trailing ``let``/``var``
        reports the declared binding type for the REPL declaration echo. Only
        called after checking; ``check_only`` retains a diverging initializer's
        bottom type without evaluating it.
        """
        from agnette.agl.syntax.nodes import (
            Binder,
            Declaration,
            LetDecl,
            VarDecl,
        )

        # The resolved program holds the entry with its operator chains grouped.
        items = checked.resolved.program.body.items
        if not items:
            return None
        last = items[-1]
        # A statement item (a scope region, an import, ...) has no type.
        if not isinstance(last, (Binder, Declaration)):
            return checked.node_types.get(last.node_id)
        if isinstance(last, (LetDecl, VarDecl)):
            from agnette.agl.semantics.types import BottomType

            initializer_type = checked.node_types[last.value.node_id]
            if isinstance(initializer_type, BottomType):
                return initializer_type
            if last.name == "_":
                return None
            return checked.type_env.get_binding_type(last.node_id)
        return None

    # ------------------------------------------------------------------
    # type_of — type without evaluation
    # ------------------------------------------------------------------

    def type_of(self, text: str, *, warnings: list[Diagnostic] | None = None) -> str:
        """Return the canonical display type of *text* as an expression entry.

        A module-level attribute is governed like an entry's: ``unused-module-doc`` is
        appended to *warnings*, or raised as a :class:`DiagnosedError` when the policy denies it.

        Resolves against the session scope, typechecks against the session
        environment, and match-compiles before reporting the type. It never
        lowers, evaluates, promotes, or advances the node-id counter. Raises the
        underlying ``AglSyntaxError``/``AglScopeError``/``AglTypeError`` on
        failure, or ``AglError`` for match errors or a non-expression entry.

        Parses *text* as a throwaway REPL entry, seeded at the session's
        node-id counter: this never promotes or advances the session
        counter, so seeding at ``_next_node_id`` is safe -- all promoted ids
        are strictly below it, making this parse's ids disjoint from the
        session's.
        """
        from agnette.agl.lexer import spaced_qualifier_collector
        from agnette.agl.parser import parse_program_seeded
        from agnette.agl.syntax.nodes import Binder, Declaration, ScopeRegion

        with spaced_qualifier_collector() as spaced_sink:
            program, next_node_id = parse_program_seeded(text, start_id=self._next_node_id)
        spaced_qualifiers = tuple(spaced_sink)
        items = program.body.items
        if len(items) != 1 or isinstance(items[0], (Binder, Declaration, ScopeRegion)):
            raise AglError(
                "':type' expects a single expression, not a binding, declaration, or statement."
            )
        resolved_program, checked_program = self._entry_pipeline.resolve_and_check_program(
            program, next_node_id, source_text=text, spaced_qualifiers=spaced_qualifiers
        )
        checked = checked_program.modules[checked_program.entry_id]
        from agnette.agl.matchcompile import (
            cached_module_sites,
            compile_program_matches,
            match_issue_error,
        )

        match_result = compile_program_matches(
            checked_program, cached_module_sites(self._last_match_compilation)
        )
        if match_result.compiled is None:
            raise match_issue_error(match_result.issues[0], resolved_program.speller)
        # The resolved program holds the expression with its operator chains grouped.
        typ = checked.node_types[checked.resolved.program.body.items[-1].node_id]
        from agnette.agl.repl.type_display import format_type_for_repl

        outcome = self._govern_warnings(
            unused_module_doc_warnings(program),
            resolved_program.modules.values(),
            scope_complete=True,
        )
        if outcome.denied:
            raise DiagnosedError(outcome.denied)
        self._open_failure = ()
        shown = format_type_for_repl(typ, checked.type_env.type_table, self._base_spelling)
        if warnings is not None:
            warnings.extend(outcome.reported)
        return shown

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def descriptors(self) -> "ValueDescriptors":
        """Return the descriptor view for rendering a live value from this session.

        Reflects every declaration promoted so far -- the cumulative tables a
        fresh entry's own ``ExecutableProgram`` carries too -- so it renders a
        value correctly regardless of which entry produced it (used by
        ``:bindings`` and any other introspection over already-promoted state).
        """
        return self._link_image.descriptors()

    def bindings(self) -> list[tuple[str, "Type", "Value"]]:
        """Return promoted user bindings as (name, declared type, current value)."""
        from agnette.agl.semantics.values import Cell

        result: list[tuple[str, Type, Value]] = []
        for name, ref in self._session_scope.bindings.items():
            # Every promoted let/var binding has a recorded type, symbol, and slot.
            typ = self._type_env.binding_type_of(ref.decl_node_id)
            symbol = self._link_image.symbol_of(ref.decl_node_id)
            slot = self._ir_base_frame[symbol]
            value = slot.value if isinstance(slot, Cell) else slot
            result.append((name, typ, value))
        return result

    def info_of(self, name: str) -> str | None:
        """Return a compact AgL-shaped description of one known identifier.

        This never evaluates anything or promotes an entry. It resolves names through
        the same entry pipeline as the REPL, so bare and imported qualified names
        select their actual visible declaration, and a name scope rejects raises
        scope's rejection. ``None`` when *name* is no identifier.

        Library declarations are described from a checked library image, so a session
        that has promoted nothing is :meth:`open`-ed first, as a host does; a rejected
        image raises :class:`SessionOpenError` until :meth:`reset`, another ``open``, a successful
        ``:type`` or check-only entry, or an entry promotes an image.
        """
        if self._next_node_id:
            self._open_failure = ()
        else:
            if not self._open_failure:
                self.open()
            if self._open_failure:
                raise SessionOpenError(self._open_failure)
        parts = tuple(name.removeprefix("::").split("::"))
        if not name or any(not part for part in parts):
            return None
        try:
            return self._info_of_parsed(name, parts)
        finally:
            self._info_probe = None

    def _info_of_parsed(self, name: str, parts: tuple[str, ...]) -> str | None:
        """Describe identifier *name* of ``::`` *parts* (see :meth:`info_of`)."""
        if "/" in name and len(parts) == 1:
            # A lone slash route is a module before it is a declaration.
            route = tuple(name.removeprefix("/").split("/"))
            found = self._describe_scopes(name, route, anchored=name.startswith("/"))
            if found is not None:
                return found
            return self._describe_declaration(name, self._parse_info_name(name))
        parsed = self._parse_info_name(name)
        reference = parsed.reference
        chain = None if reference is None else self._qualifier_of(reference)
        try:
            described = self._describe_declaration(name, parsed)
        except AglError:
            found = None if chain is None else self._describe_scopes(name, chain)
            if found is not None:
                return found
            hidden = None if reference is None else self._hidden_region(reference)
            if hidden is not None:
                raise hidden from None
            raise
        return described

    @staticmethod
    def _qualifier_of(reference: "VarRef") -> "QualifierChain":
        """Return the qualifier chain *reference* spells, a bare name being a one-name chain."""
        from agnette.agl.syntax.nodes import QualifierChain

        return reference.qualifier or QualifierChain(
            None, (), reference.name, reference.span, reference.node_id
        )

    def _describe_declaration(self, name: str, parsed: "_InfoName") -> str | None:
        """Describe the binding, function, constructor, or type *name*, parsed, selects."""
        from agnette.agl.repl.render import _render_value_or_cyclic_message
        from agnette.agl.semantics.values import Cell

        resolved_reference = self._resolve_info_reference(name, parsed)
        ref = None if resolved_reference is None else resolved_reference.binding
        location = (
            self._repl_location(ref.decl_span)
            if ref is not None and ref.module_id.is_entry
            else None
        )
        if ref is not None and ref.kind.value != "constructor_binding":
            declared = _declared_spelling(ref.scope_path, ref.name)
            type_env = self._info_type_env(ref.module_id)
            signature = type_env.get_function_signature_by_node_id(ref.decl_node_id)
            if signature is not None:
                return "\n".join(
                    (
                        f"{name} is a function.",
                        _format_info_section(
                            "Signature",
                            f"def {declared}{_format_repl_signature(signature)}",
                            location,
                            self._declaration_doc(ref.module_id, ref.decl_node_id),
                        ),
                    )
                )
            binding_type = type_env.binding_type_of(ref.decl_node_id)
            symbol = self._link_image.symbol_for_decl(ref.decl_node_id)
            slot = self._ir_base_frame.get(symbol) if symbol is not None else None
            if slot is None:
                return f"{name} is a value.\n{_format_info_section('Type', repr(binding_type))}"
            value = slot.value if isinstance(slot, Cell) else slot
            rendered = _render_value_or_cyclic_message(
                value, self.descriptors(), quote_strings=True
            )
            keyword = "var" if ref.mutable else "let"
            return "\n".join(
                (
                    f"{name} is a {'mutable ' if ref.mutable else ''}binding.",
                    _format_info_section(
                        "Binding",
                        f"{keyword} {declared}",
                        doc=self._declaration_doc(ref.module_id, ref.decl_node_id),
                    ),
                    _format_info_section("Type", repr(binding_type)),
                    _format_info_section("Value", rendered, location),
                )
            )

        if resolved_reference is not None and resolved_reference.builtin_type:
            return self._describe_scopes(
                name, self._qualifier_of(resolved_reference.reference), builtin_type=True
            )
        if resolved_reference is not None:
            described = self._describe_type_declaration(name, resolved_reference)
            if described is not None:
                return described
        if resolved_reference is None or resolved_reference.constructor is None:
            return None
        constructor_signature = self._constructor_signature(
            resolved_reference, resolved_reference.constructor
        )
        return "\n".join(
            (
                f"{name} is a constructor.",
                _format_info_section(
                    "Signature",
                    _format_constructor_signature(constructor_signature),
                    doc=self._type_doc(constructor_signature.result_template),
                ),
            )
        )

    def _parse_info_name(self, name: str) -> _InfoName:
        """Parse *name* as an ad-hoc entry; its reference is ``None`` unless it is one name."""
        from agnette.agl.lexer import spaced_qualifier_collector
        from agnette.agl.parser import parse_program_seeded
        from agnette.agl.syntax.nodes import VarRef

        with spaced_qualifier_collector() as spaced_sink:
            program, next_node_id = parse_program_seeded(name, start_id=self._next_node_id)
        items = program.body.items
        reference = items[0] if len(items) == 1 and isinstance(items[0], VarRef) else None
        return _InfoName(program, next_node_id, tuple(spaced_sink), reference)

    def _resolve_info_reference(self, name: str, parsed: _InfoName) -> _InfoReference | None:
        """Resolve NAME, parsed, as a value and as a type with the REPL entry resolver, unchecked.

        ``None`` when NAME is not a name reference. A name neither reading
        selects raises the value reading's rejection: a value lookup that
        finds nothing already answers with the type lookup's verdict for the
        same spelling (a type name, an ambiguity, a hidden path), so it is
        scope's one decision for the name, exactly as an entry spelling it
        reports.
        """
        from agnette.agl.syntax.types import NameT

        reference = parsed.reference
        if reference is None:
            return None
        program, next_node_id = parsed.program, parsed.next_node_id
        spaced_qualifiers = parsed.spaced_qualifiers
        # The same name as a type: scope selects its declaration in the one
        # type position a bare type entry probes too.
        type_name = NameT(
            reference.name, reference.span, reference.node_id, qualifier=reference.qualifier
        )
        type_program, _fresh_name, type_next_id = self._type_probe_program(
            type_name, next_node_id, ()
        )
        resolved = _static_verdict(
            lambda: self._entry_pipeline.resolve_program(
                program, next_node_id, source_text=name, spaced_qualifiers=spaced_qualifiers
            )
        )
        type_resolved = _static_verdict(
            lambda: self._entry_pipeline.resolve_program(
                type_program, type_next_id, source_text=name, spaced_qualifiers=spaced_qualifiers
            )
        )
        type_key = (
            None
            if isinstance(type_resolved, AglError)
            else self._type_env.with_owner_declarations(
                type_resolved.modules[type_resolved.entry_id].resolved.owner_declarations
            ).type_name_declaration(type_name)
        )
        type_env = self._type_env
        builtin = (
            type_key is None
            and reference.qualifier is None
            and reference.name in NULLARY_BUILTIN_TYPE_NAMES
        )
        if isinstance(resolved, AglError):
            if type_key is None and not builtin:
                raise resolved
            binding, constructor = None, None
        else:
            entry = resolved.modules[resolved.entry_id].resolved
            binding = entry.resolution.get(reference.node_id)
            constructor = entry.constructor_refs.get(reference.node_id)
            # This ad-hoc parse's own qualifiers resolved against this fresh
            # scope pass, not the retained entry the session's type env was
            # built from, and its node ids are not reserved (this parse never
            # advances ``_next_node_id``) -- a query-scoped view carries their
            # identities (read only by ``_constructor_signature``, via
            # ``_InfoReference.type_env``) without risking a later entry's
            # colliding node ids reading them back.
            type_env = type_env.with_owner_declarations(entry.owner_declarations)
        return _InfoReference(
            binding=binding,
            constructor=constructor,
            type_key=type_key,
            reference=reference,
            type_env=type_env,
            builtin_type=builtin,
        )

    def _describe_type_declaration(self, name: str, reference: _InfoReference) -> str | None:
        """Describe the type declaration NAME selects as a type, if it reads as one.

        An alias reads as its declaration wherever it is declared; a type this
        session declares, a generic type from anywhere, and an imported plain
        type without a constructor (an enum), as its definition. Any other
        selection -- an imported plain type with a constructor -- reads as its
        value instead. An owner's inline member selects no type declaration here, so
        one reached through an applied owner reads as its instantiated
        constructor; type arguments never instantiate a declaration nested
        beneath that member, which reads as its definition.
        """
        from agnette.agl.repl.type_display import (
            format_generic_type_def_for_repl,
            format_type_def_for_repl,
        )
        from agnette.agl.semantics.types import EnumType, ExceptionType, RecordType
        from agnette.agl.syntax.types import render_type_expr

        key = reference.type_key
        if key is None:
            return None
        module_id, scope_path, decl_name = key
        declared = _declared_spelling(scope_path, decl_name)
        alias = self._alias_declaration(key)
        if alias is not None:
            params = f"[{', '.join(alias.type_params)}]" if alias.type_params else ""
            definition = f"type {declared}{params} = {render_type_expr(alias.type_expr)}"
            display = _format_info_section(
                "Type", definition, doc=self._declaration_doc(module_id, alias.node_id)
            )
            return f"{name} is a type alias.\n{display}"
        # A type this session declares is located at its declaration.
        location = (
            self._repl_location(self._session_scope_nodes[scope_path].members[decl_name].decl_span)
            if module_id.is_entry
            else None
        )
        type_env = self._info_type_env(module_id)
        described = module_id.is_entry or reference.constructor is None
        typ = type_env.get_type(declared) if described else None
        if typ is not None:
            assert isinstance(typ, (RecordType, EnumType, ExceptionType))  # declared types
            definition = format_type_def_for_repl(
                declared, typ, self._type_env.type_table, self._base_spelling
            )
            display = _format_info_section("Type", definition, location, self._type_doc(typ))
            return f"{name} is {_indefinite(typ.kind)} type.\n{display}"
        generic = type_env.get_generic_type(declared)
        if generic is None:
            return None
        definition = format_generic_type_def_for_repl(
            declared, generic, self._type_env.type_table, self._base_spelling
        )
        return "\n".join(
            (
                f"{name} is a generic {generic.kind} type.",
                _format_info_section(
                    "Type", definition, location, self._type_doc(generic.template)
                ),
            )
        )

    def _declaration_doc(self, module_id: "ModuleId", node_id: int) -> str | None:
        """Return the ``@doc`` prose of declaration *node_id* of *module_id*, if any."""
        if module_id.is_entry:
            return self._session_docs.get(node_id)
        checked = self._retained_checked_modules.get(module_id)
        return None if checked is None else checked.resolved.attributes.docs.get(node_id)

    def _type_doc(self, typ: "NominalOwner") -> str | None:
        """Return the ``@doc`` prose of the nominal declaration *typ* names, if any."""
        from agnette.agl.semantics.types import ExceptionType

        table = self._type_env.type_table
        if isinstance(typ, ExceptionType):
            return table.exception_def(typ).doc
        return table.declaration_doc(typ)

    def _describe_scopes(
        self,
        name: str,
        target: "tuple[str, ...] | QualifierChain",
        *,
        anchored: bool = False,
        builtin_type: bool = False,
    ) -> str | None:
        """Describe every module and scope region *name* reaches, or ``None`` if it reaches none.

        Scope resolves *target* (*name* as a slash route, ``anchored`` when written
        ``/a/b``, or as a qualifier chain) as an entry would: through imports, uses,
        aliases, and the session's own regions. One source is described by itself;
        several are each described under a label naming their source. A *builtin_type*
        *name* also names that type, which is one more source.
        """
        from agnette.agl.modules.ids import spell_scope_path
        from agnette.agl.scope.hiding import unremoved
        from agnette.agl.scope.lookup import spelled_origins
        from agnette.agl.scope.symbols import to_bare_path

        resolved = self._probe_resolve()
        sources = resolved.sources(resolved.entry_id)
        origins = spelled_origins(sources, target, (), anchored=anchored)
        found: list[tuple[SectionLabel, str, str]] = (
            [("Type", "builtin", f"builtin type {name}")] if builtin_type else []
        )
        for module, atom in sorted(unremoved(origins, sources), key=_qname_order):
            path = to_bare_path(atom)
            source = "this session" if module.is_entry else module.path_str()
            if not path:
                doc = self._retained_checked_modules[module].resolved.attributes.module_doc
                found.append(("Module", source, _documented_header(f"module {source}", doc)))
                continue
            known: Mapping[tuple[str, ...], str | None] = (
                self._session_regions
                if module.is_entry
                else module_regions(self._retained_checked_modules[module].resolved)
            )
            if path in known:
                header = f"scope {spell_scope_path(path)}"
                found.append(("Scope", source, _documented_header(header, known[path])))
        if not found:
            return None
        if len(found) == 1:
            label, _source, code = found[0]
            kind = "builtin type" if builtin_type else label.lower()
            return f"{name} is a {kind}.\n{_format_info_section(label, code)}"
        sections = (
            _format_info_section(label, code, source=source) for label, source, code in found
        )
        what = _count_phrase(
            [("builtin type" if label == "Type" else label.lower()) for label, _src, _code in found]
        )
        return f"{name} names {what}.\n" + "\n".join(sections)

    def _prune_session_docs(self) -> None:
        """Drop the docs of declarations the session no longer reaches (superseded or retired)."""
        live = {
            ref.decl_node_id
            for node in self._session_scope_nodes.values()
            for ref in node.members.values()
        }
        live.update(
            owner.alias.node_id
            for owner in self._session_type_paths.values()
            if owner.alias is not None
        )
        self._session_docs = {
            node_id: doc for node_id, doc in self._session_docs.items() if node_id in live
        }

    def _hidden_region(self, reference: "VarRef") -> HiddenMemberError | None:
        """Return the rejection an entry gets for a qualified *reference* naming only hidden things.

        A bare name needs no probe: describing it already reports a hidden name.
        """
        from agnette.agl.scope.lookup import lookup_hidden

        if reference.qualifier is None:
            return None
        resolved = self._probe_resolve()
        return lookup_hidden(resolved.sources(resolved.entry_id), reference.qualifier, (), None)

    def _probe_resolve(self) -> "ResolvedProgram":
        """Resolve an empty entry: the scope state an entry written now would see.

        One :meth:`info_of` query resolves it at most once; the session does not
        change while a query runs.
        """
        from agnette.agl.parser import parse_program_seeded

        if self._info_probe is None:
            program, next_node_id = parse_program_seeded("", start_id=self._next_node_id)
            self._info_probe = self._entry_pipeline.resolve_program(
                program, next_node_id, source_text=""
            )
        return self._info_probe

    def _alias_declaration(self, key: "DeclarationKey") -> "TypeAlias | None":
        """Return the alias declaration *key* identifies, if it is an alias."""
        from agnette.agl.syntax.nodes import TypeAlias, static_type_items

        module_id, scope_path, name = key
        if module_id.is_entry:
            owner = self._session_type_paths.get((*scope_path, name))
            return None if owner is None else owner.alias
        items = self._retained_resolved_modules[module_id].resolved.program.body.items
        return next(
            (
                item
                for item in static_type_items(items)
                if isinstance(item, TypeAlias)
                and item.name == name
                and tuple(segment.name for segment in item.scope_path) == scope_path
            ),
            None,
        )

    def _info_type_env(self, module_id: "ModuleId") -> "TypeEnvironment":
        """Return the retained type environment of *module_id*'s declarations."""
        checked = self._retained_checked_modules.get(module_id)
        return self._type_env if checked is None else checked.type_env

    def _constructor_signature(
        self, reference: _InfoReference, constructor: "ConstructorRef"
    ) -> "ConstructorSignature":
        """Return the signature *constructor* spelled as *reference* has, as the checker selects."""
        from agnette.agl.typecheck.constructors import selected_constructor_signature

        return selected_constructor_signature(
            reference.type_env,
            reference.reference,
            constructor,
            reference.reference.span,
            type_vars=frozenset(),
        )[1]

    @property
    def _ambient_type_names(self) -> frozenset[str]:
        """Root-declared type names from prior promoted entries.

        Derived from :attr:`_session_type_paths` rather than accumulated
        separately, for qualified constructor access (``Owner::variant``)
        across REPL entries and for :meth:`type_names`.
        """
        from agnette.agl.scope.type_owners import root_type_names

        return root_type_names(self._session_type_paths)

    def type_names(self) -> frozenset[str]:
        """Return the names of types declared in prior promoted entries.

        Drives the REPL highlighter's type colouring: a NAME matching one of
        these (or a builtin type spelling) is rendered as a type.  Types declared
        in an entry become available here only after that entry is promoted.
        """
        return self._ambient_type_names

    def constructor_names(self) -> frozenset[str]:
        """Return the constructor names declared in prior promoted entries.

        Drives the REPL highlighter's constructor colouring (enum variants and
        record constructors).  Like :meth:`type_names`, populated on promotion.
        """
        from agnette.agl.modules.ids import ENTRY_ID
        from agnette.agl.scope.type_owners import owned_constructors

        return frozenset(
            name for name, _, _, _ in owned_constructors(ENTRY_ID, self._session_type_paths)
        )

    def close(self) -> None:
        """Close the session's companion state, honoring the current ``debug`` setting."""
        debug = self._current.get("debug")
        self._extern_runtime_state.close_all(
            debug=debug is not None and cast("BoolValue", debug).value
        )

    def reset(self) -> None:
        """Clear ALL session state (symbols, types, values, source, ids).

        Closes the session's companion state first (see :meth:`close`).
        Restores the live engine settings (strict-json/timeout)
        to their values at session construction, undoing any effect-at-binding
        from ``std/config`` writes entered during the session.
        """
        self.close()
        from agnette.agl.lower import LinkImage
        from agnette.agl.scope.symbols import ScopeNode
        from agnette.agl.typecheck.env import TypeEnvironment

        self._session_scope = ScopeNode(node_id=-1, parent=None)
        self._session_scope_nodes = {(): self._session_scope}
        self._session_type_paths = {}
        self._session_builtin_declarations = {}
        self._session_docs = {}
        self._session_regions = {}
        self._type_env = TypeEnvironment()
        self._type_env.seal()
        self._link_image = LinkImage()
        self._ir_base_frame = {}
        self._next_node_id = 0
        self._open_failure = ()
        self._entry_origins = {}
        self._source_log = []
        # Restore the current-value map to the seed, so a prior
        # ``std/config::KEY := VALUE`` write does not bleed past :reset.
        # ``_engine_seed`` is now populated with any declared default learned
        # while ``std/config`` was loaded (see
        # :meth:`_record_declared_engine_defaults`), so this lands on a host
        # control where one exists and a declared default otherwise -- the
        # same precedence construction applies, since a declared default is
        # only ever added via ``setdefault`` and never outranks a host seed.
        self._current = dict(self._engine_seed)
        self._shell_exec_timeout = self._seeded_timeout_seconds()
        self._trace_path = self._initial_trace_path
        self._builtin_var_values = dict(self._builtin_var_seed)
        self._param_seed_values = {}
        self._pending_param_raw_values = {}
        # Clear module state.
        self._roots = None
        self._loaded_lib_modules = {}
        self._pending_bootstrap_warnings = []
        # The retained library artifacts survive: each is consulted only through
        # the identity of the AST or checked module it was derived from, so a
        # reopened session reuses one only when it loads that very module, and
        # anything else it holds is never consulted again. A reopen that reloads
        # the same standard library therefore keeps its library image even when
        # it has to recompile the entry program.
        self._accumulated_imports = []
        self._accumulated_scoped_imports = []
        self._accumulated_infix = {}
        # Discard the session's extern (Python FFI) registry like every other
        # session-scoped binding: a companion resolves and imports again on
        # its next use, as though the session were new.
        self._runtime.reset_extern_registry()
        self._session_host.reset_all()

    def load_file(self, path: "Path") -> list[EntryResult]:
        """Evaluate the contents of *path* incrementally.

        Saved REPL transcripts retain their original entry boundaries. Other
        files feed each top-level item to :meth:`eval_entry` in order. This
        preserves both declaration-wide forward resolution and redefinition
        across entries, so a ``:save`` transcript reliably round-trips through
        ``:load``.

        The load halts at the FIRST non-``ok`` result (like running a script);
        the returned list holds the results collected so far, including the
        failing one.  Items that already succeeded remain promoted.  The file's leading
        inner attributes head its first entry, so a module-level ``@!doc`` warns
        once, like one typed at the prompt, while its ``@!allow`` names govern the
        warnings of every entry. Diagnostics and runtime errors carry the file's
        path and file-relative positions.

        A file that is no program (a syntax error, over-deep nesting) is evaluated as a
        single entry: a bare type entry succeeds, anything else yields a single failed
        ``EntryResult`` carrying the parse diagnostic.  An empty or comment-only file has no
        items to run and yields an empty list (a benign no-op).
        """
        from agnette.agl.lexer import lex_replay, lex_source, tab_warning_collector
        from agnette.agl.parser import entry_slices, parse_repl_transcript
        from agnette.agl.scope.attributes import module_allowed_names
        from agnette.core.fs import read_text
        from agnette.util.text import normalize_newlines

        # Normalize newlines with the SAME helper the lexer/interpreter use so the
        # item-span char offsets align with the text we slice below.
        normalized = normalize_newlines(read_text(path))

        label = str(path)
        saved_entries = _decode_transcript(normalized)
        if saved_entries is not None:
            saved_results: list[EntryResult] = []
            try:
                for entry in saved_entries:
                    self._inherited_allows = entry.allows
                    result = self.eval_entry(
                        entry.text, origin=_origin_at(label, normalized, entry.offset)
                    )
                    saved_results.append(result)
                    if not result.ok:
                        break
            finally:
                self._inherited_allows = frozenset()
            return saved_results

        # A blank / comment-only file parses to an empty module with nothing
        # to run; skip the pipeline entirely and load it as a no-op.
        lexed = lex_source(normalized)
        if not has_runnable_statements(lexed):
            return []

        # Parse the whole file ONCE only to find top-level item boundaries;
        # this parse is never promoted (each slice is re-parsed by eval_entry with
        # the session's continuing node-id counter).  start_id=0 is fine here.
        # A file that is no program is one entry as a whole: a bare type, else its
        # syntax error.
        failure: AglError | None = None
        with tab_warning_collector() as tab_sink, lex_replay(lexed):
            try:
                program = parse_repl_transcript(normalized, source=self._next_entry_source())
            except AglError as exc:
                failure = exc
        if failure is not None:
            return [
                self._eval_unparsed(
                    normalized,
                    FileOrigin(label, 1, 1),
                    failure,
                    list(tab_sink),
                    lexed if lexed.failure is None else None,
                )
            ]

        # The file's leading inner attributes head its first item, as if typed with it;
        # their ``@!allow`` names govern every item. A file of attributes alone is one entry.
        leading = [attribute.span.start_offset for attribute in program.attributes]
        spans = [(item.span.start_offset, item.span.end_offset) for item in program.body.items]
        if not spans:
            spans = [(min(leading), max(a.span.end_offset for a in program.attributes))]
        spans[0] = (min([spans[0][0], *leading]), spans[0][1])
        # Each entry is read off the file's one lex and parse, not scanned and parsed again.
        readings = entry_slices(lexed, spans)
        results: list[EntryResult] = []
        self._inherited_allows = module_allowed_names(program)
        try:
            for (start, end), reading in zip(spans, readings):
                result = self.eval_entry(
                    normalized[start:end],
                    origin=_origin_at(label, normalized, start),
                    lexed=reading,
                )
                results.append(result)
                if not result.ok:
                    break  # halt on the first failing item, like a script
        finally:
            self._inherited_allows = frozenset()
        return results

    def _eval_unparsed(
        self,
        text: str,
        origin: FileOrigin,
        failure: AglError,
        tab_warnings: list[Diagnostic],
        lexed: "LexedSource | None",
    ) -> EntryResult:
        """Settle *text*, a whole file that is no program, as one entry rejected with *failure*.

        *failure* is the file's parse error, stamped with the entry's next source identity
        (see :meth:`_next_entry_source`): the program reading is not run again, so only a
        bare type entry can still succeed.
        """
        source = self._begin_entry(origin)
        result = self._fail_static_governed(failure, tab_warnings)
        settled = self._settle_entry(
            text, result, next_id=self._next_node_id, lexed=lexed, source=source
        )
        return relocate(settled, self._place, unplaced=REPL_LABEL)

    def dump_source(self) -> str:
        """Serialize successfully promoted sources with their entry boundaries."""
        if not self._source_log:
            return ""
        from agnette.util.text import normalize_newlines

        def frame(source: str, allows: frozenset[str]) -> str:
            entry = normalize_newlines(source)
            mark = _TRANSCRIPT_ALLOW_MARK + ",".join(sorted(allows)) if allows else ""
            return f"{_TRANSCRIPT_ENTRY_PREFIX}{len(entry)}{mark}\n{entry}\n"

        return _TRANSCRIPT_HEADER + "".join(frame(*logged) for logged in self._source_log)
