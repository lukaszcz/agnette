"""Pure CLI projection of host-parameter types: flags, value forms and raw values.

:func:`project_option` maps one name and AgL type onto its CLI surface (the Click
side is :mod:`agnette.agm.cli_support.program_options`): ``bool``
becomes a bare ``--x``/``--no-x`` pair, ``Option[T]``/``Optional[T]`` a
value-taking pair, ``text`` a verbatim ``VALUE``, standard ``Agent`` host Agent
syntax, and every other type one strict-JSON ``VALUE``. Whether a type is the
standard ``Option`` or ``Optional`` is decided by nominal provenance, not name.
This is the single derivation of every flag, so the CLI, completion, config
resolution and the claim-level table can never disagree. Free of Click.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agnette.agl.runtime.arguments import OptionSome, option_none_raw
from agnette.agl.runtime.serialize import dumps_exact
from agnette.agl.runtime.types import HostParamInfo, HostParamKind, ProgramParamInfo
from agnette.agl.semantics.types import (
    BoolType,
    JsonType,
    TextType,
    is_negatable_host_type,
    is_standard_agent_enum,
    is_standard_option_enum,
    is_standard_optional_enum,
)
from agnette.config.engine_keys import REGISTER_ENABLING_ENGINE_KEYS
from agnette.core.parse import normalize_option_text

if TYPE_CHECKING:
    from agnette.agl.semantics.types import Type as AglType

__all__ = [
    "ProjectedOption",
    "ValueForm",
    "is_nullable_value_form",
    "is_optional_default",
    "native_raw_value",
    "param_spellings",
    "project_option",
    "project_param_option",
    "short_flag",
    "spelling_tuple",
    "token_raw_value",
]

# The value text that supplies ``Optional::Default`` rather than a value.
_OPTIONAL_DEFAULT_KEYWORD = "default"


def spelling_tuple(spelling: str | None) -> tuple[str, ...]:
    """Return *spelling* as a zero- or one-element tuple, for splicing into flag lists."""
    return () if spelling is None else (spelling,)


class ValueForm(enum.Enum):
    """How a projected option's ``VALUE`` becomes a raw decode input."""

    #: ``text``: the token, verbatim.
    TEXT = "text"
    #: ``Option[T]``: a ``{"$case": ...}`` envelope wrapping ``T``'s own value.
    OPTION = "option"
    #: ``Optional[T]``: ``Option[T]`` plus the reserved ``default`` value.
    OPTIONAL = "optional"
    #: ``bool``: a native Python bool, no ``VALUE`` token.
    BOOL = "bool"
    #: Standard ``Agent``: compact syntax, command text, a constructor call, or
    #: canonical JSON.
    AGENT = "agent"
    #: ``json``: one JSON token.
    JSON = "json"
    #: Every other type: one strict-JSON-or-AgL-value-syntax token.
    VALUE = "value"


@dataclass(frozen=True, slots=True)
class ProjectedOption:
    """One type's CLI surface, as :func:`project_option` derives it.

    ``value_form`` is the one stored discriminant: ``negative_flag`` and
    ``takes_value`` both follow from it, so :attr:`takes_value` is derived
    rather than stored and cannot disagree with the form. A form with no
    negation spells no ``negative_flag`` at all.

    ``option_inner``/``option_inner_form`` are ``Option[T]`` or ``Optional[T]``'s
    own ``T`` and ``T``'s bare value form for either optional value form,
    and ``None`` for every other form — computed once here rather than
    re-derived from the parameter's type on every token, so the raw-value
    builder and the metavar never have to re-classify ``T`` themselves.
    """

    flag: str
    negative_flag: str | None
    value_form: ValueForm
    option_inner: AglType | None = None
    option_inner_form: ValueForm | None = None

    @property
    def takes_value(self) -> bool:
        return self.value_form is not ValueForm.BOOL


def _optional_projection(type_: AglType) -> tuple[ValueForm, AglType] | None:
    """Return the CLI form and inner type for a standard optional enum."""
    if is_standard_option_enum(type_):
        form = ValueForm.OPTION
    elif is_standard_optional_enum(type_):
        form = ValueForm.OPTIONAL
    else:
        return None
    return (form, type_.type_args[0]) if type_.type_args else None


def is_nullable_value_form(form: ValueForm) -> bool:
    """Return whether *form* has value and negative CLI polarities."""
    return form in (ValueForm.OPTION, ValueForm.OPTIONAL)


def _bare_value_form(type_: AglType) -> ValueForm:
    """Return the CLI value form for a bare type: never ``bool`` or an optional enum.

    An optional enum's inner ``T`` shares this mapping with a bare parameter.
    """
    if isinstance(type_, TextType):
        return ValueForm.TEXT
    if is_standard_agent_enum(type_):
        return ValueForm.AGENT
    if isinstance(type_, JsonType):
        return ValueForm.JSON
    return ValueForm.VALUE


def project_option(name: str, type_: AglType) -> ProjectedOption:
    """Project one *name*/*type_* pair (a program parameter or an engine key) onto its CLI flags.

    *name* is the option's external spelling: an engine key's own name, or a
    program parameter's ``@opt-name`` (defaulting to its declared name).
    """
    optional = _optional_projection(type_)
    inner: AglType | None = None
    inner_form: ValueForm | None = None
    if isinstance(type_, BoolType):
        form = ValueForm.BOOL
    elif optional is not None:
        form, inner = optional
        inner_form = _bare_value_form(inner)
    else:
        form = _bare_value_form(type_)
    return ProjectedOption(
        flag=f"--{name}",
        negative_flag=f"--no-{name}" if is_negatable_host_type(type_) else None,
        value_form=form,
        option_inner=inner if is_nullable_value_form(form) else None,
        option_inner_form=inner_form,
    )


