"""Package Python requirements: syncing, plus checks for ``pkg check`` and companion loading.

The activation index is the source of truth. A global store's active packages
are synced into the running interpreter's environment as one union of their
``[python] dependencies``; nothing is ever uninstalled. A project store's
packages instead get a per-project *overlay* holding that union, rebuilt on
activation changes (unless the union and the pins are as built), so project
requirements never alter AgM's own environment.

The overlay is the symlink :attr:`PackageStore.python_overlay_path` to a
generation directory ``.python-<token>`` beside it, which also records the
requirements and the constraints it was built under. A rebuild installs into a
new generation under constraints pinning every distribution of AgM's
interpreter; the caller publishes the activation; only then does one atomic
rename of a fresh link switch the overlay. An overlay whose distributions differ
in version from the interpreter's (the interpreter changed since the build) is
*stale*: it is neither used nor consulted, and its requirements count as
unsatisfied until ``pkg sync --project`` rebuilds it.

A process fixes one generation per store, takes a shared ``flock`` on its
:data:`GENERATION_LOCK` for its lifetime, and puts it ahead of AgM's libraries
on ``sys.path`` only for the companions of packages whose requirements it holds.
A rebuild deletes an old generation only when it can lock it exclusively, so a
running process never loses the generation it imports from; one still held is
left for a later rebuild.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import site
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from agnette.config import context as config_context
from agnette.core import dry_run, fs, pyenv
from agnette.packages.activation import activating_store
from agnette.packages.layout import StoreScope
from agnette.packages.model import PackageInfo
from agnette.packages.store import PackageStore, context_layers, global_store, project_store

#: A generation's lock file: processes using it hold it shared, a deleter exclusive.
GENERATION_LOCK = ".lock"
_GENERATION_PREFIX = ".python-"
_NEW_LINK = ".python.new"
_SPECS = ".specs.txt"
_CONSTRAINTS = ".constraints.txt"

# Process-wide state: the generation fixed per store root, the staleness verdict
# per generation, and the shared-lock descriptors held per generation.
_fixed: dict[Path, Path | None] = {}
_stale: dict[Path, bool] = {}
_held: dict[Path, int] = {}


def python_dependencies(packages: Iterable[PackageInfo]) -> tuple[str, ...]:
    """Union of *packages*' requirements in package then manifest order; duplicates once."""

    specs: dict[str, None] = {}
    for package in packages:
        for spec in package.manifest.python_dependencies:
            specs[spec] = None
    return tuple(specs)


def unsatisfied_python_dependencies(
    specs: Iterable[str], overlay: Path | None = None
) -> tuple[tuple[str, pyenv.RequirementStatus], ...]:
    """Each of *specs* the environment does not satisfy, with its status.

    The environment is that of :func:`requirement_status`.
    """

    statuses = ((spec, requirement_status(spec, overlay)) for spec in specs)
    return tuple((spec, status) for spec, status in statuses if not status.satisfied)


def requirement_status(spec: str, overlay: Path | None = None) -> pyenv.RequirementStatus:
    """Status of *spec* in the *overlay* directory when given, then the interpreter.

    A stale *overlay* satisfies nothing.
    """

    if overlay is not None and overlay_is_stale(overlay):
        return pyenv.RequirementStatus(pyenv.RequirementState.STALE)
    return pyenv.requirement_status(spec, overlay)


def sync_python_dependencies(specs: Sequence[str]) -> tuple[str, ...]:
    """Install all *specs* jointly when any is unsatisfied; return the unsatisfied ones.

    The whole union goes to the installer so it resolves every requirement
    together, failing on conflicts rather than downgrading a satisfied one.
    Honours dry-run; raises :class:`~agnette.core.pyenv.RequirementInstallError`.
    """

    unsatisfied = tuple(spec for spec, _ in unsatisfied_python_dependencies(specs))
    if unsatisfied:
        pyenv.install_requirements(specs)
    return unsatisfied


def requirement_store(package: PackageInfo) -> PackageStore | None:
    """The store whose sync provides *package*'s requirements, in the current context.

    A package resolved from a store belongs to it. Any other (a development
    checkout, which carries no scope) belongs to the store whose activation
    selects it, if one does.
    """

    context = config_context.current_config_context()
    if package.store_scope is StoreScope.PROJECT:
        return None if context.proj_dir is None else project_store(context.proj_dir)
    if package.store_scope is StoreScope.GLOBAL:
        return global_store(home=context.home)
    return activating_store(context_layers(home=context.home, proj_dir=context.proj_dir), package)


