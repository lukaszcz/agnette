import os
import sys

from agnette.agl.artifact_storage import finish_prune
from agnette.util.text import format_description_column


def forked_work(name):
    """Fork a child that lists *name* with a `src/` helper nothing else here calls."""
    # Compiling this program may have started the artifact cache's prune thread.
    finish_prune()
    assert len(sys._current_frames()) == 1
    pid = os.fork()
    if pid == 0:
        try:
            listing = format_description_column([(name, "forked")], width=40)
            os.write(1, f"child: {listing!r}\n".encode())
        finally:
            os._exit(3)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)
