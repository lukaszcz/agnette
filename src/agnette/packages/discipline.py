"""Filesystem and command discipline for package directories and archives."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, TypeVar

from agnette.agl.diagnostics import AglError
from agnette.agl.ir.static_keys import (
    FunctionParamKey,
    StaticBindingKey,
    host_param_label,
    static_binding_key,
)
from agnette.agl.modules.ids import ModuleId
from agnette.agl.modules.loader import ModuleGraph, build_repl_graph
from agnette.agl.modules.roots import RootSet
from agnette.agl.parser.parser import parse_program_seeded
from agnette.agl.scope import BuiltinKind, ModuleResolution, resolve_program
from agnette.agl.syntax.nodes import (
    Call,
    FuncDef,
    ImportDecl,
    Program,
    scope_names,
    static_function_items,
)
from agnette.agl.syntax.resources import ResourceError, resolve_resource, resource_path
from agnette.agl.syntax.types import UnitT
from agnette.agl.syntax.visitor import walk
from agnette.agl.zones import NAME_ADDRESSABLE_ZONES
from agnette.command_catalog import RESERVED_CONFIG_SECTION_NAMES, invalid_command_path
from agnette.config.qualified_keys import (
    display_table_path,
    full_spelling_collision,
    param_spellings_for,
)
from agnette.core import fs
from agnette.core.toml import TomlDict
from agnette.packages.command_registry import (
    CommandRegistry,
    ProgramTarget,
    command_registry,
    split_command_address,
)
from agnette.packages.distribution import MANIFEST_NAME, distribution_files
from agnette.packages.errors import DisciplineError as DisciplineError
from agnette.packages.layout import MODULE_TREE_DIRNAME
from agnette.packages.manifest import (
    PackageManifest,
    describe_unknown_fields,
    distribution_manifest,
    expanded_commands,
    program_reference,
    registered_command_paths,
    split_program_reference,
)
from agnette.packages.model import PackageInfo, is_std_package_name
from agnette.stdlib_locator import shipped_stdlib_root
from agnette.util.graph import reachable_nodes
from agnette.util.ident import is_identifier
from agnette.util.text import normalize_newlines

if TYPE_CHECKING:
    from agnette.agl.semantics.type_table import TypeTable

T = TypeVar("T")
_ModuleResolutions = Mapping[ModuleId, ModuleResolution]


@dataclass(frozen=True, slots=True)
class PackageResolution:
    """One name resolution of a package's module tree, reusable by later checks.

    Validating what a package *ships* needs the same names as validating the
    tree it ships from, so the resolution is handed on rather than recomputed:
    a distribution is that tree minus excluded files, and no module in it is
    ever loaded or resolved twice. ``all_resolutions`` covers every module the
    graph reached — package, dependencies, and ``std`` — for checks over the
    whole dependency closure (manifest ``[config]`` parameter spellings);
    ``resolutions`` is its restriction to ``modules``, the package's own.
    """

    package: PackageInfo
    modules: Mapping[ModuleId, Path] = field(default_factory=dict)
    all_resolutions: _ModuleResolutions = field(default_factory=dict)
    adjacency: Mapping[ModuleId, tuple[ModuleId, ...]] = field(default_factory=dict)
    # Source-authored edges only: a program's closure, as at run time.
    source_adjacency: Mapping[ModuleId, tuple[ModuleId, ...]] = field(default_factory=dict)
    digests: Mapping[ModuleId, str] = field(default_factory=dict)

    @property
    def resolutions(self) -> _ModuleResolutions:
        """This package's own module resolutions, restricted from ``all_resolutions``."""
        return {module_id: self.all_resolutions[module_id] for module_id in self.modules}


def validate_package(
    package: PackageInfo, *, dependency_packages: Iterable[PackageInfo] = ()
) -> PackageResolution:
    """Validate a package's module tree, commands, imports, resources, and references.

    The resolution it performed is returned so a caller that goes on to check
    this package's distribution reuses it.
    """

    _validate_manifest_schema(package.manifest)
    modules = validate_package_structure(package)
    dependencies = tuple(dependency_packages)
    resolution = _resolve_package_modules(package, modules, dependency_packages=dependencies)
    _validate_inherit_addresses(resolution, dependency_packages=dependencies)
    _validate_resolved_package(
        package.manifest,
        modules,
        resolution,
        exists=lambda relative: _resource_exists(package.root, relative),
    )
    return resolution


