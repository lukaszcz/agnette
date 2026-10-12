"""Pure grammar exporters; add a module and one registry entry for each target."""

from collections.abc import Callable

from agnette.agl.highlight.grammar import Grammar
from agnette.agl.highlight.targets.kde import generate

TARGETS: dict[str, Callable[[Grammar], str]] = {"kde": generate}
