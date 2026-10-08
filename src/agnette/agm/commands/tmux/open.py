"""agm tmux open."""

from __future__ import annotations

from agnette.agm.cli_support.args import TmuxOpenArgs
from agnette.agm.tmux.session import create_tmux_session


def run(args: TmuxOpenArgs) -> None:
    create_tmux_session(
        detach=args.detach,
        pane_count=args.pane_count,
        session_name=args.session_name,
    )
