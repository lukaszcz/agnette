"""Shared bat/syntect decoding and reference comparison for development probes."""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from agnette.agl.highlight.agl_grammar import AGL_GRAMMAR
from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.grammar import Grammar
from agnette.agl.highlight.targets.textmate import TEXTMATE_SCOPES
from tools.highlight_engine_support import (
    CORPUS,
    CorpusCase,
    EngineResult,
    assert_result,
    spans_from_categories,
)
from tools.install_highlighting import bat_executable

_THEME = "agl-probe"
# One distinct 24-bit foreground per category; unmatched text (LF) uses TEXT's.
_COLORS = {
    category: (0x10 + index, 0x20 + index, 0x30 + index) for index, category in enumerate(Category)
}
_COLOR_CATEGORIES = {color: category for category, color in _COLORS.items()}
_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _hex(category: Category) -> str:
    return "#" + "".join(f"{channel:02X}" for channel in _COLORS[category])


def _theme() -> str:
    rules = "".join(
        f"<dict><key>scope</key><string>{scope}</string><key>settings</key><dict>"
        f"<key>foreground</key><string>{_hex(category)}</string></dict></dict>"
        for category, scope in TEXTMATE_SCOPES.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
        f"<key>name</key><string>{_THEME}</string><key>settings</key><array>"
        f"<dict><key>settings</key><dict><key>foreground</key><string>{_hex(Category.TEXT)}"
        "</string><key>background</key><string>#000000</string></dict></dict>"
        f"{rules}</array></dict></plist>\n"
    )


def _decode(output: str) -> EngineResult:
    """Read SGR foregrounds back into categories; reset text is TEXT."""
    text: list[str] = []
    colors: list[Category] = []
    category = Category.TEXT
    position = 0
    for match in _SGR.finditer(output):
        chunk = output[position : match.start()]
        text.append(chunk)
        colors.extend([category] * len(chunk))
        position = match.end()
        parameters = match.group()[2:-1].split(";")
        if parameters[:2] == ["38", "2"]:
            category = _COLOR_CATEGORIES[
                (int(parameters[2]), int(parameters[3]), int(parameters[4]))
            ]
        elif parameters in (["0"], [""]):
            category = Category.TEXT
        else:
            raise ValueError(f"Unexpected bat SGR sequence: {match.group()!r}")
    chunk = output[position:]
    text.append(chunk)
    colors.extend([category] * len(chunk))
    return EngineResult("".join(text), spans_from_categories(colors))


def run_corpus(
    definition: Path,
    cases: tuple[CorpusCase, ...] = CORPUS,
    *,
    grammar: Grammar = AGL_GRAMMAR,
) -> None:
    """Compare exact LF-normalized text/styles: bat splits lines at LF only.

    Normalize before both bat and reference evaluation, not in the shared
    engine-neutral contract, as the KDE probe does.
    """
    bat = bat_executable()
    assert bat is not None, "bat is not installed"
    with tempfile.TemporaryDirectory() as directory:
        assets = Path(directory) / "assets"
        (assets / "syntaxes").mkdir(parents=True)
        (assets / "themes").mkdir()
        shutil.copy2(definition, assets / "syntaxes" / "agl.sublime-syntax")
        (assets / "themes" / f"{_THEME}.tmTheme").write_text(_theme(), encoding="utf-8")
        cache = Path(directory) / "cache"
        subprocess.run(
            [bat, "cache", "--build", "--blank", "--source", assets, "--target", cache],
            capture_output=True,
            check=True,
            timeout=30,
        )
        # Ignore the user's bat configuration and environment.
        env = {key: value for key, value in os.environ.items() if not key.startswith("BAT_")}
        env |= {"BAT_CACHE_PATH": str(cache), "BAT_CONFIG_PATH": os.devnull}
        env["COLORTERM"] = "truecolor"
        for case in cases:
            source = case.source.replace("\r\n", "\n").replace("\r", "\n")
            process = subprocess.run(
                [
                    bat,
                    "--color=always",
                    "--style=plain",
                    "--paging=never",
                    "--wrap=never",
                    "--tabs=0",
                    f"--theme={_THEME}",
                    "--language=agl",
                ],
                input=source,
                text=True,
                encoding="utf-8",
                capture_output=True,
                env=env,
                check=True,
                timeout=30,
            )
            assert_result(CorpusCase(case.name, source), _decode(process.stdout), grammar=grammar)
