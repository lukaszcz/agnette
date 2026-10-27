"""Typed CLI argument containers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ConfigCopyArgs:
    dirname: str


@dataclass(slots=True)
class ConfigEnvArgs:
    pass


@dataclass(slots=True)
class DevHighlightArgs:
    target: str
    output: str | None


@dataclass(slots=True)
class DevDefinitionArgs:
    file: str
    line: int
    column: int
    module_paths: list[str] = field(default_factory=list)
    no_stdlib: bool = False
    stdin: bool = False


@dataclass(slots=True)
class PkgCheckArgs:
    directory: str | None


@dataclass(slots=True)
class PkgInitArgs:
    directory: str | None
    name: str | None
    version: str


@dataclass(slots=True)
class PkgCreateArgs:
    directory: str | None
    output: str | None


@dataclass(slots=True)
class PkgInstallArgs:
    source: str
    editable: bool
    shadow: bool
    reinstall: bool = False
    project: bool = False


@dataclass(slots=True)
class PkgUninstallArgs:
    name: str
    project: bool = False


@dataclass(slots=True)
class PkgSwitchArgs:
    target: str
    project: bool = False


@dataclass(slots=True)
class PkgListArgs:
    pass


@dataclass(slots=True)
class PkgSyncArgs:
    project: bool = False


@dataclass(slots=True)
class PkgInfoArgs:
    name: str


@dataclass(slots=True)
class ConfigUpdateArgs:
    pass


@dataclass(slots=True)
class WorktreeNewArgs:
    worktrees_dir: str | None
    branch: str
    no_fetch: bool = True


@dataclass(slots=True)
class WorktreeRemoveArgs:
    force: bool
    branch: str


@dataclass(slots=True)
class DepNewArgs:
    branch: str | None
    repo_url: str


@dataclass(slots=True)
class DepRemoveArgs:
    all: bool
    target: str


@dataclass(slots=True)
class DepSwitchArgs:
    dep: str
    branch: str
    create_branch: bool
    no_fetch: bool = True


@dataclass(slots=True)
class OpenArgs:
    detached: bool
    pane_count: str | None
    parent: str | None
    branch: str
    no_fetch: bool = True


@dataclass(slots=True)
class CloseArgs:
    branch: str
    force: bool
    force_delete: bool
    keep_branch: bool
    keep_workspace: bool


@dataclass(slots=True)
class InitArgs:
    positional: list[str]
    branch: str | None
    embedded: bool
    split: bool
    clone: bool
    no_repo_git: bool
    no_config_git: bool
    no_notes_git: bool
    no_git_init: bool


@dataclass(slots=True)
class RunArgs:
    run_command: list[str]
    no_sandbox: bool
    no_patch: bool
    memory: str | None
    swap: str | None
    no_memory_limit: bool
    no_swap_limit: bool
    settings_file: str | None
    pty: bool | None = None


@dataclass(slots=True)
class TmuxOpenArgs:
    detach: bool
    pane_count: str | None
    session_name: str | None


@dataclass(slots=True)
class TmuxCloseArgs:
    session_name: str


@dataclass(slots=True)
class TmuxLayoutArgs:
    pane_count: str
    window_id: str | None


@dataclass(slots=True)
class CheckArgs:
    files: list[str]
    module_paths: list[str] = field(default_factory=list)
    no_stdlib: bool = False


@dataclass(slots=True)
class ExecArgs:
    file: str | None
    echo: bool | None = None
    argument_tokens: list[str] = field(default_factory=list)
    code: str | None = None
    program: str | None = None
    module_paths: list[str] = field(default_factory=list)
    no_stdlib: bool = False
    # Optional recursion call-depth override (None = no override).
    max_call_depth: int | None = None
    # Static artifacts produced only when CLI ambiguity required early discovery.
    pipeline_cache: object | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class ReplArgs:
    quiet: bool
    echo: bool | None = None
    # Optional recursion call-depth override (None = no override).
    max_call_depth: int | None = None
    no_stdlib: bool = False
    # Engine-setting flag tokens (``--timeout 5``, ``--no-trace``).
    tokens: list[str] = field(default_factory=list)
    # Force the plain, non-interactive line front end even on a tty; the
    # auto-detected default (non-tty stdin/stdout, or TERM=dumb) needs no flag.
    plain: bool = False