def _validate_resolved_package(
    manifest: PackageManifest,
    modules: Mapping[ModuleId, object],
    resolution: PackageResolution,
    *,
    exists: Callable[[str], bool],
) -> None:
    """Validate a resolved package's commands, manifest config, and resources."""
    _validate_command_programs(manifest, resolution.resolutions)
    _validate_program_param_spellings(manifest, resolution)
    _validate_manifest_config(manifest, resolution.all_resolutions)
    _validate_resources(modules, resolution.resolutions, exists=exists)


def validate_package_distribution(resolution: PackageResolution) -> None:
    """Validate the filtered distribution produced by a directory package."""

    files = dict(distribution_files(resolution.package.root))
    validate_distribution_view(
        resolution,
        distribution_manifest(resolution.package.manifest),
        distribution_paths=(MANIFEST_NAME, *files),
    )


def validate_staged_distribution(resolution: PackageResolution, staged: PackageInfo) -> None:
    """Validate a staged distribution against the source resolution it came from.

    Staging copies a validated source tree, so what has to be established is
    that the copy is of what was validated rather than of a tree that changed
    underneath it — a content question, answered by comparing digests, not by
    resolving the same modules again.
    """

    files = [path.relative_to(staged.root).as_posix() for path in staged.root.rglob("*")]
    validate_distribution_view(resolution, staged.manifest, distribution_paths=files)
    for module_id, digest in resolution.digests.items():
        # Addressed by the place staging copied it to, not by the staged
        # manifest's own name, which the copy may disagree about.
        relative = resolution.modules[module_id].relative_to(resolution.package.root)
        staged_path = staged.root / relative
        if not staged_path.is_file():
            continue
        if _digest(normalize_newlines(fs.read_text(staged_path))) != digest:
            raise DisciplineError(
                f"staged package module {staged_path} differs from the validated "
                f"source {resolution.modules[module_id]}"
            )


def validate_distribution_view(
    resolution: PackageResolution,
    manifest: PackageManifest,
    *,
    distribution_paths: Iterable[str],
) -> None:
    """Validate what a package ships against the resolution of the tree it ships from.

    Every module a distribution keeps is one *resolution* already covers, so
    nothing here parses or resolves. What the exclusion filter can break is
    what is checked: the module tree must survive it, a retained module may not
    import an excluded one, a referenced resource must ship, and the manifest
    the distribution carries must name programs that ship.
    """

    validate_unreserved_package_name(manifest.name)
    _validate_command_paths(manifest)
    paths = frozenset(distribution_paths)
    module_root = MODULE_TREE_DIRNAME + "/"
    if not any(path.startswith(module_root) for path in paths):
        raise DisciplineError(
            f"package {manifest.name!r} requires module tree {MODULE_TREE_DIRNAME!r}"
        )
    root = resolution.package.root
    retained = {
        module_id: path
        for module_id, path in resolution.modules.items()
        if path.relative_to(root).as_posix() in paths
    }
    for module_id, path in retained.items():
        for target in resolution.adjacency.get(module_id, ()):
            if target in resolution.modules and target not in retained:
                raise DisciplineError(
                    f"module {resolution.modules[target]} imported by {path} "
                    "is excluded from the package distribution"
                )
    _validate_command_programs(
        manifest, {module_id: resolution.resolutions[module_id] for module_id in retained}
    )
    _validate_resources(
        retained,
        resolution.resolutions,
        exists=lambda relative: _archive_resource_exists(relative, paths),
    )


def _validate_manifest_schema(manifest: PackageManifest) -> None:
    """Reject manifest keys the schema does not define.

    Loading tolerates them so that a store AgM itself baked stays readable
    across builds; a package presented for validation — checked, installed, or
    packaged — is held to the schema here, where a typo is still the author's
    to fix and refusing it strands nothing.
    """

    if manifest.unknown_fields:
        raise DisciplineError(describe_unknown_fields(manifest.unknown_fields))


