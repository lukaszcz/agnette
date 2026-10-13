# Agnette

Agnette (Agentic Notation & Execution Toolkit) provides CLI agent management facilities
and a complete agent workflow DSL:

- **AgM (Agnette Manager)** — the CLI agent project manager: workspaces, dependencies,
  git worktrees, tmux sessions, setup scripts, and sandboxed commands.
- **AgL (Agnette Language)** — the statically typed workflow DSL for orchestrating agents
  and shell commands, run with `agm exec` or interactively with `agm repl`.

The Python package is `agnette`, the executable `agm`. Configuration uses `.agm` directories
and `AGM_*` variables; AgL sources use `.agl`.

## Documentation

The [documentation landing page](docs/index.md) links the
[AgL language reference](docs/agl/reference/index.md), the
[command reference](docs/commands/index.md), and the [architecture guide](docs/arch/index.md).
`agm help [COMMAND]` prints command help.

`just docs-build` builds the site into `site/`; `just docs-serve` serves a live preview on
port 8000 (pass server options, e.g. `--dev-addr 127.0.0.1:8080`). Read the Docs builds from
`.readthedocs.yaml` and `mkdocs.yml`.

## Requirements

- Python 3.14, [`uv`](https://docs.astral.sh/uv/), [`just`](https://github.com/casey/just)
- `git`, `bash`
- `tmux` for `agm open` and `agm tmux`
- [`srt`](https://github.com/anthropic-experimental/sandbox-runtime) for sandboxed `agm run`
  and agent calls; `systemd-run` for `agm run` memory limits

## Install

```bash
just setup                  # development environment in .venv
just install                # install agm and its AgM home
just install /usr/local     # install under a prefix
just install --force        # replace differing config files, keeping <file>.bak
```

`just install` installs `agm` with the locked runtime dependencies as a `uv tool`, copies
config files and sandbox templates into the AgM home, refreshes the managed standard
library, reinstalls the repository packages under `packages/`, syncs their Python
requirements, and installs the AgL editor modes (Micro, and Emacs when available — see
[config/emacs/README.md](config/emacs/README.md)). It replaces any existing `agm` executable
and always refreshes the editor modes and highlighting definitions; `--force` controls
replacement of differing config files and existing Pandoc defaults.

[AgL highlighting](docs/commands/dev.md) installs for Kate, TextMate, Sublime Text, bat, Emacs, and Pandoc (`pandoc -d agl`).

The AgM home is `$AGM_HOME`, else a populated `.agm` beside the executable, else
`$HOME/.agm`. A prefix install puts `agm` in `<prefix>/bin` (add it to `PATH`) and the
AgM home in `<prefix>/.agm`, overriding `$AGM_HOME` while installing.

## Projects

`agm init` creates a project in one of two layouts: embedded by default when the project
directory is already a git repo, split otherwise.

```text
split                 embedded
myproject/            myproject/
├── repo/             ├── .agm/
├── deps/             │   ├── config/
├── notes/            │   ├── deps/
├── worktrees/        │   ├── notes/
└── config/           │   └── worktrees/
                      └── <main workspace files>
```

A workspace is the main repo or a branch worktree, with its own tmux session:

```bash
agm init myproject https://github.com/org/repo.git
cd myproject
agm open feat/login         # create the branch worktree and open its tmux session
agm close feat/login        # remove the worktree, branch, and session
agm sync pull               # fetch and merge all workspaces and dependencies
```

## AgL

```bash
agm exec workflow.agl "add login"   # run a program; its parameters become CLI arguments
agm exec -c 'print("hello")'        # run inline source
agm check lib/*.agl                 # static check only
agm repl                            # interactive session
```

Agent calls run inside the `agm run` sandbox by default. See the
[AgL language reference](docs/agl/reference/index.md) and
[`agm exec`/`check`/`repl`](docs/commands/agl.md).

## Commands

| Command | Purpose | Reference |
| ------- | ------- | --------- |
| `agm open`, `agm close` | Open or close a workspace and its tmux session | [workspaces](docs/commands/workspaces.md) |
| `agm workspace` (`wsp`) | List, open, close, and set up workspaces | [workspaces](docs/commands/workspaces.md) |
| `agm init` | Initialize a project, optionally cloning a repo | [workspaces](docs/commands/workspaces.md) |
| `agm sync` | Fetch and merge the project's repositories | [workspaces](docs/commands/workspaces.md) |
| `agm exec`, `agm check`, `agm repl` | Run, check, or explore AgL | [agl](docs/commands/agl.md) |
| `agm pkg` | Create, check, install, and manage AgL packages | [pkg](docs/commands/pkg.md) |
| `agm dev` | Export AgL highlighting definitions; find AgL declarations | [dev](docs/commands/dev.md) |
| `agm config` | Copy config files, print workspace env, update config | [config](docs/commands/config.md) |
| `agm dep` | Manage dependency checkouts | [dependencies](docs/commands/dependencies.md) |
| `agm run` | Run a command, sandboxed by default | [run](docs/commands/run.md) |
| `agm worktree` (`wt`) | Low-level worktree operations | [worktrees](docs/commands/worktrees.md) |
| `agm tmux` | Manage tmux sessions and pane layouts | [tmux](docs/commands/tmux.md) |

Packages can register their `program def` entries as `agm` commands. The repository ships
`packages/devel` (development workflows, e.g. `agm devel plan`) and `packages/sysone`
(the `sysone/jev` module for TypeSafe's System One model); `just install` installs both.
