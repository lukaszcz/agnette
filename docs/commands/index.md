# AgM commands reference

AgM (Agnette Manager) is Agnette's CLI agent project manager. Agnette (Agentic
Notation & Execution Toolkit) provides CLI agent management facilities and a complete
agent workflow DSL, AgL (Agnette Language). The `agm` executable manages workspaces,
dependencies, sandboxing, git worktrees, and tmux sessions, and runs AgL programs.

## Global usage

```text
agm <command> [options] [args]
```

Global options:

- `--version` prints the AgM and AgL standard-library versions.
- `--install-completion`
- `--show-completion`

`agm help` prints the command overview; `agm help <command>` prints detailed help
for a single command or command group. Each command also accepts `--help`.

## Chapters

| Chapter | Contents |
| ------- | -------- |
| [Workspace and project lifecycle](workspaces.md) | `agm open`/`close`, `agm workspace`/`wsp`, `agm init`, `agm sync` |
| [AgL workflow DSL](agl.md) | `agm exec`, `agm check`, `agm repl` |
| [Packages](pkg.md) | `agm pkg` check/create/install/switch/uninstall/list/info |
| [Configuration](config.md) | `agm config` copy/env/update |
| [Dependencies](dependencies.md) | `agm dep` |
| [Sandboxing](run.md) | `agm run` |
| [Worktrees](worktrees.md) | `agm worktree`/`wt` |
| [tmux sessions](tmux.md) | `agm tmux` |