def validate_package_structure(package: PackageInfo) -> dict[ModuleId, Path]:
    """Run the manifest and module-tree checks that read no module source."""

    validate_unreserved_package_name(package.manifest.name)
    _validate_command_paths(package.manifest)
    return package_module_files(package)


def _resolve_package_modules(
    package: PackageInfo,
    modules: Mapping[ModuleId, Path],
    *,
    dependency_packages: tuple[PackageInfo, ...],
) -> PackageResolution:
    """Load and name-resolve a package under runtime package-visibility rules.

    A synthetic wildcard entry pulls the whole module tree into one graph, so
    every module is parsed exactly once and later checks read the compiler's
    own name resolution instead of re-deriving it.
    """

    if not modules:
        return PackageResolution(package)
    entry, next_id = _graph_entry([(package.manifest.name,)], wildcard=True)
    graph, resolved = _load_resolved(
        package, dependency_packages, entry, next_id, subject=f"package {package.manifest.name!r}"
    )
    return PackageResolution(
        package,
        modules=dict(modules),
        all_resolutions=resolved,
        adjacency=dict(graph.adjacency),
        source_adjacency=dict(graph.source_adjacency),
        digests={module_id: _digest(graph.modules[module_id].source_text) for module_id in modules},
    )


def _load_resolved(
    package: PackageInfo,
    dependency_packages: tuple[PackageInfo, ...],
    entry: Program,
    next_id: int,
    *,
    subject: str,
) -> tuple[ModuleGraph, dict[ModuleId, ModuleResolution]]:
    """Load and name-resolve *entry*'s graph with *package* and its dependencies mounted.

    A failure is reported as one resolving *subject*.
    """

    stdlib_root = (
        package.root
        if is_std_package_name(package.manifest.name)
        else shipped_stdlib_root().resolve()
    )
    roots = RootSet(
        roots=frozenset(),
        packages=(package, *dependency_packages),
        stdlib_roots=frozenset({stdlib_root}),
    )
    try:
        graph, _next_id, _loaded = build_repl_graph(
            entry,
            next_id,
            path=None,
            cached={},
            roots=roots,
            source_text="",
        )
        resolved = resolve_program(graph)
    except AglError as exc:
        raise DisciplineError(f"cannot resolve {subject}: {exc}") from exc
    return graph, {
        module_id: resolved_module.resolved
        for module_id, resolved_module in resolved.modules.items()
    }


def _digest(source_text: str) -> str:
    """Digest one module's normalized source, the form the loader resolved."""

    return hashlib.sha256(source_text.encode()).hexdigest()


def _graph_entry(module_paths: Sequence[tuple[str, ...]], *, wildcard: bool) -> tuple[Program, int]:
    """Build a synthetic entry importing each of *module_paths*, without reparsing the paths.

    A wildcard import is unaliased; a single-module one takes its own alias, so
    modules sharing a last segment do not clash.
    """

    source = "".join(
        "import package_root/*\n" if wildcard else f"import package_root/m as m{n}\n"
        for n in range(len(module_paths))
    )
    program, next_id = parse_program_seeded(source, start_id=0)
    declarations = [item for item in program.body.items if isinstance(item, ImportDecl)]
    imports = tuple(
        replace(declaration, module_path=module_path)
        for declaration, module_path in zip(declarations, module_paths, strict=True)
    )
    return replace(program, body=replace(program.body, items=imports)), next_id


def validate_archive_package(
    manifest: PackageManifest,
    *,
    archive_paths: Iterable[str],
    read_module: Callable[[str], str],
    dependency_packages: Iterable[PackageInfo] = (),
) -> None:
    """Validate archived module content against its resolved dependencies."""

    validate_unreserved_package_name(manifest.name)
    _validate_command_paths(manifest)
    dependencies = tuple(dependency_packages)
    paths = tuple(archive_paths)
    module_root = MODULE_TREE_DIRNAME + "/"
    if not any(path.startswith(module_root) for path in paths):
        raise DisciplineError(
            f"package {manifest.name!r} requires module tree {MODULE_TREE_DIRNAME!r}"
        )
    modules: dict[ModuleId, str] = {}
    for path in paths:
        if not path.startswith(module_root) or not path.endswith(".agl"):
            continue
        relative = PurePosixPath(path.removeprefix(module_root)).with_suffix("").as_posix()
        modules[_package_module_id(manifest.name, relative)] = path
    available_dependencies = {dependency.manifest.name for dependency in dependencies}
    if all(
        is_std_package_name(name) or name in available_dependencies
        for name in manifest.dependencies
    ):
        _validate_archive_content(
            manifest,
            modules,
            archive_paths=frozenset(paths),
            read_module=read_module,
            dependency_packages=dependencies,
        )


