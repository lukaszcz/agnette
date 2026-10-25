"""``:info`` section labels, shared by the emitter (session) and the highlighter (meta)."""

from __future__ import annotations

import re
from typing import Literal

type SectionLabel = Literal["Binding", "Signature", "Type", "Value", "Module", "Scope"]
"""Labels of the AgL code sections ``:info`` emits; emitters are typed by it."""

SECTION_LABELS: tuple[SectionLabel, ...] = (
    "Binding",
    "Signature",
    "Type",
    "Value",
    "Module",
    "Scope",
)

_LABEL_ALTERNATIVES = "|".join(SECTION_LABELS)

SECTION_RE = re.compile(rf"^(?:{_LABEL_ALTERNATIVES})(?: \(.*\))?:\n", re.MULTILINE)
"""A code section's header line, with its optional ``(source)`` suffix."""

SECTION_BOUNDARY_RE = re.compile(rf"^(?:{_LABEL_ALTERNATIVES}|Location)(?: \(.*\))?:", re.MULTILINE)
"""The start of whatever ends a code section: another section or its ``Location`` line."""


def sourced_label(label: SectionLabel, source: str) -> str:
    """Return *label* qualified by the *source* it was found in."""
    return f"{label} ({source})"