def overlay_for(package: PackageInfo) -> Path | None:
    """The overlay generation this process uses for *package*, if its store is a project's.

    The first lookup per store fixes the generation (see
    :func:`_process_generation`), so checks and imports agree for the process's
    lifetime. ``None`` when the store has no overlay.
    """

    store = requirement_store(package)
    if store is None or store.scope is not StoreScope.PROJECT:
        return None
    return _process_generation(store)


def overlay_is_stale(overlay: Path) -> bool:
    """Whether *overlay* holds a distribution at a different version than the interpreter.

    The interpreter's distributions are read without the store's generations.
    The verdict is fixed for the process.
    """

    if overlay not in _stale:
        built = pyenv.installed_versions([str(overlay)])
        interpreter = pyenv.installed_versions(_outside_generations(overlay.resolve().parent))
        _stale[overlay] = any(
            name in interpreter and interpreter[name] != version for name, version in built.items()
        )
    return _stale[overlay]


def prepend_overlay_to_path(overlay: Path) -> None:
    """Put *overlay* ahead of the interpreter's libraries on ``sys.path``, once.

    The overlay and the directories its ``.pth`` files add (wherever they insert)
    go before the first site-packages entry (last if there is none), after the
    standard library.
    """

    entry = str(overlay.resolve())
    if entry in sys.path:
        return
    previous = list(sys.path)
    site.addsitedir(entry)
    added = [item for item in sys.path if item not in previous]
    sys.path[:] = [item for item in sys.path if item in previous]
    libraries = {*site.getsitepackages(), site.getusersitepackages()}
    position = next((i for i, item in enumerate(sys.path) if item in libraries), len(sys.path))
    sys.path[position:position] = added


@contextmanager
def rebuilt_python_overlay(
    store: PackageStore, packages: Iterable[PackageInfo], *, force: bool = False
) -> Iterator[tuple[str, ...]]:
    """Build *store*'s overlay for *packages*' requirements, yield those installed, then switch.

    Nothing is built, and nothing yielded, when the current generation was
    built from these very requirements under the interpreter's current pins,
    unless *force*. The caller holds the store lock and publishes the
    activation in the body. The build happens first, and a failure (including
    an interrupt) leaves the previous overlay; so does a body that raises,
    which discards the build. After the body, one rename switches the overlay,
    so it is never absent while being replaced. An empty union builds nothing
    and removes the overlay. Dry-run prints what would run and changes
    nothing. A switch that fails after the body is only warned about: the
    activation is already published, and ``pkg sync --project`` rebuilds the
    overlay. Raises :class:`~agnette.core.pyenv.RequirementInstallError` before
    the body.
    """

    specs = python_dependencies(packages)
    unchanged = not force and _built_from(store, specs)
    generation = None if unchanged else _build_generation(store, specs)
    try:
        yield () if unchanged else specs
    except BaseException:
        if generation is not None:
            _discard(store, generation)
        raise
    try:
        if generation is not None:
            os.replace(store.root / _NEW_LINK, store.python_overlay_path)
        elif not specs:
            _remove_overlay(store)
    except OSError as exc:
        print(
            f"warning: cannot switch {store.python_overlay_path}: {exc}; "
            "run `agm pkg sync --project` to rebuild it",
            file=sys.stderr,
        )


def _built_from(store: PackageStore, specs: Sequence[str]) -> bool:
    """Whether the current generation records exactly *specs* and the interpreter's current pins.

    Equal pins mean the overlay holds no other version of what the interpreter
    has, so a generation passing this is not stale.
    """

    generation = store.python_overlay_path.resolve()
    try:
        recorded = (
            tuple((generation / _SPECS).read_text("utf-8").splitlines()),
            tuple((generation / _CONSTRAINTS).read_text("utf-8").splitlines()),
        )
    except FileNotFoundError:
        return False
    return bool(specs) and recorded == (tuple(specs), _pins(store))