def _validate_archive_content(
    manifest: PackageManifest,
    modules: Mapping[ModuleId, str],
    *,
    archive_paths: frozenset[str],
    read_module: Callable[[str], str],
    dependency_packages: tuple[PackageInfo, ...],
) -> None:
    """Materialize archived modules so the filesystem graph loader can resolve them."""

    try:
        with TemporaryDirectory(prefix="agm-package-check-") as temporary:
            package = PackageInfo(Path(temporary), manifest)
            materialized: dict[ModuleId, Path] = {}
            for module_id, archive_path in modules.items():
                module_path = package.module_path(module_id.segments)
                module_path.parent.mkdir(parents=True, exist_ok=True)
                module_path.write_text(read_module(archive_path), encoding="utf-8")
                companion_path = str(PurePosixPath(archive_path).with_suffix(".py"))
                if companion_path in archive_paths:
                    module_path.with_suffix(".py").write_text("", encoding="utf-8")
                materialized[module_id] = module_path
            resolution = _resolve_package_modules(
                package, materialized, dependency_packages=dependency_packages
            )
            _validate_inherit_addresses(resolution, dependency_packages=dependency_packages)
    except (OSError, UnicodeDecodeError) as exc:
        raise DisciplineError(f"cannot load archive package {manifest.name!r}: {exc}") from exc
    _validate_resolved_package(
        manifest,
        modules,
        resolution,
        exists=lambda relative: _archive_resource_exists(relative, archive_paths),
    )


def _archive_resource_exists(relative: str, paths: frozenset[str]) -> bool:
    """Return whether an archive contains a file or nonempty directory resource."""
    normalized = PurePosixPath(relative).as_posix()
    return (
        normalized == "."
        or normalized in paths
        or any(path.startswith(normalized + "/") for path in paths)
    )


def _resource_exists(root: Path, relative: str) -> bool:
    """Return whether a resource exists without leaving a package root."""
    try:
        resolve_resource(root, relative)
    except ResourceError:
        return False
    return True


def _validate_resources(
    modules: Mapping[ModuleId, T],
    resolutions: _ModuleResolutions,
    *,
    exists: Callable[[str], bool],
) -> None:
    """Verify that every literal resource target in package modules is present."""

    for module_id, module_path in modules.items():
        for call, kind in _resource_calls(resolutions[module_id]):
            try:
                relative = resource_path(call, is_directory=kind is BuiltinKind.RESOURCE_DIR)
            except ResourceError as exc:
                raise DisciplineError(f"invalid resource call in {module_path}: {exc}") from exc
            if relative is not None and not exists(relative):
                raise DisciplineError(
                    f"resource {relative!r} referenced by {module_path} does not exist"
                )


def _resource_calls(resolution: ModuleResolution) -> list[tuple[Call, BuiltinKind]]:
    """Return the calls the scope pass resolved to a resource builtin, in source order."""

    calls: list[tuple[Call, BuiltinKind]] = []

    def collect(node: object) -> None:
        if not isinstance(node, Call):
            return
        kind = resolution.builtin_calls.get(node.node_id)
        if kind is BuiltinKind.RESOURCE or kind is BuiltinKind.RESOURCE_DIR:
            calls.append((node, kind))

    walk(resolution.program, collect)
    return calls


def validate_unreserved_package_name(name: str) -> None:
    """Reject a package name that AgM's own commands or config sections reserve."""

    if name in RESERVED_CONFIG_SECTION_NAMES:
        raise DisciplineError(f"package name {name!r} is reserved by AgM")