def project_param_option(param: HostParamInfo, name: str) -> ProjectedOption:
    """Project *param* under the external spelling *name*.

    An engine setting that turns its register on has no negative: the
    register's own switch (``--no-trace``) turns it off.
    """
    projected = project_option(name, param.type)
    if param.kind is HostParamKind.ENGINE and param.name in REGISTER_ENABLING_ENGINE_KEYS:
        return replace(projected, negative_flag=None)
    return projected


def _option_some_raw(value: object) -> OptionSome:
    """Box one raw ``Option[T]`` "Some" value for ``decode_param_value`` to decode.

    The single place this box is built, shared by :func:`token_raw_value` (a CLI
    ``VALUE`` token) and :func:`native_raw_value` (an already-native TOML/JSON
    config value). Decoding *value* (host-text dispatch when it is a string, as
    is otherwise) and wrapping it into the enum's ``{"$case": "Some", "value": ...}``
    JSON shape happens at decode time against the ``Some`` variant's own field
    type, not here.
    """
    return OptionSome(value)


def _optional_default_raw() -> object:
    """Return the ``Default`` envelope ``decode_param_value`` expects for ``Optional[T]``."""
    return {"$case": "Default"}


def is_optional_default(projected: ProjectedOption, raw: object) -> bool:
    """Return whether *raw* is the exact ``default`` keyword of an ``Optional`` slot.

    The one spelling of that rule: the keyword supplies ``Optional::Default``
    rather than a value, so it is neither decoded nor read as a path.
    """
    return projected.value_form is ValueForm.OPTIONAL and raw == _OPTIONAL_DEFAULT_KEYWORD


def token_raw_value(projected: ProjectedOption, token: str) -> object:
    """Build the raw value for one ``VALUE`` token: a flag's, or a positional slot's.

    The one reading of a value token, so a parameter's flag, its ``@opt-env``
    variable and its positional slot accept the same spellings. Every
    non-optional form takes its token verbatim: ``decode_param_value`` reads a
    string raw value through the shared host-text dispatch, so parsing it
    here would duplicate that step and lose its diagnostic. An optional token
    is boxed ``Some`` for the same deferred decode against ``T``'s own field
    type, except for ``Optional``'s exact ``default`` shortcut. A ``bool`` flag
    takes no token and never reaches here; a ``bool`` slot's token is verbatim.
    The envelope rule is spelled here, beside :func:`native_raw_value`, so no
    surface restates it.
    """
    if is_optional_default(projected, token):
        return _optional_default_raw()
    if projected.option_inner is not None:
        return _option_some_raw(token)
    return token


def native_raw_value(
    projected: ProjectedOption, raw: object, *, option_text: bool = False
) -> object:
    """Project one already-native host value onto *projected*'s raw argument shape.

    The config-table counterpart of :func:`token_raw_value`, which applies the
    same optional-enum rule to a CLI ``VALUE`` token. A present value is
    normally wrapped ``Some``: a config table has no
    ``--no-x`` equivalent, so an absent key supplies nothing at all and the
    parameter falls back to its own default. The exact ``"default"`` string
    supplies ``Optional::Default``. A native string for a slot whose
    type is ``json`` (the parameter's own type, or ``Option[T]``'s inner
    ``T``) is encoded into JSON text, preserving its distinction from AgL
    value syntax; every other native string is host text, decoded the same
    way a CLI token or ``@opt-env`` value is; a non-string native is already
    decoded data. With *option_text* (an ``Option[text]`` slot), *raw* reads as
    :func:`~agnette.core.parse.normalize_option_text` says: the explicit ``None``
    envelope, or ``Some`` of the normalized text.
    """
    if is_optional_default(projected, raw):
        return _optional_default_raw()
    if is_nullable_value_form(projected.value_form):
        if option_text:
            text = normalize_option_text(raw)
            return option_none_raw() if text is None else _option_some_raw(text)
        value = raw
        if isinstance(raw, str) and projected.option_inner_form is ValueForm.JSON:
            value = dumps_exact(raw, indent=None)
        return _option_some_raw(value)
    if projected.value_form is ValueForm.JSON and isinstance(raw, str):
        return dumps_exact(raw, indent=None)
    return raw


def short_flag(param: ProgramParamInfo | HostParamInfo) -> str | None:
    """Return *param*'s ``-x`` short spelling, or ``None`` when it declares none."""
    short = param.cli.short
    return None if short is None else f"-{short}"


def param_spellings(
    param: ProgramParamInfo | HostParamInfo, *projections: ProjectedOption
) -> list[str]:
    """Return every flag one parameter claims: its positives, its negatives, and its short.

    The one enumeration of a parameter's CLI spellings, so the reservation
    check (``program_options._check_reservation``), completion
    (``ProgramCommand.option_spellings``) and the module-parameter surface
    (:mod:`agnette.host_params.surface`) can never disagree about what a parameter
    occupies. A module parameter is projected once per qualified route it
    answers to; every other parameter has the one projection of its own name.
    """
    return [
        *(
            flag
            for projected in projections
            for flag in (projected.flag, *spelling_tuple(projected.negative_flag))
        ),
        *spelling_tuple(short_flag(param)),
    ]