def _build_generation(store: PackageStore, specs: Sequence[str]) -> Path | None:
    """Clear leftovers, then install *specs* into a new generation linked at ``.python.new``.

    The generation records its requirements and constraints. Returns it, or
    ``None`` when nothing is built: no *specs*, or dry-run.
    """

    overlay = store.python_overlay_path
    try:
        _clear_leftovers(store)
        if not specs:
            return None
        if dry_run.enabled():
            pyenv.install_requirements(specs, target=overlay)
            return None
        generation = store.root / f"{_GENERATION_PREFIX}{secrets.token_hex(4)}"
        generation.mkdir()
        try:
            (generation / GENERATION_LOCK).touch()
            constraints = generation / _CONSTRAINTS
            constraints.write_text("".join(f"{pin}\n" for pin in _pins(store)), "utf-8")
            (generation / _SPECS).write_text("".join(f"{spec}\n" for spec in specs), "utf-8")
            pyenv.install_requirements(specs, target=generation, constraints=constraints)
            (store.root / _NEW_LINK).symlink_to(generation.name)
        except BaseException:
            _discard(store, generation)
            raise
        return generation
    except OSError as exc:
        raise pyenv.RequirementInstallError(f"cannot build {overlay}: {exc}") from exc


def _generations(store: PackageStore) -> list[Path]:
    return sorted(store.root.glob(f"{_GENERATION_PREFIX}*"))


def _clear_leftovers(store: PackageStore) -> None:
    """Remove every unused generation but the current one, and a link left half-made."""

    link = store.root / _NEW_LINK
    if link.is_symlink():
        fs.unlink(link)
    current = store.python_overlay_path.resolve()
    for generation in _generations(store):
        if generation != current:
            _remove_unused(generation)


def _discard(store: PackageStore, generation: Path) -> None:
    """Remove a built *generation* and its link."""

    fs.unlink(store.root / _NEW_LINK, missing_ok=True)
    fs.rmtree(generation)


def _remove_overlay(store: PackageStore) -> None:
    """Remove the overlay link and every unused generation."""

    if store.python_overlay_path.is_symlink():
        fs.unlink(store.python_overlay_path)
    for generation in _generations(store):
        _remove_unused(generation)


def _remove_unused(generation: Path) -> None:
    """Remove *generation* unless a process holds its shared lock (a later rebuild retries).

    One without a lock file was never used.
    """

    fd = _open_lock(generation)
    try:
        if fd is None or _flock(fd, fcntl.LOCK_EX):
            fs.rmtree(generation)
    finally:
        if fd is not None:
            os.close(fd)


def _open_lock(generation: Path) -> int | None:
    try:
        return os.open(generation / GENERATION_LOCK, os.O_RDONLY)
    except FileNotFoundError:
        return None


def _flock(fd: int, operation: int) -> bool:
    """Take the lock *operation* on *fd* without waiting; whether it was taken."""

    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _process_generation(store: PackageStore) -> Path | None:
    """The generation this process uses for *store*, fixed on first use.

    One of the store's already on ``sys.path`` is reused. Otherwise the current
    one is locked shared for the process's lifetime, so no rebuild deletes it.
    Resolving is retried once, as a rebuild may delete the generation between
    resolving the link and locking it; ``None`` when there is none to lock.
    """

    root = store.root.resolve()
    if root not in _fixed:
        _fixed[root] = _on_sys_path(root) or _hold_current(store)
    return _fixed[root]


def _on_sys_path(root: Path) -> Path | None:
    return next((g for entry in sys.path if (g := _generation_of(entry, root))), None)


def _hold_current(store: PackageStore) -> Path | None:
    for _ in range(2):
        generation = store.python_overlay_path.resolve()
        fd = _open_lock(generation)
        if fd is None:
            continue
        if _flock(fd, fcntl.LOCK_SH):
            _held[generation] = fd
            return generation
        os.close(fd)
    return None


def _generation_of(entry: str, root: Path) -> Path | None:
    """The generation directory of the store at *root* that *entry* is, or lies within."""

    path = Path(entry).resolve()
    return next(
        (
            p
            for p in (path, *path.parents)
            if p.parent == root and p.name.startswith(_GENERATION_PREFIX)
        ),
        None,
    )


def _outside_generations(root: Path) -> list[str]:
    """``sys.path`` without the entries within the generations of the store at *root*."""

    return [entry for entry in sys.path if _generation_of(entry, root) is None]


def _pins(store: PackageStore) -> tuple[str, ...]:
    """Pins of the distributions of AgM's interpreter, leaving out *store*'s own overlay."""

    return pyenv.installed_pins(_outside_generations(store.root.resolve()))