def package_module_files(package: PackageInfo) -> dict[ModuleId, Path]:
    """Return every ``.agl`` file under *package*'s module tree, keyed by module id."""
    module_root = package.module_root
    if not module_root.is_relative_to(package.root):
        raise DisciplineError(
            f"package {package.manifest.name!r} module tree escapes the package root"
        )
    if not fs.is_dir(module_root):
        raise DisciplineError(
            f"package {package.manifest.name!r} requires module tree {MODULE_TREE_DIRNAME!r}"
        )

    modules: dict[ModuleId, Path] = {}
    for path in sorted(fs.rglob(module_root, "*.agl")):
        if not fs.is_file(path):
            continue
        relative = path.relative_to(module_root).with_suffix("").as_posix()
        target = path.resolve()
        if not target.is_relative_to(module_root):
            raise DisciplineError(f"package module {relative!r} escapes its module tree")
        modules[_package_module_id(package.manifest.name, relative)] = target
    return modules


def _package_module_id(name: str, relative: str) -> ModuleId:
    """Build the module id a package gives one of its module-tree relative paths."""

    try:
        return ModuleId.from_path(f"{name}/{relative}")
    except ValueError as exc:
        raise DisciplineError(f"invalid module path {relative!r}") from exc


def _validate_command_paths(manifest: PackageManifest) -> None:
    """Hold command and alias paths to every reserved name, config sections included.

    Loading tolerates a path that only collides with a config section, so an
    installed package stays runnable; a validated package may not register one.
    """
    for kind, paths in (("command", manifest.commands), ("alias", manifest.aliases)):
        for path in paths:
            invalid = invalid_command_path(path, reserved=RESERVED_CONFIG_SECTION_NAMES)
            if invalid is not None:
                raise DisciplineError(f"{kind} path {path!r} {invalid}")


def _validate_command_programs(manifest: PackageManifest, resolutions: _ModuleResolutions) -> None:
    for command in manifest.commands.values():
        if command.program is not None:
            _validate_program_reference(manifest, command.program, resolutions)


def _validate_program_reference(
    manifest: PackageManifest, reference: str, resolutions: _ModuleResolutions
) -> None:
    module_path, separator, declaration_path = reference.partition("::")
    if not separator or not module_path or not declaration_path:
        raise DisciplineError(
            f"program reference {reference!r} must include module and declaration paths"
        )
    try:
        module_id = ModuleId.from_path(module_path)
    except ValueError as exc:
        raise DisciplineError(
            f"program reference {reference!r} has an invalid module path"
        ) from exc
    declaration = tuple(declaration_path.split("::"))
    if not all(is_identifier(segment) for segment in declaration):
        raise DisciplineError(f"program reference {reference!r} has an invalid declaration path")
    if module_id.segments[0] != manifest.name:
        raise DisciplineError(
            f"program reference {reference!r} is outside package {manifest.name!r}"
        )
    resolution = resolutions.get(module_id)
    if resolution is None:
        raise DisciplineError(f"program reference {reference!r} names no package module")

    function = _program_declarations(resolution).get(declaration)
    if function is None:
        raise DisciplineError(f"program reference {reference!r} names no program declaration")
    if function.type_param_slots or (
        function.return_type is not None and not isinstance(function.return_type, UnitT)
    ):
        raise DisciplineError(
            f"registered program {reference!r} must declare no type parameters and a unit result"
        )


def _validate_program_param_spellings(
    manifest: PackageManifest, package: PackageResolution
) -> None:
    """Reject a registered program whose closure has parameters sharing a full spelling.

    The closure is the program's module and the modules it imports, transitively
    (those, not the whole package, are what a host addresses together). Only
    named modules are checked, as at run time (``colliding_declarations``).
    """
    for command in manifest.commands.values():
        if command.program is None:
            continue
        entry = ModuleId(split_program_reference(command.program)[0])
        closure = _import_closure(entry, package.source_adjacency)
        collision = full_spelling_collision(
            declaration
            for module_id in closure
            for declaration in _param_declarations(module_id, package.all_resolutions[module_id])
        )
        if collision is not None:
            raise DisciplineError(f"program {command.program!r}: {collision}")


