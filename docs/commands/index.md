# AgM commands reference

AgM (Agnette Manager) is the CLI agent project manager of Agnette (Agentic Notation &
Execution Toolkit). The `agm` executable manages workspaces, dependencies, sandboxing, git
worktrees, and tmux sessions, and runs AgL (Agnette Language) programs.

## Global usage

```text
agm <command> [options] [args]
```

Global options:

- `--version` prints the AgM and AgL standard-library versions.
- `--install-completion`
- `--show-completion`

`agm help` prints the command overview and `agm help <command>` a command's or group's
help; each command also accepts `--help`.

## Chapters

| Chapter | Contents |
| ------- | -------- |
| [Workspace and project lifecycle](workspaces.md) | `agm open`/`close`, `agm workspace`/`wsp`, `agm init`, `agm sync` |
| [AgL workflow DSL](agl.md) | `agm exec`, `agm check`, `agm repl` |
| [Packages](pkg.md) | `agm pkg` init/check/create/install/switch/uninstall/list/info/sync |
| [Configuration](config.md) | `agm config` copy/env/update |
| [Dependencies](dependencies.md) | `agm dep` |
| [Sandboxing](run.md) | `agm run` |
| [Worktrees](worktrees.md) | `agm worktree`/`wt` |
| [tmux sessions](tmux.md) | `agm tmux` |
