"""agm config update."""

from __future__ import annotations

from agnette.agm.cli_support.args import ConfigUpdateArgs
from agnette.agm.project.config_git import commit_config_dir_changes
from agnette.agm.project.dependency_env import update_all_project_dependency_configs
from agnette.agm.project.layout import require_current_project_dir
from agnette.core.env import clone_env


def run(args: ConfigUpdateArgs) -> None:
    del args
    env = clone_env()
    project_dir = require_current_project_dir()
    update_all_project_dependency_configs(project_dir, env=env)
    commit_config_dir_changes(project_dir, "chore: update config", env=env)