def _import_closure(
    entry: ModuleId, adjacency: Mapping[ModuleId, tuple[ModuleId, ...]]
) -> tuple[ModuleId, ...]:
    """Return *entry* and every module it imports in source, transitively.

    *adjacency* holds source-authored edges only (``ModuleGraph.source_adjacency``),
    so the closure is the run-time one (``ModuleGraph.source_reachable_modules``).
    """
    return reachable_nodes(entry, lambda module_id: adjacency.get(module_id, ()))


def _resolution_params(
    module_id: ModuleId, resolution: ModuleResolution
) -> Iterator[tuple[tuple[str, ...], str, StaticBindingKey | FunctionParamKey]]:
    """Yield ``(declaration route, name, key)`` for each host parameter *resolution* declares.

    A function parameter's route is its function's qualified route.
    """
    for binding in resolution.param_bindings():
        name = binding.cli.name
        yield (
            binding.scope_path,
            name,
            static_binding_key(module_id, binding.scope_path, name),
        )
    for function_binding in resolution.function_param_bindings():
        function = function_binding.function.name
        name = function_binding.cli.name
        yield (
            (*function_binding.scope_path, function),
            name,
            FunctionParamKey(module_id, function_binding.scope_path, function, name),
        )


def _param_declarations(
    module_id: ModuleId, resolution: ModuleResolution
) -> Iterator[tuple[str, str]]:
    """Yield ``(full spelling, label)`` for each host parameter *resolution* declares."""
    for route, name, key in _resolution_params(module_id, resolution):
        spelling = param_spellings_for(module_id.segments, route, name, is_entry=False)[-1]
        yield spelling, host_param_label(key)


def _program_declarations(resolution: ModuleResolution) -> dict[tuple[str, ...], FuncDef]:
    """Return *resolution*'s ``program def`` items by scope-qualified declaration path."""
    return {
        scope_names(function.scope_path) + (function.name,): function
        for function in static_function_items(resolution.program.body.items)
        if function.is_program
    }


def _command_argument_spellings(
    manifest: PackageManifest, resolutions: _ModuleResolutions
) -> dict[tuple[str, ...], frozenset[str]]:
    """Return, per registered command path (aliases expanded), its name-addressable arguments.

    A program's argument is addressed in a config table by its ``cli.name``,
    exactly as the run-time reader does for the zones of ``NAME_ADDRESSABLE_ZONES``.
    """
    spellings: dict[tuple[str, ...], frozenset[str]] = {}
    for path, command in expanded_commands(manifest).items():
        if command.program is None:
            continue
        module_segments, declaration = split_program_reference(command.program)
        resolution = resolutions[ModuleId(module_segments)]
        function = _program_declarations(resolution)[declaration]
        spellings[tuple(path.split())] = _program_argument_spellings(resolution, function)
    return spellings


def _program_argument_spellings(resolution: ModuleResolution, function: FuncDef) -> frozenset[str]:
    """Return the name-addressable argument spellings of *function*, a program of *resolution*."""
    attributes = resolution.attributes
    return frozenset(
        attributes.program_options[param.node_id].name
        for param in function.params
        if attributes.param_zones[param.node_id] in NAME_ADDRESSABLE_ZONES
    )


def _level_argument_spellings(
    commands: Mapping[tuple[str, ...], frozenset[str]], prefix: tuple[str, ...]
) -> frozenset[str]:
    """Return the argument spellings a ``[config]`` table at *prefix* may set.

    As at run time, a command's table, its group tables and the root all reach
    its program's arguments.
    """
    return frozenset(
        spelling
        for path, spellings in commands.items()
        if path[: len(prefix)] == prefix
        for spelling in spellings
    )


