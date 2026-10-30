"""Write the portable grammar's Unicode snapshot to stdout."""

import json
import unicodedata

from agnette.agl.highlight.unicode_vocabulary import scan_unicode_classes


def snapshot_source() -> str:
    """Render the canonical vocabulary as a literal module."""
    patterns = scan_unicode_classes()
    lines = [
        '"""Generated Unicode vocabulary; regenerate with tools/highlight_unicode.py."""',
        "",
        "from typing import Final",
        "",
        "UNICODE_SNAPSHOT: Final[tuple[str, tuple[str, str]]] = (",
        f"    {json.dumps(unicodedata.unidata_version)},",
        "    (",
    ]
    for pattern in patterns:
        lines.append("        (")
        lines.extend(
            f"            {json.dumps(pattern[offset : offset + 24], ensure_ascii=False)}"
            for offset in range(0, len(pattern), 24)
        )
        lines.append("        ),")
    lines.extend(("    ),", ")", ""))
    return "\n".join(lines)


if __name__ == "__main__":
    print(snapshot_source(), end="")
