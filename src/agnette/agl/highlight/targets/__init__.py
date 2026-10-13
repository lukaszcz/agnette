"""Pure grammar exporters; add a module and one registry entry for each target.

Each exporter is imported only when it runs, so listing targets stays cheap.
"""

from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agnette.agl.highlight.grammar import Grammar


def _exporter(target: str) -> Callable[["Grammar"], str]:
    def generate(grammar: "Grammar") -> str:
        rendered: str = import_module(f"{__name__}.{target}").generate(grammar)
        return rendered

    return generate


TARGETS: dict[str, Callable[["Grammar"], str]] = {
    target: _exporter(target) for target in ("kde", "emacs", "textmate", "sublime")
}