def _validate_manifest_config(manifest: PackageManifest, resolutions: _ModuleResolutions) -> None:
    """Validate ``[config]``'s nested command tables and leaves.

    A nested table's key names a registered command, group, or alias path
    (*manifest*'s own, ``@command`` merged in); every other leaf spells an
    engine setting, a ``@param`` binding or ``@param`` function parameter in the
    package's resolved dependency closure (*resolutions*: package, dependencies,
    and ``std``) or, at a level a command's program is reached from, one of that
    program's arguments.
    Per-program reachability, precedence and cross-route ambiguity are a run-time
    concern: a leaf the selected program does not consume is warned about there.
    """
    if not manifest.config:
        return
    from agnette.agl.runtime.engine_config import validate_manifest_leaf_value
    from agnette.agl.semantics.type_table import create_seeded_type_table

    registered = registered_command_paths(manifest)
    param_spellings = _closure_param_spellings(resolutions)
    arguments = _command_argument_spellings(manifest, resolutions)
    # A word a ``@param`` spells shadows the engine setting of that spelling for
    # some programs; its value is then decoded per program at run time.
    engine_spellings = _engine_spellings(excluding=param_spellings)
    _validate_config_level(
        manifest.config,
        (),
        registered,
        param_spellings,
        arguments,
        engine_spellings,
        create_seeded_type_table(),
        validate_manifest_leaf_value,
    )


def _validate_config_level(
    table: TomlDict,
    prefix: tuple[str, ...],
    registered: frozenset[tuple[str, ...]],
    param_spellings: frozenset[str],
    arguments: Mapping[tuple[str, ...], frozenset[str]],
    engine_spellings: Mapping[str, str],
    type_table: "TypeTable",
    decode: Callable[[str, object, "TypeTable"], object],
) -> None:
    level_arguments = _level_argument_spellings(arguments, prefix)
    for key, value in table.items():
        path = (*prefix, key)
        is_command_word = path in registered
        is_leaf_spelling = (
            key in engine_spellings or key in param_spellings or key in level_arguments
        )
        if is_command_word and is_leaf_spelling:
            raise DisciplineError(
                f"manifest config key {display_table_path(path)!r} is ambiguous between a "
                "registered command and a parameter or engine-setting spelling"
            )
        if is_command_word:
            if not isinstance(value, dict):
                raise DisciplineError(
                    f"manifest config key {display_table_path(path)!r} registers a command "
                    "and must be a table"
                )
            nested: TomlDict = value
            _validate_config_level(
                nested,
                path,
                registered,
                param_spellings,
                arguments,
                engine_spellings,
                type_table,
                decode,
            )
            continue
        if key in engine_spellings and key not in level_arguments:
            try:
                decode(engine_spellings[key], value, type_table)
            except ValueError as exc:
                raise DisciplineError(
                    f"manifest config key {display_table_path(path)!r}: {exc}"
                ) from exc
            continue
        if key not in param_spellings and key not in level_arguments:
            raise DisciplineError(
                f"manifest config key {display_table_path(path)!r} names neither a registered "
                "command nor a known argument, parameter or engine setting"
            )


def _closure_param_spellings(resolutions: _ModuleResolutions) -> frozenset[str]:
    """Return every bare or dotted spelling of a module or function ``@param`` in *resolutions*."""
    return frozenset(
        spelling
        for module_id, resolution in resolutions.items()
        for route, name, _key in _resolution_params(module_id, resolution)
        for spelling in param_spellings_for(
            module_id.segments, route, name, is_entry=module_id.is_entry
        )
    )


def _engine_spellings(*, excluding: frozenset[str] = frozenset()) -> dict[str, str]:
    """Map each engine-setting spelling not in *excluding* to its setting's name."""
    from agnette.agl.runtime.engine_config import engine_setting_params

    return {
        spelling: param.name
        for param in engine_setting_params()
        for spelling in param.declared_spellings
        if spelling not in excluding
    }


