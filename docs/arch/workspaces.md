# Projects and Workspaces

AgM's project-management layer turns a directory tree into an agent-oriented project: a main repository, parallel git worktrees for branches, dependency checkouts, per-workspace environment and setup, and tmux sessions. State is read from disk, never tracked separately.

## Project Layout

A project has fixed roles — repository, branch worktrees, config directory, dependencies, notes — in one of two layouts: **embedded** (under `.agm/` in the project root) or **split** (sibling directories). `project/layout.py` detects the layout and resolves role paths; current-project discovery walks up from the invocation directory (skipping the AgM home), then falls back to `$PROJ_DIR`. `agm init` creates either layout.

## Workspaces and Worktrees

A *workspace* is the main repository or a linked worktree in the project's worktrees directory; `project_workspaces` is the single source of that set. Opening a branch creates its worktree (and the branch, if missing) through one preparation flow (`commands/workspace/open.py`): config and environment, worktree, generated config commit, session. Branch resolution across remotes follows git's rule. Only branch workspaces can be closed.

## Dependencies

Dependencies are sibling repositories under the deps directory, managed by `agm dep`. Each dependency directory holds a main checkout plus per-branch checkouts; a workspace's config `[deps]` table selects one per dependency, exported as `<NAME>_DIR`, and `dep switch` rewrites it. A new branch's dependency config inherits from its parent workspace's (main by default).

## Sync

Branch resolution uses local refs unless `--fetch` is given. `sync fetch` prunes stale worktrees, fetches the main repo and dependencies, and creates tracking branches; `sync pull` then merges every workspace and dependency from its upstream.

## Workspace Environment and Shell

A workspace's environment chains the dependency environment, config-directory dotenv files, and shell env files. A per-workspace shell wrapper, also used as `$SHELL`, restores the workspace identity after the user's rc file, keeping sessions independent of inherited tmux state. Setup scripts prepare new workspaces.

## Git and Tmux

Shared git helpers live in `vcs/git.py`, each optionally taking the caller's environment; config-directory commits, `init`, and `dep new` build their git commands directly. Workspace sessions are tmux sessions running the workspace shell in a grid layout (`tmux/layout.py`), uniquely named per workspace and targeted by tmux IDs and exact names.

## Code Entry Points

- `src/agnette/agm/project/layout.py` — layout detection, role paths, workspace enumeration.
- `src/agnette/agm/project/worktree.py` — worktree creation and branch/remote synchronization.
- `src/agnette/agm/project/workspace_env.py`, `workspace_shell.py`, `workspace_setup.py` — environment, shell wrapper, setup scripts.
- `src/agnette/agm/project/dependency_env.py`, `dependency_checkout.py`, `config_git.py` — dependency environment, checkout discovery, config-directory git.
- `src/agnette/vcs/git.py` — shared git integration; `src/agnette/agm/tmux/` — tmux sessions and layout.
- `src/agnette/agm/commands/init.py`, `workspace/`, `worktree/`, `dep/`, `sync/`, `tmux/`, `config/` — the management commands (`config env` is what the workspace shell evaluates).
