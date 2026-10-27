"""Click command for a program's CLI surface.

Type-to-flag projection and raw-value rules live in :mod:`agnette.host_params.projection`.

:func:`build_program_command` builds a whole program's CLI surface from its
declaration: one ``click.Command`` carrying a catch-all positional argument
and one option per name-addressable parameter, plus a reservation check
against the invoking surface's flag inventory and against every other
parameter's own spellings. Click owns every token convention — short options, bundling,
attached values, ``--x=V``, the ``--`` end-of-options marker, and
``-h``/``--help`` — while zone pairing, missing required arguments and excess
positionals stay with the shared binder in ``agnette.agl.runtime.arguments``, the
one place they are diagnosed. A parameter supplied more than once is
diagnosed here instead: the binder takes a name-keyed mapping and so is
entitled to assume its caller already rejected repetition.

The command also owns its own help. :func:`program_help_requested` asks Click
whether a token stream requests it — so a ``-h`` bundled into a short group is
a help request while one standing where a value-taking option expects its
value is that value — and :func:`render_program_help` renders it: the usage
line of the invocation the reader typed, the program's ``@doc`` as the
description (or whatever description its host prefers), one entry per visible
option with its own ``@doc`` and metavar, and no entry at all for a hidden
one. :meth:`ProgramCommand.parse` raises :class:`ProgramHelpRequested` for a
help request its caller has not already recognized, so the tokens are read
once, by Click, however the invocation reaches this module.

A parameter's *external* name (its declared name, or whatever ``@opt-name``
renames it to) governs every host surface: the flag, its derived negative,
the qualified config key, completion and the undeclared-config-key warning.
The external name stops here: :meth:`ProgramCommand.parse` maps it back to
the declared name, so the shared binder only ever sees declared names.

Each surface a program runs under reserves its own flag inventory.
``EXEC_RESERVED_FLAGS`` is ``agm exec``'s: the flags it declares itself
(``--help``, ``-p``, ``--program``, …). ``REGISTERED_RESERVED_FLAGS`` is a
package-registered command's: its ``agm exec`` run-time options
(``REGISTERED_RUN_FLAGS``) beside the help flags every program command owns.
A program parameter can never be projected onto its surface's reserved flags
— :func:`build_program_command` reports the collision instead. Engine settings
are host parameters of the lowest claim level, so they reserve nothing, and no
reserved flag is an engine-setting flag. A host with no program (``agm repl``,
the FILE scan of ``agm exec``) uses :func:`engine_command`, the engine settings'
bare surface.

The same inventory decides which tail token is ``agm exec``'s FILE argument
and which belong to the program: :func:`split_exec_tail` is the one place
that split is derived, and :func:`retain_end_of_options` is what keeps the
end-of-options marker visible to it across Click's own parse.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import click
from click.core import ParameterSource

from agnette.agl.ir.static_keys import HostParamKey
from agnette.agl.runtime.arguments import ProgramArguments, option_none_raw
from agnette.agl.runtime.types import (
    HostParamInfo,
    HostParamKind,
    ProgramDeclInfo,
    ProgramParamInfo,
)
from agnette.agl.zones import NAME_ADDRESSABLE_ZONES, ParamZone
from agnette.agm.cli_support.execution_options import (
    EXECUTION_OPTION_NAMES,
    execution_option_flags,
    write_execution_options,
)
from agnette.host_params.projection import (
    ProjectedOption,
    ValueForm,
    is_nullable_value_form,
    param_spellings,
    project_option,
    project_param_option,
    short_flag,
    spelling_tuple,
    token_raw_value,
)
from agnette.host_params.surface import ParamSurface, ParamSurfaceEntry, build_param_surface
from agnette.util.text import keep_indented_paragraphs

__all__ = [
    "DuplicateOptionFlagError",
    "EXEC_RESERVED_FLAGS",
    "ExecTail",
    "ProgramCommand",
    "ProgramHelpRequested",
    "ProgramOptionError",
    "ParsedTail",
    "REGISTERED_RESERVED_FLAGS",
    "REGISTERED_RUN_FLAGS",
    "ReservedFlagError",
    "build_program_command",
    "contains_help_flag",
    "engine_command",
    "exec_program_help",
    "exec_program_name",
    "option_value_map",
    "program_command_for",
    "program_help_requested",
    "protect_potential_program_values",
    "protect_host_option_values",
    "render_program_help",
    "retain_end_of_options",
    "split_exec_tail",
]

#: Help section listing the engine settings.
ENGINE_SETTINGS_SECTION = "Engine settings"

# Zones whose parameter fills a positional CLI slot.
_POSITIONAL_ZONES = (ParamZone.POSITIONAL_ONLY, ParamZone.STANDARD)

# The command parameter every positional token lands in. Click keys its parsed
# values by parameter name, so this and the per-option names below are internal
# identifiers only — a parameter's user-visible spelling lives in its flags.
_POSITIONAL_DEST = "_positionals"


# The flags a program command reserves for its own help, in both spellings.
HELP_FLAGS = ("--help", "-h")


def contains_help_flag(tokens: "Sequence[str]") -> bool:
    """Return whether *tokens* spell a help flag at all, in either spelling.

    A cheap pre-filter a host applies before discovering a program, matching
    only the common spelling: a hit is worth building the program's command
    for. A help flag bundled into a short group, as in ``-vh``, is missed
    here and recognized later by the program command itself while parsing.
    """
    return any(token in HELP_FLAGS for token in tokens)


class _HelpSignal(Exception):
    """One program command's help option, read while parsing its tokens.

    Raised by the help callback, which knows only that the flag was read.
    The command being parsed re-raises it as :class:`ProgramHelpRequested`,
    attaching itself, so every instance that escapes this module carries the
    command a handler needs to render.
    """


class ProgramHelpRequested(_HelpSignal):
    """Raised when a token stream asks a program command for its help.

    ``command`` is the command whose help was asked for, and ``program`` the
    selected declaration it was built from — attached by the host that holds
    it, so a caller can render the help its own invocation calls for (an
    ``agm exec`` usage line, or a registered command's) without discovering
    the program a second time.
    """

    def __init__(
        self,
        command: "ProgramCommand",
        program: ProgramDeclInfo | None = None,
    ) -> None:
        super().__init__("program help requested")
        self.command = command
        self.program = program


def _help_callback(ctx: click.Context, param: click.Parameter, value: object) -> None:
    """Signal a help request instead of printing, leaving rendering to the caller."""
    del param
    if value and not ctx.resilient_parsing:
        raise _HelpSignal("program help requested")


def _help_option() -> click.Option:
    """Return the ``-h``/``--help`` option every program command carries."""
    return click.Option(
        [*HELP_FLAGS],
        is_flag=True,
        is_eager=True,
        expose_value=False,
        callback=_help_callback,
        help="Show this message and exit.",
    )


def _positional_argument() -> click.Argument:
    """Return the catch-all argument every non-option token lands in."""
    return click.Argument([_POSITIONAL_DEST], nargs=-1)


def _positive_dest(index: int) -> str:
    return f"_opt{index}"


def _negative_dest(index: int) -> str:
    return f"_neg{index}"


# Flags ``agm exec`` declares itself, short spellings included: with
# ``@opt-short`` a program parameter can claim a single-dash spelling of its
# own, so the host's own short flags are reserved beside its long ones.
# Every flag the command declares must appear either here or in
# ``execution_option_flags()``: an unreserved flag is worse than a rejected
# program parameter, because Click binds the token to the built-in option and
# the parameter advertised under that flag silently keeps its default. The
# declarations live in ``agnette.agm.cli``, a layer above this one, so they are
# mirrored here; the tests derive ``agm exec``'s own option spellings from
# that command and assert every one of them is reserved, so a flag added
# there and forgotten here fails.
_BUILTIN_EXEC_FLAGS: frozenset[str] = frozenset(
    {
        "--code",
        "-c",
        "--program",
        "-p",
        "--module-path",
        "-I",
        "--max-call-depth",
        # ``agm exec`` turns off Click's built-in help option so that program
        # ``--param`` tokens pass through to it, and recognises these itself.
        "--help",
        "-h",
        "--no-stdlib",
        "--warning",
    }
)

# ``agm exec``'s reserved flag strings: built-ins plus every shared execution
# option. Collision checking is verbatim — no underscore/hyphen normalisation.
EXEC_RESERVED_FLAGS: frozenset[str] = _BUILTIN_EXEC_FLAGS | execution_option_flags()

# ``agm exec``'s run-time options a package-registered command parses too.
# ``agnette.agm.cli_dispatch`` takes the options themselves from ``agm exec``'s command
# by these spellings.
REGISTERED_RUN_FLAGS: frozenset[str] = execution_option_flags()

# A package-registered command's reserved flag strings: its run-time options
# and help. Its program may claim ``agm exec``'s other spellings
# (``-p``, ``--module-path``, …), which select sources a registration fixes.
REGISTERED_RESERVED_FLAGS: frozenset[str] = frozenset(HELP_FLAGS) | REGISTERED_RUN_FLAGS

# The end-of-options marker. ``agm exec`` consumes one only when it is what
# names the FILE; any other marker belongs to the program and reaches it.
END_OF_OPTIONS = "--"


@dataclass(frozen=True, slots=True)
class ExecTail:
    """``agm exec``'s source selector and the tokens the program itself reads.

    ``file`` is the FILE argument — a path or an installed reference — or
    ``None`` when the invocation names none (``-c/--code``, or nothing at
    all). ``tokens`` is every remaining tail token in written order, for the
    selected program's own command to read.
    """

    file: str | None
    tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedTail:
    """Raw program arguments and host-parameter values from one CLI tail.

    ``positionally_filled`` names the parameters a positional token supplied.
    """

    arguments: ProgramArguments
    params: dict["HostParamKey", object]
    positionally_filled: frozenset[str] = frozenset()


def _is_option_token(token: str) -> bool:
    """Return whether *token* is written as an option rather than as a value.

    A lone ``-`` is a value, exactly as Click reads it.
    """
    return len(token) > 1 and token.startswith("-")


def _is_host_option(token: str) -> bool:
    """Return whether *token* spells one of ``agm exec``'s own options.

    The host's inventory is :data:`_BUILTIN_EXEC_FLAGS`, and a long option's
    ``--x=V`` form spells the same option as ``--x``.
    """
    return token.partition("=")[0] in _BUILTIN_EXEC_FLAGS


def _file_index(tokens: "Sequence[str]", arity: Mapping[str, bool] | None = None) -> int | None:
    """Return the position of the FILE token in *tokens*, or ``None`` for no FILE.

    *arity* gives the separate-value arity of the options known beforehand;
    :func:`split_exec_tail` passes the engine-only surface's, and only when no
    program resolves.

    Only the host's own options may precede the FILE, and Click has already
    consumed the ones it declares, so what is left before the FILE is a help
    flag (which ``agm exec`` declares but does not let Click intercept) or a
    program option written out of position. A program option's arity is
    unknown until the program is known — which is what the FILE selects — so
    a value token directly after one is read as that option's value rather
    than as the FILE, unless the option already carries its value inline.
    """
    claims_next = False
    for index, token in enumerate(tokens):
        if _is_option_token(token):
            flag, separator, _value = token.partition("=")
            if arity is not None and flag in arity:
                claims_next = arity[flag] and not separator
            else:
                claims_next = not _is_host_option(token) and "=" not in token
            continue
        if claims_next:
            claims_next = False
            continue
        return index
    return None


def _file_index_from_program(
    tokens: "Sequence[str]", program_command_for_file: "Callable[[str], ProgramCommand | None]"
) -> int | None:
    """Find a FILE candidate whose selected program supplies its option arity.

    A pre-FILE program flag cannot be classified from spelling alone: it may
    be a flag, take a separate value, or take an attached short value. The
    source it selects provides that information. Candidates are considered
    from right to left because the FILE follows the program tokens in this
    otherwise ambiguous form. A resolver returning ``None`` means the token
    does not select one usable program, so it cannot settle the ambiguity.
    """
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        if not _is_option_token(token) and program_command_for_file(token) is not None:
            return index
    return None


type OptionValueMap = Mapping[str, bool] | Collection[str]


def _option_value_map(options: OptionValueMap) -> Mapping[str, bool]:
    """Normalize an option inventory to flag → takes-separate-value metadata."""
    if isinstance(options, Mapping):
        return options
    return {flag: False for flag in options}


def _option_token_ownership(token: str, options: OptionValueMap) -> tuple[bool, bool]:
    """Return whether *token* is owned and claims the next token by *options*.

    The short-option walk follows Click's attached-value convention: a
    value-taking ``-p`` owns both ``-p VALUE`` and ``-pVALUE``. The second
    result is true only for the separate form.
    """
    option_values = _option_value_map(options)
    if token.startswith("--"):
        flag, separator, _value = token.partition("=")
        takes_value = option_values.get(flag)
        return (takes_value is not None, bool(takes_value and not separator))
    if not token.startswith("-") or token == "-":
        return False, False
    recognized = False
    short_group = token[1:]
    for offset, short in enumerate(short_group):
        takes_value = option_values.get(f"-{short}")
        if takes_value is None:
            continue
        recognized = True
        if takes_value:
            return True, offset + 1 == len(short_group)
    return recognized, False


def option_value_map(params: Sequence[click.Option]) -> dict[str, bool]:
    """Return Click option spellings and whether each consumes a value."""
    return {
        flag: not param.is_flag for param in params for flag in (*param.opts, *param.secondary_opts)
    }


def retain_end_of_options(
    args: "Sequence[str]", host_options: OptionValueMap = frozenset()
) -> list[str]:
    """Return *args* with the host's own end-of-options marker doubled.

    Click's parser removes the first bare ``--`` that is not already the
    value of a host option, so the tail it hands over no longer shows where
    host option scanning ended. Doubling that marker keeps both readings: Click removes
    the copy it would have removed anyway — stopping option parsing at
    exactly the same token — and the survivor is the marker the reader wrote.
    For ``agm exec`` that survivor marks the boundary for
    :func:`split_exec_tail`, which consumes it only when it is what named the
    FILE and otherwise forwards it; a registered package command names no
    FILE, so its survivor always reaches the program. Either way the
    program's own parser reads it.
    """
    tokens = list(args)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == END_OF_OPTIONS:
            return [*tokens[:index], END_OF_OPTIONS, *tokens[index:]]
        _owned, claims_next = _option_token_ownership(token, host_options)
        index += 2 if claims_next and index + 1 < len(tokens) else 1
    return tokens


def split_exec_tail(
    tail: "Sequence[str]",
    *,
    program_command_for_file: "Callable[[str], ProgramCommand | None] | None" = None,
) -> ExecTail:
    """Derive ``agm exec``'s FILE argument and the program's own tokens from *tail*.

    The single place a tail token is classified. *tail* is what Click leaves
    after parsing the host's own options: the FILE, the program's tokens, the
    help flags, and the end-of-options marker :func:`retain_end_of_options`
    kept. The marker ends host option scanning: when the tokens before it name
    no FILE, the token after it is the FILE however it is spelled — this is
    how a file named like an option is named — and the host consumes that
    marker. A marker that named no FILE was written for the program instead,
    and is forwarded in its own position for the program's parser to apply.
    Everything else keeps its written order too, so the program's command
    reads exactly the tokens the reader wrote for it. A tail whose program
    flags precede the FILE cannot be split by spelling alone — a program
    flag's arity is not known until the program is, and the FILE is what
    selects it — so when *program_command_for_file* is given, the rightmost
    pre-marker token naming one usable program settles it. That scan is
    inference where a marker is an explicit statement, so a marker naming a
    usable program wins over it. Without a resolver (inline ``-c`` source,
    which names no FILE) the inexpensive spelling-only scan stands.
    """
    tokens = list(tail)
    marker = tokens.index(END_OF_OPTIONS) if END_OF_OPTIONS in tokens else None
    before_marker = tokens if marker is None else tokens[:marker]
    index = _file_index(before_marker)
    needs_program_arity = index is None or any(
        _is_option_token(token) and not _is_host_option(token) for token in before_marker[:index]
    )
    if needs_program_arity and program_command_for_file is not None:
        # Preserve the inexpensive spelling-only answer when it actually
        # names a usable program. Otherwise its unknown arity may have put
        # the FILE one token too early or too late, so consult the marker
        # first — the reader wrote it to say which token is the FILE, where
        # the pre-marker scan only infers one — and let the target's program
        # signature settle it only when the marker names no usable program.
        if index is None or program_command_for_file(before_marker[index]) is None:
            marker_names_file = (
                marker is not None
                and marker + 1 < len(tokens)
                and program_command_for_file(tokens[marker + 1]) is not None
            )
            index = (
                None
                if marker_names_file
                else _file_index_from_program(before_marker, program_command_for_file)
            )
            if index is None and not marker_names_file:
                # No program resolves: only the engine-only surface's option arity applies.
                index = _file_index(before_marker, engine_command().option_arity())
    consumed: set[int] = set()
    if index is None and marker is not None and marker + 1 < len(tokens):
        # The marker is what named the FILE, so the host consumes it. A marker
        # that named no FILE was written for the program instead, and stays in
        # its own position for the program's own parser to read.
        index = marker + 1
        consumed.add(marker)
    if index is not None:
        consumed.add(index)
    return ExecTail(
        file=None if index is None else tokens[index],
        tokens=tuple(token for position, token in enumerate(tokens) if position not in consumed),
    )


def protect_host_option_values(
    tokens: Sequence[str], program_command: ProgramCommand | None, host_options: OptionValueMap
) -> tuple[list[str], dict[str, str]]:
    """Hide host-looking values until an outer Click command has parsed.

    Click otherwise consumes a known host flag even when a selected program's
    value-taking option owns that following token. Replacing just those
    tokens with unique non-options preserves ordinary parsing, after which a
    host restores the original program tail.
    """
    if program_command is None:
        return list(tokens), {}
    protected = program_command.value_token_indexes(tokens, host_options=host_options)
    replacements: dict[str, str] = {}
    parsed = list(tokens)
    unavailable = set(tokens)
    for index in protected:
        value = tokens[index]
        owned_by_host, _claims_next = _option_token_ownership(value, host_options)
        if value != END_OF_OPTIONS and not owned_by_host:
            continue
        replacement = f"agm-program-value-{index}"
        suffix = 1
        while replacement in unavailable:
            replacement = f"agm-program-value-{index}-{suffix}"
            suffix += 1
        parsed[index] = replacement
        replacements[replacement] = value
        unavailable.add(replacement)
    return parsed, replacements


def protect_potential_program_values(
    tokens: Sequence[str], host_options: OptionValueMap
) -> list[str]:
    """Hide possible program values for an advisory outer parse.

    Before a source has been discovered its unknown options have unknown
    arity. Treat a separate token after one as a possible value only for the
    preview that locates that source, except after a flag the engine-only
    surface (:func:`engine_command`) knows takes none. The authoritative pass
    later uses the discovered :class:`ProgramCommand` and restores ordinary
    host ownership.
    """
    parsed = list(tokens)
    unavailable = set(tokens)
    engine_arity = engine_command().option_arity()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == END_OF_OPTIONS:
            break
        owned_by_host, host_claims_next = _option_token_ownership(token, host_options)
        if owned_by_host:
            index += 2 if host_claims_next and index + 1 < len(tokens) else 1
            continue
        if engine_arity.get(token) is False:
            index += 1
            continue
        possible_program_option = token.startswith("--") and "=" not in token
        possible_program_option = possible_program_option or (
            token.startswith("-") and token != "-" and len(token) == 2
        )
        if not possible_program_option or index + 1 >= len(tokens):
            index += 1
            continue
        value_index = index + 1
        value = tokens[value_index]
        value_owned_by_host, _claims_next = _option_token_ownership(value, host_options)
        if value == END_OF_OPTIONS or value_owned_by_host:
            replacement = f"agm-program-preview-{value_index}"
            suffix = 1
            while replacement in unavailable:
                replacement = f"agm-program-preview-{value_index}-{suffix}"
                suffix += 1
            parsed[value_index] = replacement
            unavailable.add(replacement)
        index += 2
    return parsed


@dataclass(frozen=True, slots=True)
class ReservedFlagError:
    """One program parameter's projected flag collides with a reserved host flag."""

    parameter: str
    flag: str


@dataclass(frozen=True, slots=True)
class DuplicateOptionFlagError:
    """Two program parameters project onto the same CLI flag.

    AgL identifiers are kebab-case, so e.g. ``cache: bool`` and
    ``no-cache: bool`` are both plausible parameter names, yet the first's
    negative flag (``--no-cache``) and the second's positive flag spell the
    same string. ``first_parameter``/``second_parameter`` are the colliding
    parameters' declared names, in declaration order.
    """

    first_parameter: str
    second_parameter: str
    flag: str


type ProgramOptionError = ReservedFlagError | DuplicateOptionFlagError


def _reject_repetition(
    occurrences: Sequence[object], flag: str, negative_flag: str | None = None
) -> None:
    """Reject a parameter supplied by more than one CLI token.

    Every projected option is ``multiple`` (:class:`_ProgramOption`), so Click
    records one entry per occurrence and anything past the first is the
    duplicate. The message names the offending token's own polarity:
    *negative_flag* is given only for a ``bool`` pair, whose two polarities
    share one Click parameter and whose occurrences carry their own polarity
    (``True`` for *flag*), so a repeated ``--no-x`` is reported as ``--no-x``
    rather than as the ``--x`` the user never typed. A parameter's long flag
    stands in for its ``@opt-short`` spelling, which shares the same Click
    parameter and leaves no separate trace. An ``@opt-env`` value is always a
    single occurrence, so it never collides with the token that overrides it.
    """
    if len(occurrences) < 2:
        return
    offender = occurrences[1]
    spelling = flag if negative_flag is None or offender else negative_flag
    raise ValueError(f"Option {spelling!r} specified more than once")


def _from_commandline(ctx: click.Context, dest: str) -> bool:
    """Return whether *dest* was filled by a CLI token rather than the environment.

    Click records where each parameter's value came from, which is the only
    way to tell a token from an environment fallback: both arrive as an
    ordinary parsed value.
    """
    return ctx.get_parameter_source(dest) is ParameterSource.COMMANDLINE


def _from_environment(ctx: click.Context, dest: str) -> bool:
    return ctx.get_parameter_source(dest) is ParameterSource.ENVIRONMENT


# One parameter no CLI token and no ``@opt-env`` value supplied, which no
# parsed value can be mistaken for.
_NOT_SUPPLIED: object = object()


def _supplied_value(
    ctx: click.Context,
    values: Mapping[str, object],
    index: int,
    projected: ProjectedOption,
    *,
    flag: str,
    negative_flag: str,
    paired_polarities: bool,
    suppress_positive: bool,
) -> object:
    """Return one parsed option's raw value, or :data:`_NOT_SUPPLIED`.

    The one reading of a projected option's two Click parameters, shared by
    program and module parameters: a parameter supplied by more than one
    token is rejected, and a negatable one given both polarities on the
    command line is the contradiction. *paired_polarities* marks a ``bool``
    whose two flags fill a single Click parameter, so its occurrences already
    carry their own polarity and repetition alone can be diagnosed.
    *suppress_positive* drops a positive value a higher-ranked source already
    supplied; a negative one is a token and always stands.
    """
    positive = cast(tuple[object, ...], values.get(_positive_dest(index), ()))
    bool_form = projected.value_form is ValueForm.BOOL
    if bool_form and paired_polarities:
        _reject_repetition(positive, flag, negative_flag)
        return _NOT_SUPPLIED if suppress_positive or not positive else positive[0]
    _reject_repetition(positive, flag)
    if not bool_form and not is_nullable_value_form(projected.value_form):
        if suppress_positive or not positive:
            return _NOT_SUPPLIED
        return token_raw_value(projected, cast(str, positive[0]))
    negative = cast(tuple[object, ...], values.get(_negative_dest(index), ()))
    _reject_repetition(negative, negative_flag)
    # Two Click parameters, so only their sources tell a supplied token from
    # an ``@opt-env`` value. Both polarities typed together is the
    # contradiction; a ``--no-x`` token against an environment-supplied
    # positive is not, since a CLI token outranks the environment.
    if _from_commandline(ctx, _positive_dest(index)) and _from_commandline(
        ctx, _negative_dest(index)
    ):
        raise ValueError(f"Options {flag!r} and {negative_flag!r} cannot both be supplied")
    if negative:
        return False if bool_form else option_none_raw()
    if suppress_positive or not positive:
        return _NOT_SUPPLIED
    return positive[0] if bool_form else token_raw_value(projected, cast(str, positive[0]))


class _ProgramOption(click.Option):
    """A program parameter's Click option: every occurrence recorded, separately.

    Every projected option is ``multiple``, so Click records one entry per
    token rather than collapsing repetitions to the last value — a parameter
    supplied twice is a usage error (:meth:`ProgramCommand.parse` diagnoses
    it), not a silent overwrite, and the shared binder downstream is entitled
    to assume duplicates were already caught.

    An ``@opt-env`` value stays exactly one occurrence: Click otherwise reads
    a ``multiple`` option's environment value as a ``os.path.pathsep``-joined
    list, which would make ``VAR=a:b`` look like a repeated flag. Click
    supplies an environment value only when it is non-empty, so ``VAR=""``
    reads as unset and an environment fallback can never deliver an empty
    ``text``.
    """

    def value_from_envvar(self, ctx: click.Context) -> list[str] | None:
        """Return this option's environment value as a single occurrence."""
        raw = self.resolve_envvar_value(ctx)
        return None if raw is None else [raw]


class _EnvironmentFallbackOption(_ProgramOption):
    """An environment-only program parameter fallback with no CLI declaration."""

    def add_to_parser(self, parser: object, ctx: click.Context) -> None:
        """Keep the synthetic declaration out of Click's accepted option table."""
        del parser, ctx


# The placeholder a bare (non-optional) value form announces; every form left
# out reads one strict-JSON or AgL value-syntax token.
_VALUE_FORM_METAVARS: dict[ValueForm, str] = {
    ValueForm.TEXT: "TEXT",
    ValueForm.AGENT: "AGENT",
    ValueForm.JSON: "JSON",
}


def _value_form_metavar(form: ValueForm) -> str:
    """Return the placeholder for a bare (non-``Option``) value form."""
    return _VALUE_FORM_METAVARS.get(form, "VALUE")


def _default_metavar(param: "ProgramParamInfo | HostParamInfo", projected: ProjectedOption) -> str:
    """Return the placeholder standing for a value-taking option's own VALUE.

    A ``path`` parameter, directly or inside either optional enum, announces a path.
    Otherwise it names how the token is read rather than the declared type: a
    ``text`` parameter takes its token verbatim, standard ``Agent`` uses host
    syntax, ``json`` parses one JSON value, and every other type reads strict
    JSON or AgL value syntax. Both optional enums follow ``T``.
    """
    if param.is_path:
        return "PATH"
    if is_nullable_value_form(projected.value_form):
        return _value_form_metavar(cast(ValueForm, projected.option_inner_form))
    return _value_form_metavar(projected.value_form)


def _positive_option(
    index: int,
    param: "ProgramParamInfo | HostParamInfo",
    projected: ProjectedOption,
    declarations: Sequence[str],
    *,
    environment_only: bool = False,
) -> _ProgramOption:
    """Build a parameter's positive-polarity option under *declarations*.

    A ``bool`` form is a bare flag; every other form takes a ``VALUE``
    announced by the parameter's ``@opt-metavar`` or by its type's
    placeholder. Only this polarity carries the ``@opt-env`` fallback, so a
    variable is read once, and only it carries the ``@doc``, which describes
    the parameter rather than either polarity of its flag.
    *environment_only* builds the synthetic declaration for a parameter left
    with no positive spelling of its own, which carries neither help nor a
    visible flag because only its variable can fill it.
    """
    spec = param.cli
    flag_form = projected.value_form is ValueForm.BOOL
    is_flag = True if flag_form else None
    value_type = click.BOOL if flag_form else None
    metavar = None if flag_form else spec.metavar or _default_metavar(param, projected)
    if environment_only:
        return _EnvironmentFallbackOption(
            [_positive_dest(index), *declarations],
            is_flag=is_flag,
            type=value_type,
            metavar=metavar,
            default=None,
            multiple=True,
            envvar=spec.env,
            hidden=True,
        )
    return _ProgramOption(
        [_positive_dest(index), *declarations],
        is_flag=is_flag,
        type=value_type,
        metavar=metavar,
        default=None,
        multiple=True,
        envvar=spec.env,
        help=None if spec.doc is None else keep_indented_paragraphs(spec.doc),
        hidden=spec.hidden,
    )


def _negative_option(
    index: int, param: "ProgramParamInfo | HostParamInfo", declarations: Sequence[str]
) -> _ProgramOption:
    """Build a parameter's independent ``--no-`` flag under *declarations*.

    Carries neither help nor environment fallback: both belong to the
    parameter's positive polarity.
    """
    return _ProgramOption(
        [_negative_dest(index), *declarations],
        is_flag=True,
        type=click.BOOL,
        default=None,
        multiple=True,
        hidden=param.cli.hidden,
    )


def _click_params(
    index: int, param: "ProgramParamInfo", projected: ProjectedOption
) -> list[click.Parameter]:
    """Build the Click parameter(s) carrying one program parameter's CLI surface.

    A ``bool`` parameter is a single ``--x/--no-x`` flag, whose recorded
    occurrences carry their own polarity; an optional-enum parameter is a
    value-taking ``--x`` plus an independent ``--no-x`` flag, whose
    exclusivity :meth:`ProgramCommand.parse` checks once both are parsed;
    every other form is one value-taking option.
    """
    short = spelling_tuple(short_flag(param))
    if projected.value_form is ValueForm.BOOL:
        pair = f"{projected.flag}/{projected.negative_flag}"
        return [_positive_option(index, param, projected, (pair, *short))]
    positive = _positive_option(index, param, projected, (projected.flag, *short))
    negative = spelling_tuple(projected.negative_flag)
    if not negative:
        return [positive]
    return [positive, _negative_option(index, param, negative)]


def _surface_click_params(
    index: int, entry: ParamSurfaceEntry, projected: ProjectedOption
) -> list[click.Parameter]:
    """Build one surface parameter's Click options from its resolved flag table.

    A polarity every higher level already claimed contributes no option at
    all; a parameter left with no positive spelling keeps its ``@opt-env``
    fallback through a synthetic declaration.
    """
    positive = (*entry.positive_option_spellings, *spelling_tuple(entry.short_option))
    params: list[click.Parameter] = []
    if positive:
        params.append(_positive_option(index, entry.param, projected, positive))
    elif entry.param.cli.env is not None:
        params.append(
            _positive_option(
                index,
                entry.param,
                projected,
                (f"--_module-env-{index}",),
                environment_only=True,
            )
        )
    if entry.negative_option_spellings:
        params.append(_negative_option(index, entry.param, entry.negative_option_spellings))
    return params


def _surface_help_record(entry: ParamSurfaceEntry, projected: ProjectedOption) -> tuple[str, str]:
    """Render one surface parameter: its shortest positive spelling beside its shortest negative."""
    positive = (*entry.positive_option_spellings, *spelling_tuple(entry.short_option))
    labels: list[str] = []
    if positive:
        metavar = entry.param.cli.metavar or _default_metavar(entry.param, projected)
        labels.append(f"{positive[0]} {metavar}" if projected.takes_value else positive[0])
    labels.extend(entry.negative_option_spellings[:1])
    help_text = keep_indented_paragraphs(entry.param.cli.doc or "")
    if entry.param.cli.env is not None:
        environment = f"[env var: {entry.param.cli.env}]"
        help_text = f"{help_text}  {environment}" if help_text else environment
    return " / ".join(labels), help_text


def _help_section_title(entry: ParamSurfaceEntry) -> str:
    """Return the help section *entry* is listed under."""
    if entry.param.kind is HostParamKind.ENGINE:
        return ENGINE_SETTINGS_SECTION
    return f"Parameters of {entry.section}"


def _write_surface_section(
    formatter: click.HelpFormatter,
    title: str,
    entries: Sequence[tuple[ParamSurfaceEntry, ProjectedOption]],
    *,
    col_max: int = 30,
) -> None:
    """Write *title* listing the visible *entries*, or nothing when none is."""
    records = [
        _surface_help_record(entry, projected) for entry, projected in entries if not entry.hidden
    ]
    if records:
        with formatter.section(title):
            formatter.write_dl(records, col_max=col_max)


class _ProgramClickCommand(click.Command):
    """A program's Click command, whose usage line names its positional slots.

    Every positional token lands in one catch-all argument whose internal
    name says nothing to a reader, so the usage line is spelled from the
    program's own positional parameters instead, in declaration order; see
    :func:`_usage_slots` for how one slot is spelled.
    """

    def __init__(
        self,
        name: str,
        *,
        params: list[click.Parameter],
        description: str | None,
        usage_slots: tuple[str, ...],
        positional_help: tuple[tuple[str, str], ...] = (),
        parameter_sections: tuple[tuple[str, tuple[click.Parameter, ...]], ...] = (),
        surface_help_sections: tuple[
            tuple[str, tuple[tuple[ParamSurfaceEntry, ProjectedOption], ...]], ...
        ] = (),
        ambiguous_options: Mapping[str, tuple[HostParamInfo, ...]] | None = None,
        surface_entries: Mapping[HostParamInfo, ParamSurfaceEntry] | None = None,
        context_settings: dict[str, bool] | None = None,
    ) -> None:
        settings: dict[str, bool] = {} if context_settings is None else dict(context_settings)
        super().__init__(
            name=name,
            params=params,
            help=None if description is None else keep_indented_paragraphs(description),
            add_help_option=False,
            context_settings=settings,
        )
        self.usage_slots = usage_slots
        self.positional_help = positional_help
        self.parameter_sections = parameter_sections
        self.surface_help_sections = surface_help_sections
        self.ambiguous_options = {} if ambiguous_options is None else ambiguous_options
        self.surface_entries = {} if surface_entries is None else surface_entries
        self.execution_option_params = tuple(
            param for param in params if param.name in EXECUTION_OPTION_NAMES
        )

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Turn Click's invalid ambiguity value syntax into the candidate diagnostic."""
        ambiguous_short = self._ambiguous_short_option(args)
        if ambiguous_short is not None:
            short_candidates = self.ambiguous_options[ambiguous_short]
            raise click.UsageError(
                _ambiguous_option_message(ambiguous_short, short_candidates, self.surface_entries),
                ctx,
            )
        try:
            return super().parse_args(ctx, args)
        except click.BadOptionUsage as exc:
            candidates = self.ambiguous_options.get(exc.option_name)
            if candidates is None:
                raise
            raise click.UsageError(
                _ambiguous_option_message(exc.option_name, candidates, self.surface_entries), ctx
            ) from exc

    def _ambiguous_short_option(self, args: Sequence[str]) -> str | None:
        """Return an ambiguous short spelling Click reaches before any parse error.

        Click rejects the suffix of ``-tVALUE`` after accepting the flag
        ``-t`` when that flag is a rejecting ambiguity sentinel.  Detect that
        spelling before Click reaches its suffix so both the direct command
        and :meth:`ProgramCommand.parse` report the candidates.  The walk
        deliberately stops at a value-taking option: in ``-xt``, ``t`` is
        the attached value for ``-x``, not another short option.
        """
        option_values = option_value_map(cast(Sequence[click.Option], self.params[1:]))
        index = 0
        while index < len(args):
            token = args[index]
            if token == END_OF_OPTIONS:
                return None
            if token.startswith("--"):
                spelling, separator, _value = token.partition("=")
                if option_values.get(spelling, False) and not separator:
                    index += 2
                    continue
                index += 1
                continue
            if not token.startswith("-") or token == "-":
                index += 1
                continue
            for offset, short in enumerate(token[1:]):
                spelling = f"-{short}"
                if spelling in self.ambiguous_options:
                    return spelling
                takes_value = option_values.get(spelling)
                if takes_value is None:
                    index += 1
                    break
                if takes_value:
                    if offset + 2 == len(token) and index + 1 < len(args):
                        index += 2
                    else:
                        index += 1
                    break
            else:
                index += 1
                continue
            continue
        return None

    def collect_usage_pieces(self, ctx: click.Context) -> list[str]:
        """Return the usage pieces after the command name."""
        del ctx
        options = [] if self.options_metavar is None else [self.options_metavar]
        return [*options, *self.usage_slots]

    def format_options(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Render program options, module parameters, and shared execution options."""
        section_params = {
            id(param) for _title, params in self.parameter_sections for param in params
        }
        execution_param_ids = {id(param) for param in self.execution_option_params}
        own_options = [
            record
            for param in self.get_params(ctx)
            if id(param) not in section_params
            if id(param) not in execution_param_ids
            if (record := param.get_help_record(ctx)) is not None
        ]
        with formatter.section("Options"):
            formatter.write_dl(own_options)
        if self.positional_help:
            with formatter.section("Arguments"):
                formatter.write_dl(list(self.positional_help))
        for title, entries in self.surface_help_sections:
            _write_surface_section(formatter, title, entries)
        write_execution_options(formatter)


def _build_click_command(
    name: str,
    *,
    params: Sequence[click.Parameter],
    description: str | None,
    usage_slots: tuple[str, ...],
    positional_help: tuple[tuple[str, str], ...] = (),
    extra_options: Sequence[click.Parameter] = (),
    parameter_sections: tuple[tuple[str, tuple[click.Parameter, ...]], ...] = (),
    surface_help_sections: tuple[
        tuple[str, tuple[tuple[ParamSurfaceEntry, ProjectedOption], ...]], ...
    ] = (),
    ambiguous_options: Mapping[str, tuple[HostParamInfo, ...]] | None = None,
    surface_entries: Mapping[HostParamInfo, ParamSurfaceEntry] | None = None,
    context_settings: dict[str, bool] | None = None,
) -> _ProgramClickCommand:
    """Assemble one program command: its own parameters, then *extra_options*, then help.

    The single constructor for the command that parses a program's tokens,
    the one that renders its help under a host's own invocation name, and the
    stand-in for a program that could not be built, so none of them can
    advertise different options.
    """
    return _ProgramClickCommand(
        name,
        params=[*params, *extra_options, _help_option()],
        description=description,
        usage_slots=usage_slots,
        positional_help=positional_help,
        parameter_sections=parameter_sections,
        surface_help_sections=surface_help_sections,
        ambiguous_options=ambiguous_options,
        surface_entries=surface_entries,
        context_settings=context_settings,
    )


def _usage_slots(positional: "tuple[ProgramParamInfo, ...]") -> tuple[str, ...]:
    """Return one usage slot per positional-capable parameter, in declaration order.

    A slot is named by the parameter's own ``@opt-metavar`` when it declares
    one and by its external name otherwise, so a positional parameter — never
    addressed by a flag — still gets the placeholder its declaration asks for.

    ``@opt-hidden`` is about how a parameter is addressed *by name*, so it
    hides the parameter's ``--name`` entry alone: every positional-capable
    parameter keeps its slot here, because the shared binder still fills it
    from the positional tokens a reader types.
    """
    slots: list[str] = []
    for param in positional:
        slot = param.cli.metavar or param.cli.name
        slots.append(f"[{slot}]" if param.has_default else f"<{slot}>")
    return tuple(slots)


@dataclass(frozen=True, slots=True)
class ProgramCommand:
    """One program's whole CLI surface: a ``click.Command`` and what it stands for.

    ``command`` parses tokens; ``positional`` lists CLI positional-capable
    parameters in declaration order; and
    ``options`` pairs every AgL name-addressable parameter (``STANDARD``,
    ``NAMED_ONLY``) with its projected value form. An unzoned required
    parameter whose type has no negative polarity also appears in
    ``positional``; its Click option exists only for an environment
    fallback, when declared.

    ``options`` also carries the external↔declared correspondence: each entry
    holds the parameter's declared name and the external name its flags were
    spelled from (``ProgramParamInfo.cli.name``).

    ``params`` are the Click parameters ``command`` parses with, kept so the
    help rendering (:meth:`render_help`) can present the same surface under a
    host's own invocation name. The positional slots its usage line names
    live on ``command`` itself.
    """

    command: _ProgramClickCommand
    positional: tuple["ProgramParamInfo", ...]
    options: tuple[tuple["ProgramParamInfo", ProjectedOption], ...]
    surface_options: tuple[tuple[ParamSurfaceEntry, ProjectedOption], ...]
    surface: ParamSurface
    params: tuple[click.Parameter, ...]

    def parse(self, tokens: Sequence[str]) -> ParsedTail:
        """Parse *tokens* into raw positional/named host values, keyed by declared name.

        Click owns the token conventions: short options and their bundles,
        attached and ``--x=V`` values, the ``--`` end-of-options marker, and
        an option's value being whatever token follows it. Every non-option
        token lands in the catch-all positional slot unconditionally, and one
        that fills a slot is read as that parameter's flag value would be. An
        implicit positional CLI value is mapped to its AgL named parameter;
        the shared zone binder diagnoses any excess positional arguments, except
        a surplus after tokens routed by name, which it would misreport.

        A parameter whose value came from neither a token nor its
        ``@opt-env`` variable is left out of the result entirely, so a host
        can tell "supplied" from "defaulted" and fall back to its own
        configuration layer.

        :raises ProgramHelpRequested: when the tokens ask for this command's
            help, so a caller that has not already recognized the request —
            a ``-h`` bundled into a short group, say — renders it rather than
            running the program.
        :raises ValueError: for any Click usage error (unknown option,
            missing value, a value given to a flag), for a parameter supplied
            more than once, for a surplus positional token after tokens routed
            by name, or for an optional-enum parameter given both
            polarities on the command line. A malformed ``VALUE`` — including
            a malformed optional payload (see the module docstring) — is
            not raised here: it reaches this method's caller as a deferred
            decode diagnostic from ``decode_param_value``, anchored at the
            parameter's declaration.
        """
        try:
            ctx = self.command.make_context(self.command.name, list(tokens))
        except _HelpSignal as exc:
            raise ProgramHelpRequested(self) from exc
        except click.UsageError as exc:
            raise ValueError(exc.format_message()) from exc
        values = cast(dict[str, object], ctx.params)
        cli_tokens = cast(tuple[str, ...], values[_POSITIONAL_DEST])
        flagged = frozenset(
            param.name
            for index, (param, _projected) in enumerate(self.options)
            if _from_commandline(ctx, _positive_dest(index))
            or _from_commandline(ctx, _negative_dest(index))
        )
        slots = self._token_slots(len(cli_tokens), flagged)
        if any(slot is None for slot, _by_name in slots) and any(
            by_name for _slot, by_name in slots
        ):
            # Tokens routed by name leave the binder's positional list short
            # of the slots it walks, so it would pair a surplus token with a
            # slot already filled instead of reporting the overflow.
            raise ValueError("Too many positional arguments")
        positionally_filled = frozenset(slot.name for slot, _by_name in slots if slot is not None)

        named: dict[str, object] = {}
        for index, (param, projected) in enumerate(self.options):
            # A STANDARD parameter can be filled by either its positional
            # slot or its name. A positional CLI token outranks an envvar
            # fallback just as a named CLI token does, but a named CLI token
            # remains a duplicate for the shared binder to reject.
            value = _supplied_value(
                ctx,
                values,
                index,
                projected,
                flag=projected.flag,
                negative_flag=projected.negative_flag or projected.flag,
                paired_polarities=True,
                suppress_positive=param.name in positionally_filled
                and _from_environment(ctx, _positive_dest(index)),
            )
            if value is not _NOT_SUPPLIED:
                named[param.name] = value
        positional: list[object] = []
        for token, (slot, by_name) in zip(cli_tokens, slots, strict=True):
            if slot is None:
                positional.append(token)
                continue
            value = token_raw_value(project_option(slot.cli.name, slot.type), token)
            if by_name:
                named[slot.name] = value
            else:
                positional.append(value)
        surface_params: dict[HostParamKey, object] = {}
        for offset, (entry, projected) in enumerate(self.surface_options, start=len(self.options)):
            # Every surface spelling is resolved ahead of the parse, so a
            # polarity whose spellings a higher level claimed has no Click
            # parameter at all and the diagnostics fall back to the other.
            flag = entry.primary_flag
            value = _supplied_value(
                ctx,
                values,
                offset,
                projected,
                flag=flag,
                negative_flag=next(iter(entry.negative_option_spellings), flag),
                paired_polarities=False,
                suppress_positive=False,
            )
            if value is not _NOT_SUPPLIED:
                surface_params[entry.param.key] = value
        return ParsedTail(
            arguments=ProgramArguments(positional=tuple(positional), named=named),
            params=surface_params,
            positionally_filled=positionally_filled,
        )

    def value_token_indexes(
        self, tokens: Sequence[str], *, host_options: OptionValueMap = frozenset()
    ) -> frozenset[int]:
        """Return indexes that this program reads as separate option values.

        Hosts use this before their outer Click command runs. Host options
        and their own separate values are skipped as one unit. A token in one
        of these positions belongs to the program even when it happens to
        spell a host option, so the outer command must leave it in the raw
        tail. Inline long and attached short values are part of their option
        token and therefore have no separate index to report.
        """
        return self._read_tokens(tokens, host_options)[0]

    def next_positional(
        self, tokens: Sequence[str], token: str, *, host_options: OptionValueMap = frozenset()
    ) -> "ProgramParamInfo | None":
        """Return the positional slot *token*, written after *tokens*, fills.

        ``None`` when *token* fills none: option tokens, their separate values,
        and *host_options* with their values fill no slot, nor does any token
        once every positional-capable parameter is filled. Every token after
        ``--`` fills one.
        """
        _indexes, count, flagged = self._read_tokens(tokens, host_options)
        if self._read_tokens([*tokens, token], host_options)[1] == count:
            return None
        return self._token_slots(count + 1, flagged)[-1][0]

    def option_arity(self) -> Mapping[str, bool]:
        """Return each option spelling this command accepts and whether it takes a separate value.

        Only a positive spelling of a value-taking form does; a derived
        ``--no-x`` is valueless even when a parameter named ``no-x`` makes the
        same string a positive one elsewhere.
        """
        arity: dict[str, bool] = {}
        for param, projected in self.options:
            if param.cli.cli_positional:
                continue
            arity[projected.flag] = projected.takes_value
            if projected.negative_flag is not None:
                arity[projected.negative_flag] = False
            short = short_flag(param)
            if short is not None:
                arity[short] = projected.takes_value
        for entry, projected in self.surface_options:
            for spelling in entry.option_spellings:
                arity[spelling] = projected.takes_value and (
                    spelling in entry.positive_option_spellings or spelling == entry.short_option
                )
        return arity

    def value_options(
        self,
    ) -> tuple[tuple["ProgramParamInfo | HostParamInfo", tuple[str, ...]], ...]:
        """Return each value-taking parameter with the spellings its value follows.

        Program and module option spellings that take a value are included;
        a ``--no-x`` negative never takes a value. Implicitly positional
        parameters have no flag and are omitted.
        """
        result: list[tuple["ProgramParamInfo | HostParamInfo", tuple[str, ...]]] = []
        for param, projected in self.options:
            if not projected.takes_value or param.cli.cli_positional:
                continue
            result.append((param, (projected.flag, *spelling_tuple(short_flag(param)))))
        for entry, projected in self.surface_options:
            if projected.takes_value:
                result.append(
                    (
                        entry.param,
                        (*entry.positive_option_spellings, *spelling_tuple(entry.short_option)),
                    )
                )
        return tuple(result)

    def _read_tokens(
        self, tokens: Sequence[str], host_options: OptionValueMap
    ) -> tuple[frozenset[int], int, frozenset[str]]:
        """Return *tokens*' separate option-value indexes, positional count, and named parameters.

        The named parameters are the program parameters an option token
        supplies by name — the ``flagged`` set :meth:`_token_slots` takes.
        """
        arity = self.option_arity()
        spelled_names: dict[str, str] = {}
        for param, projected in self.options:
            if param.cli.cli_positional:
                continue
            for spelling in param_spellings(param, projected):
                spelled_names[spelling] = param.name

        values: set[int] = set()
        flagged: set[str] = set()
        positional = 0
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == END_OF_OPTIONS:
                positional += len(tokens) - index - 1
                break
            owned_by_host, host_claims_next = _option_token_ownership(token, host_options)
            if owned_by_host:
                index += 2 if host_claims_next and index + 1 < len(tokens) else 1
                continue
            if token.startswith("--"):
                flag, separator, _value = token.partition("=")
                if flag in spelled_names:
                    flagged.add(spelled_names[flag])
                if separator or not arity.get(flag, False):
                    index += 1
                    continue
                if index + 1 < len(tokens):
                    values.add(index + 1)
                    index += 2
                    continue
            elif token.startswith("-") and token != "-":
                short_group = token[1:]
                for offset, short in enumerate(short_group):
                    takes_value = arity.get(f"-{short}")
                    if takes_value is None:
                        continue
                    if f"-{short}" in spelled_names:
                        flagged.add(spelled_names[f"-{short}"])
                    if takes_value and offset + 1 == len(short_group) and index + 1 < len(tokens):
                        values.add(index + 1)
                        index += 2
                        break
                    if takes_value:
                        index += 1
                        break
                else:
                    index += 1
                continue
            else:
                positional += 1
            index += 1
        return frozenset(values), positional, frozenset(flagged)

    def render_help(
        self,
        program_name: str,
        *,
        description: str | None = None,
        extra_options: Sequence[click.Parameter] = (),
    ) -> str:
        """Render this command's help as the invocation *program_name* spells it.

        The usage line names *program_name* and the program's own positional
        slots; the description is the program's ``@doc`` unless *description*
        supplies one of the host's own (a package manifest's, say); and the
        options are this program's visible flags with their own ``@doc`` and
        metavars, followed by *extra_options* — recognized execution flags go
        into the final ``Execution options`` section, while other additions
        stay under ``Options``.
        """
        command = _build_click_command(
            program_name,
            params=self.params,
            description=self.command.help if description is None else description,
            usage_slots=self.command.usage_slots,
            positional_help=self.command.positional_help,
            extra_options=extra_options,
            parameter_sections=self.command.parameter_sections,
            surface_help_sections=self.command.surface_help_sections,
            ambiguous_options=self.command.ambiguous_options,
            surface_entries=self.command.surface_entries,
        )
        return _format_help(command, program_name)

    def option_spellings(self) -> tuple[str, ...]:
        """Return every completable option spelling this command accepts.

        Each visible parameter's long flag, its derived negative and its
        ``@opt-short`` spelling, then the help flags the command owns. A
        hidden parameter contributes none, so completion offers exactly the
        ``--name`` entries the help lists.
        """
        spellings: list[str] = []
        for param, projected in self.options:
            if param.cli.hidden or param.cli.cli_positional:
                continue
            spellings.extend(param_spellings(param, projected))
        for entry, _projected in self.surface_options:
            if not entry.hidden:
                spellings.extend(entry.option_spellings)
        spellings.extend(HELP_FLAGS)
        return tuple(spellings)

    def _token_slots(
        self, count: int, flagged: frozenset[str]
    ) -> "list[tuple[ProgramParamInfo | None, bool]]":
        """Pair each of *count* positional tokens with the slot it fills.

        Tokens fill :attr:`positional` left to right. A slot whose parameter
        the command line already supplied by name (*flagged*) is passed over
        when a CLI-positional slot follows, since that slot is reachable by
        position alone; otherwise the token lands on it and the shared binder
        reports the duplicate. Each pair's flag says whether the token must
        travel by name: it fills a CLI-positional slot, or a slot after a
        passed-over one, which the binder's left-to-right pairing would
        misplace. A token past the last slot pairs with ``None``.
        """
        slots = self.positional
        result: list[tuple[ProgramParamInfo | None, bool]] = []
        position = 0
        passed_over = False
        for _ in range(count):
            while (
                position < len(slots)
                and slots[position].name in flagged
                and any(slot.cli.cli_positional for slot in slots[position + 1 :])
            ):
                position += 1
                passed_over = True
            slot = slots[position] if position < len(slots) else None
            result.append((slot, slot is not None and (slot.cli.cli_positional or passed_over)))
            position += 1
        return result


def _check_reservation(
    options: "tuple[tuple[ProgramParamInfo, ProjectedOption], ...]",
    reserved_flags: frozenset[str],
) -> ProgramOptionError | None:
    """Return the first flag collision among *options*, or ``None`` when there is none.

    Every spelling a parameter claims — its ``--name``, the derived
    ``--no-name`` of a negatable form, and its ``-x`` short — is checked
    against *reserved_flags* (the invoking surface's inventory) and against
    every other parameter's own spellings, in declaration order.
    """
    seen: dict[str, str] = {}
    for param, projected in options:
        for flag in param_spellings(param, projected):
            if flag in reserved_flags:
                return ReservedFlagError(parameter=param.name, flag=flag)
            if flag in seen:
                return DuplicateOptionFlagError(
                    first_parameter=seen[flag], second_parameter=param.name, flag=flag
                )
            seen[flag] = param.name
    return None


def _ambiguous_option_callback(
    spelling: str,
    candidates: tuple[HostParamInfo, ...],
    entries: Mapping[HostParamInfo, ParamSurfaceEntry],
) -> Callable[[click.Context, click.Parameter, object], None]:
    """Return the usage callback for one ambiguous module-parameter flag."""

    def reject(ctx: click.Context, param: click.Parameter, value: object) -> None:
        del ctx, param
        if value:
            raise click.UsageError(_ambiguous_option_message(spelling, candidates, entries))

    return reject


def _ambiguous_option_message(
    spelling: str,
    candidates: tuple[HostParamInfo, ...],
    entries: Mapping[HostParamInfo, ParamSurfaceEntry],
) -> str:
    """Render one ambiguous option's declaration-aware usage diagnostic."""
    descriptions = "; ".join(
        f"{candidate.declaration_path} ({', '.join(entries[candidate].option_spellings)})"
        for candidate in candidates
    )
    return f"ambiguous option {spelling!r}: {descriptions}"


def _ambiguous_click_options(
    ambiguous: Mapping[str, tuple[HostParamInfo, ...]],
    entries: Mapping[HostParamInfo, ParamSurfaceEntry],
) -> list[click.Parameter]:
    """Build hidden rejecting options for every ambiguous module spelling."""
    return [
        click.Option(
            [f"_ambiguous{index}", spelling],
            is_flag=True,
            is_eager=True,
            expose_value=False,
            hidden=True,
            callback=_ambiguous_option_callback(spelling, candidates, entries),
        )
        for index, (spelling, candidates) in enumerate(ambiguous.items())
    ]


def build_program_command(
    program: ProgramDeclInfo,
    reserved_flags: frozenset[str],
    params: Sequence[HostParamInfo] = (),
) -> "ProgramCommand | ProgramOptionError":
    """Build *program*'s Click command, checking flag reservation first.

    *reserved_flags* is the invoking surface's inventory —
    :data:`EXEC_RESERVED_FLAGS` or :data:`REGISTERED_RESERVED_FLAGS`. A
    program parameter can never shadow one of those, nor another
    parameter's own spelling — so one parameter's ``--no-<name>`` negation
    can never silently steal a different parameter literally named
    ``no-<name>``, and two parameters can never claim the same short.
    Positional-only CLI parameters never enter the flag namespace and so are
    never checked. Returns a :class:`ReservedFlagError` or
    :class:`DuplicateOptionFlagError` for the caller to render on the first
    collision found, in declaration order.

    The returned command owns ``-h``/``--help`` as well: it carries the
    program's own ``@doc`` prose and each parameter's, and its help is the
    help a reader of this program sees.
    """
    signature = program.parameters
    positional = tuple(p for p in signature if p.kind in _POSITIONAL_ZONES or p.cli.cli_positional)
    options = tuple(
        (p, project_option(p.cli.name, p.type))
        for p in signature
        if p.kind in NAME_ADDRESSABLE_ZONES
    )
    collision = _check_reservation(
        tuple((param, projected) for param, projected in options if not param.cli.cli_positional),
        reserved_flags,
    )
    if collision is not None:
        return collision
    return _assemble_command(
        program.declaration_path,
        program.doc,
        positional,
        options,
        build_param_surface(reserved_flags, program, params),
    )


_engine_command: ProgramCommand | None = None


def engine_command() -> ProgramCommand:
    """Return the command of a host with no program: the engine settings' bare surface.

    The one engine-only command ``agm repl`` parses with, ``agm exec`` completes
    and describes without a program, and every FILE scan takes its option
    arity from. No reserved flag spells an engine-setting flag, so none is
    claimed. Built once (a manual cache: the project's mypy configuration
    disallows the ``Any`` ``functools.cache`` introduces).
    """
    global _engine_command
    if _engine_command is None:
        from agnette.agl.runtime.engine_config import engine_setting_params

        _engine_command = _assemble_command(
            "",
            None,
            (),
            (),
            build_param_surface(frozenset(), None, engine_setting_params(), bare_only=True),
        )
    return _engine_command


def engine_settings_section(*, width: int) -> str:
    """Render the ``Engine settings`` help section of the bare engine flags, wrapped to *width*."""
    formatter = click.HelpFormatter(width=width)
    for title, entries in engine_command().command.surface_help_sections:
        _write_surface_section(formatter, title, entries)
    return formatter.getvalue()


def _assemble_command(
    name: str,
    description: str | None,
    positional: "tuple[ProgramParamInfo, ...]",
    options: "tuple[tuple[ProgramParamInfo, ProjectedOption], ...]",
    surface: ParamSurface,
) -> ProgramCommand:
    """Assemble the Click command over a signature's *options* and the resolved *surface*."""
    surface_options = tuple(
        (entry, project_param_option(entry.param, entry.param.cli.name))
        for entry in surface.entries
        if entry.option_spellings or entry.param.cli.env is not None
    )
    click_params: list[click.Parameter] = [_positional_argument()]
    for index, (param, projected) in enumerate(options):
        if param.cli.cli_positional:
            if param.cli.env is not None:
                click_params.append(
                    _positive_option(
                        index,
                        param,
                        projected,
                        (f"--_program-env-{index}",),
                        environment_only=True,
                    )
                )
        else:
            click_params.extend(_click_params(index, param, projected))
    sections: dict[str, list[click.Parameter]] = {}
    help_sections: dict[str, list[tuple[ParamSurfaceEntry, ProjectedOption]]] = {}
    for offset, (entry, projected) in enumerate(surface_options, start=len(options)):
        entry_params = _surface_click_params(offset, entry, projected)
        click_params.extend(entry_params)
        sections.setdefault(entry.section, []).extend(entry_params)
        if entry.option_spellings:
            help_sections.setdefault(_help_section_title(entry), []).append((entry, projected))
    entries = {entry.param: entry for entry in surface.entries}
    click_params.extend(_ambiguous_click_options(surface.ambiguous_options, entries))
    command = _build_click_command(
        name,
        params=click_params,
        description=description,
        usage_slots=_usage_slots(positional),
        positional_help=tuple(
            (param.cli.metavar or param.cli.name, keep_indented_paragraphs(param.cli.doc))
            for param in positional
            if param.cli.doc is not None
            and not param.cli.hidden
            and (param.kind is ParamZone.POSITIONAL_ONLY or param.cli.cli_positional)
        ),
        parameter_sections=tuple(
            (section, tuple(section_params)) for section, section_params in sections.items()
        ),
        surface_help_sections=tuple(
            (section, tuple(section_entries)) for section, section_entries in help_sections.items()
        ),
        ambiguous_options=surface.ambiguous_options,
        surface_entries=entries,
    )
    return ProgramCommand(
        command=command,
        positional=positional,
        options=options,
        surface_options=surface_options,
        surface=surface,
        params=tuple(click_params),
    )


def program_command_for(
    program: ProgramDeclInfo | None,
    reserved_flags: frozenset[str],
    params: Sequence[HostParamInfo] = (),
) -> "ProgramCommand | None":
    """Build *program*'s command, degrading a missing program or a collision to ``None``.

    For the advisory surfaces (help, completion, ``-h`` disambiguation), which
    show no program-argument flags rather than raising the host diagnostic the
    execution path takes from :func:`build_program_command`: no program
    selected and a colliding projection are the same outcome there.
    """
    if program is None:
        return None
    result = build_program_command(program, reserved_flags, params)
    return result if isinstance(result, ProgramCommand) else None


def _fallback_command() -> click.Command:
    """Return a command that owns ``-h``/``--help`` and treats every other token as positional.

    The stand-in for a program whose command could not be built — its source
    does not compile, no single program is selected, or its flags collide —
    so a help request is still recognized by Click's own parsing rather than
    by a token scan of the host's own.
    """
    return _build_click_command(
        "",
        params=[_positional_argument()],
        description=None,
        usage_slots=(),
        context_settings={"ignore_unknown_options": True},
    )


def program_help_requested(
    tokens: "Sequence[str]", program_command: "ProgramCommand | None"
) -> bool:
    """Return whether *tokens* ask *program_command* for its help.

    Click decides, so every convention it owns applies: a ``-h`` bundled into
    a short group is a help request, one standing where a value-taking option
    expects its value is that value, and one past the end-of-options marker is
    a positional argument. A token stream Click rejects outright (an unknown
    option before the help flag) is no help request either — the host runs it
    and reports the usage error.
    """
    command = _fallback_command() if program_command is None else program_command.command
    try:
        command.make_context(command.name, list(tokens))
    except _HelpSignal:
        return True
    except click.ClickException:
        return False
    return False


def _format_help(command: click.Command, program_name: str) -> str:
    """Render *command*'s help as it reads under the invocation *program_name*."""
    return command.get_help(click.Context(command, info_name=program_name)) + "\n"


def render_program_help(
    program_command: "ProgramCommand | None",
    *,
    program_name: str,
    description: str | None = None,
    extra_options: "Sequence[click.Parameter]" = (),
) -> str:
    """Render the help of the command *program_name* invokes.

    Without a usable *program_command* — an undiscoverable program, or one
    whose flags collide — the same surface is rendered without any program
    flags, so a host that can still describe the command (its own options and
    a description of its own) presents one help layout, not two.
    """
    if program_command is not None:
        return program_command.render_help(
            program_name,
            description=description,
            extra_options=extra_options,
        )
    command = _build_click_command(
        program_name,
        params=(),
        description=description,
        usage_slots=(),
        extra_options=extra_options,
    )
    return _format_help(command, program_name)


def exec_program_help(
    program_command: "ProgramCommand | None",
    *,
    file: str | None,
    program: str | None,
    extra_options: "Sequence[click.Parameter]" = (),
) -> str:
    """Render the help of the ``agm exec`` invocation *program_command* was selected by.

    The one rendering shared by the pre-parse help path (``cli._exec_print_help``)
    and the one reached through :class:`ProgramHelpRequested`
    (``commands.exec``), so a help request answered before parsing and one
    recognized while parsing produce the same page.
    """
    return render_program_help(
        program_command,
        program_name=exec_program_name(file=file, program=program),
        extra_options=extra_options,
    )


def exec_program_name(*, file: str | None, program: str | None) -> str:
    """Return the ``agm exec`` invocation a selected program's usage line is spelled with.

    Names the source the reader gave — a path or an installed reference, or
    the ``-c`` option for inline text, whose program text would not read as a
    usage line — and the ``-p`` selection when one was made, so the usage
    line stands for the command that was actually run.
    """
    parts = ["agm", "exec", "-c SOURCE" if file is None else file]
    if program is not None:
        parts.extend(("-p", program))
    return " ".join(parts)