def _validate_inherit_addresses(
    package: PackageResolution, *, dependency_packages: tuple[PackageInfo, ...]
) -> None:
    """Validate each ``@inherit`` address in the package's modules at scope level.

    As for manifest ``[config]``, only static facts count. An address splits at
    the longest command or alias path that runs a program and that the package
    or a declared dependency registers (the packages its imports may name, not
    every installed one); its leaf must then be in the target program's
    spelling superset: its arguments, the ``@param`` spellings of its module
    closure, and the engine settings.
    """
    addresses = [
        (module_id, address)
        for module_id, resolution in package.resolutions.items()
        for written in resolution.attributes.inherits.values()
        for address in written
    ]
    if not addresses:
        return
    # Deferred: source discovery itself imports this module.
    from agnette.packages.source_commands import complete_packages

    manifest = package.package.manifest
    declared = (dep for dep in dependency_packages if dep.manifest.name in manifest.dependencies)
    registry = command_registry(complete_packages((package.package, *declared)))
    targets: list[tuple[ModuleId, str, ProgramTarget, str]] = []
    for module_id, address in addresses:
        split = split_command_address(address, registry)
        if split is None:
            reason = _unsplit_reason(address, registry)
            raise DisciplineError(f"{module_id.path_str()}: @inherit address {address!r} {reason}")
        targets.append((module_id, address, *split))
    resolutions, adjacency = _target_resolutions(
        package, dependency_packages, {_target_module(target) for _, _, target, _ in targets}
    )
    engine = frozenset(_engine_spellings())
    # Each program's spellings are computed once, however many addresses name it.
    spellings: dict[tuple[tuple[str, ...], tuple[str, ...]], frozenset[str] | None] = {}
    for module_id, address, target, leaf in targets:
        where = f"{module_id.path_str()}: @inherit address {address!r}"
        key = (target.module_segments, target.declaration_path)
        if key not in spellings:
            spellings[key] = _target_spellings(target, resolutions, adjacency, engine)
        accepted = spellings[key]
        if accepted is None:
            reference = program_reference(target.module_segments, target.declaration_path)
            raise DisciplineError(
                f"{where}: command program {reference!r} of package "
                f"{target.package.manifest.name!r} names no program declaration"
            )
        if leaf not in accepted:
            raise DisciplineError(
                f"{where}: {leaf!r} is no argument, parameter or engine setting "
                "of the target program"
            )


def _unsplit_reason(address: str, registry: CommandRegistry) -> str:
    """Say why *address* names no program command followed by a key."""
    words = address.split(".")
    for length in range(len(words), 0, -1):
        registered = registry.get(tuple(words[:length]))
        if registered is None:
            continue
        path = ".".join(words[:length])
        if registered.spec.program is None:
            return f"names the command group {path!r}, not a program command followed by a key"
        return f"names the program command {path!r} without a key after it"
    return "names no command registered by the package or its declared dependencies"


def _target_module(target: ProgramTarget) -> ModuleId:
    """Return the module declaring *target*'s program."""
    return ModuleId(target.module_segments)


def _target_resolutions(
    package: PackageResolution,
    dependency_packages: tuple[PackageInfo, ...],
    modules: set[ModuleId],
) -> tuple[_ModuleResolutions, Mapping[ModuleId, tuple[ModuleId, ...]]]:
    """Return resolutions and source import adjacency covering *modules* and their closures.

    The package's own graph covers its modules and what they import; a
    dependency module it never imports is loaded, with its closure, in one
    further graph per owning package, so a failure is attributed to that
    package (modules already parsed or resolved are reused from the shared
    caches).
    """
    resolutions = dict(package.all_resolutions)
    adjacency = dict(package.source_adjacency)
    missing: dict[str, list[tuple[str, ...]]] = {}
    for module in sorted((m for m in modules if m not in resolutions), key=str):
        missing.setdefault(module.segments[0], []).append(module.segments)
    for owner, module_paths in missing.items():
        entry, next_id = _graph_entry(module_paths, wildcard=False)
        graph, loaded = _load_resolved(
            package.package,
            dependency_packages,
            entry,
            next_id,
            subject=f"package {owner!r} (an @inherit target of {package.package.manifest.name!r})",
        )
        resolutions.update(loaded)
        adjacency.update(graph.source_adjacency)
    return resolutions, adjacency


def _target_spellings(
    target: ProgramTarget,
    resolutions: _ModuleResolutions,
    adjacency: Mapping[ModuleId, tuple[ModuleId, ...]],
    engine: frozenset[str],
) -> frozenset[str] | None:
    """Return the leaves an ``@inherit`` address may name on *target*'s program.

    ``None`` when its module declares no such program.
    """
    module_id = _target_module(target)
    function = _program_declarations(resolutions[module_id]).get(target.declaration_path)
    if function is None:
        return None
    closure = _import_closure(module_id, adjacency)
    param_spellings = _closure_param_spellings({m: resolutions[m] for m in closure})
    return _program_argument_spellings(resolutions[module_id], function) | param_spellings | engine
