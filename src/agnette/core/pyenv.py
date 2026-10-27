"""Python requirements of AgM's own interpreter environment.

Package companions run in the interpreter running AgM, so their third-party
requirements are checked against, and installed into, ``sys.executable``'s
environment. A project's requirements may instead be installed into an
*overlay* directory ahead of AgM's own libraries: a check consults the overlay's
distributions first, and :func:`installed_pins` lets the installer keep the
overlay from holding a different version of any distribution AgM has.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path

from packaging.markers import (
    EvaluateContext,
    Marker,
    UndefinedComparison,
    UndefinedEnvironmentName,
)
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from agnette.core import process


class RequirementInstallError(RuntimeError):
    """Raised when the installer cannot start or exits unsuccessfully."""


def install_requirements(
    specs: Sequence[str], *, target: Path | None = None, constraints: Path | None = None
) -> None:
    """Install PEP 508 *specs* with ``uv`` when on ``PATH``, else ``pip``; honours dry-run.

    Installs into the interpreter's environment, or into the *target* directory
    when given, and holds versions to the pins of the *constraints* file.
    """

    if not specs:
        return
    uv = shutil.which("uv")
    installer = (
        [uv, "pip", "install", "--python", sys.executable]
        if uv is not None
        else [sys.executable, "-m", "pip", "install"]
    )
    options = [] if target is None else ["--target", str(target)]
    if constraints is not None:
        options += ["-c", str(constraints)]
    cmd = [*installer, *options, *specs]
    hint = "" if uv is not None else f" (installing needs uv on PATH or pip in {sys.executable})"
    try:
        returncode = process.run_foreground_unless_dry_run(cmd)
    except OSError as exc:
        raise RequirementInstallError(f"cannot run {cmd[0]}: {exc}{hint}") from exc
    if returncode != 0:
        raise RequirementInstallError(f"{cmd[0]} exited with status {returncode}{hint}")


def installed_versions(path: Sequence[str] | None = None) -> dict[str, str]:
    """Version of every distribution installed on *path*, by canonical name.

    *path* defaults to ``sys.path``; where a distribution appears on several
    entries, the first (the one an import finds) counts. A distribution
    without a name or version is skipped.
    """

    versions: dict[str, str] = {}
    for distribution in metadata.distributions(path=list(sys.path if path is None else path)):
        name = distribution.metadata.get("Name")
        version = distribution.metadata.get("Version")
        if name is not None and version is not None:
            versions.setdefault(canonicalize_name(name), version)
    return versions


def installed_pins(path: Sequence[str] | None = None) -> tuple[str, ...]:
    """Pins of every distribution installed on *path* (see :func:`installed_versions`), by name.

    A version that is not PEP 440 is pinned by arbitrary equality
    (``name===version``). Raises :class:`RequirementInstallError` when none is
    installed: pins that pin nothing would silently void their purpose.
    """

    versions = installed_versions(path)
    if not versions:
        raise RequirementInstallError(
            "cannot read the distributions installed in AgM's interpreter"
        )
    return tuple(
        f"{name}{_pin_operator(version)}{version}" for name, version in sorted(versions.items())
    )


def _pin_operator(version: str) -> str:
    try:
        Version(version)
    except InvalidVersion:
        return "==="
    return "=="


def marker_value(
    marker: Marker,
    environment: dict[str, str] | None = None,
    *,
    context: EvaluateContext = "metadata",
) -> bool | None:
    """*marker*'s value; ``None`` when it names an undefined variable or compares incomparably."""

    try:
        return marker.evaluate(environment, context=context)
    except (KeyError, UndefinedComparison, UndefinedEnvironmentName):
        return None


class RequirementState(Enum):
    """How a requirement stands in the running interpreter's environment."""

    INSTALLED = "installed"
    UNSATISFIED = "unsatisfied"
    MISSING = "missing"
    NOT_APPLICABLE = "not applicable"
    STALE = "stale overlay"


@dataclass(frozen=True, slots=True)
class RequirementStatus:
    """A requirement's state and its distribution's installed version, if any."""

    state: RequirementState
    version: str | None = None

    @property
    def satisfied(self) -> bool:
        return self.state in (RequirementState.INSTALLED, RequirementState.NOT_APPLICABLE)

    def describe(self) -> str:
        """Human-readable status, naming the installed version when there is one."""

        state: str = self.state.value
        if self.version is None:
            return state
        suffix = " (unsatisfied)" if self.state is RequirementState.UNSATISFIED else ""
        return f"installed {self.version}{suffix}"


def requirement_status(spec: str, overlay: Path | None = None) -> RequirementStatus:
    """Status of a manifest's PEP 508 *spec* in the running interpreter's environment.

    A distribution installed in the *overlay* directory, which a run puts ahead
    of the interpreter's libraries, is looked up there first.

    A marker is evaluated as a requirement's (no ``extra``). An installed
    pre-release matches when the specifier admits it, as an installer treats it.
    Each requested extra holds when every requirement the distribution declares
    only for it is satisfied, extras included.
    """

    requirement = Requirement(spec)
    marker = requirement.marker
    if marker is not None and not marker.evaluate(context="requirement"):
        return RequirementStatus(RequirementState.NOT_APPLICABLE)
    installed = _installed_version(requirement.name, overlay)
    if installed is None:
        return RequirementStatus(RequirementState.MISSING)
    holds = _installed_holds(requirement, installed, set(), overlay)
    return RequirementStatus(
        RequirementState.INSTALLED if holds else RequirementState.UNSATISFIED, installed
    )


def _holds(requirement: Requirement, seen: set[tuple[str, str]], overlay: Path | None) -> bool:
    """Whether *requirement*'s version and extras hold, ignoring its marker."""

    installed = _installed_version(requirement.name, overlay)
    return installed is not None and _installed_holds(requirement, installed, seen, overlay)


def _installed_holds(
    requirement: Requirement, installed: str, seen: set[tuple[str, str]], overlay: Path | None
) -> bool:
    """Whether *installed* meets *requirement*'s specifier and its extras hold.

    *seen* holds the ``(distribution, extra)`` pairs already being checked. A
    declared requirement that cannot be parsed or evaluated is not satisfied.
    """

    if not requirement.specifier.contains(installed, prereleases=True):
        return False
    name = canonicalize_name(requirement.name)
    for extra in sorted(requirement.extras):
        key = (name, canonicalize_name(extra))
        if key in seen:
            continue
        seen.add(key)
        for declared in _declared_requirements(requirement.name, overlay):
            try:
                dependency = Requirement(declared)
            except InvalidRequirement:
                return False
            marker = dependency.marker
            if marker is None:
                continue
            with_extra = marker_value(marker, {"extra": extra})
            without_extra = marker_value(marker, {"extra": ""})
            if with_extra is None or without_extra is None:
                return False
            if with_extra and not without_extra and not _holds(dependency, seen, overlay):
                return False
    return True


def _overlay_distribution(name: str, overlay: Path | None) -> metadata.Distribution | None:
    if overlay is None:
        return None
    return next(iter(metadata.distributions(name=name, path=[str(overlay)])), None)


def _installed_version(name: str, overlay: Path | None) -> str | None:
    distribution = _overlay_distribution(name, overlay)
    if distribution is not None:
        return distribution.version
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _declared_requirements(name: str, overlay: Path | None) -> Sequence[str]:
    distribution = _overlay_distribution(name, overlay)
    if distribution is not None:
        return distribution.requires or ()
    return metadata.requires(name) or ()
