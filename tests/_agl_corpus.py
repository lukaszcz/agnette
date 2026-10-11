"""Shared lexical corpus: test programs, stdlib sources, and documentation fences."""

import re
import textwrap
from functools import cache
from itertools import batched
from pathlib import Path


@cache
def lexical_corpus() -> tuple[tuple[str, str], ...]:
    """Collect normalized text once; include deliberate errors and fragments."""
    root = Path(__file__).resolve().parents[1]
    cases: list[tuple[str, str]] = []
    for directory in (root / "tests/agl", root / "packages/stdlib"):
        for path in sorted(directory.rglob("*.agl")):
            cases.append((str(path.relative_to(root)), path.read_text(errors="surrogateescape")))
    fence = re.compile(r"^(?P<indent>[ \t]*)```agl\s*$")
    for path in sorted((root / "docs").rglob("*.md")):
        lines = path.read_text().splitlines()
        index = 0
        while index < len(lines):
            if fence.match(lines[index]) is None:
                index += 1
                continue
            line = index + 1
            index += 1
            body: list[str] = []
            while index < len(lines) and lines[index].strip() != "```":
                body.append(lines[index])
                index += 1
            cases.append((f"{path.relative_to(root)}:{line}", textwrap.dedent("\n".join(body))))
            index += 1
    return tuple(cases)


def lexical_corpus_batches() -> tuple[tuple[tuple[str, str], ...], ...]:
    """Bound corpus test cost while amortizing per-test isolation and collection."""
    return tuple(batched(lexical_corpus(), 32))
