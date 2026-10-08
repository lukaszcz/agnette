"""agm worktree remove."""

from __future__ import annotations

import agnette.vcs.git as git_helpers
from agnette.agm.cli_support.args import WorktreeRemoveArgs
from agnette.agm.project.worktree import remove_worktree


def run(args: WorktreeRemoveArgs) -> None:
    repo_dir = git_helpers.checkout_root()
    remove_worktree(repo_dir=repo_dir, force=args.force, branch=args.branch)
