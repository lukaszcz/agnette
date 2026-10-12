"""Compare native TextMate/Oniguruma scopes with the portable grammar reference."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TypedDict, cast

# Keep direct script execution and module execution on the same package imports.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agnette.agl.highlight.categories import Category
from agnette.agl.highlight.targets.textmate import TEXTMATE_SCOPES
from tools.highlight_engine_support import (
    CORPUS,
    CorpusCase,
    EngineResult,
    assert_result,
    spans_from_categories,
)

TOKENIZER = Path(__file__).resolve().parents[1] / "config/textmate/tests/tokenize.cjs"
SCOPE_CATEGORIES = {scope: category for category, scope in TEXTMATE_SCOPES.items()}


class _Token(TypedDict):
    start: int
    end: int
    text: str
    scopes: list[str]


def run_corpus(definition: Path, cases: tuple[CorpusCase, ...] = CORPUS) -> None:
    sources = [case.source for case in cases]
    process = subprocess.run(
        ["node", str(TOKENIZER), str(definition)],
        input=json.dumps(sources),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=30,
    )
    outputs = cast(list[list[_Token]], json.loads(process.stdout))
    for case, tokens in zip(cases, outputs, strict=True):
        colors: list[Category] = []
        for token in tokens:
            assert token["start"] == len(colors), (case.name, "noncontiguous token", token)
            assert token["text"] == case.source[token["start"] : token["end"]], (
                case.name,
                "token offsets differ from original source",
                token,
            )
            category = Category.TEXT
            for index, scope in enumerate(token["scopes"]):
                if index == 0 and scope == "source.agl":
                    continue
                if scope not in SCOPE_CATEGORIES:
                    raise ValueError(f"{case.name}: unknown TextMate scope {scope!r}: {token}")
                category = SCOPE_CATEGORIES[scope]
            colors.extend([category] * len(token["text"]))
        assert_result(
            case,
            EngineResult("".join(token["text"] for token in tokens), spans_from_categories(colors)),
        )


def assert_unknown_scopes_rejected(definition: Path) -> None:
    """Native malformed grammars must not inherit a known category or plain text."""
    grammar = definition.read_text(encoding="utf-8")
    probes = (
        ("unknown-text", "meta.text.agl", "unknown.scope.agl", " "),
        ("unknown-child", "string.quoted.agl", "string.quoted.agl unknown.scope.agl", '"x"'),
        ("unknown-parent", "meta.text.agl", "unknown.scope.agl meta.text.agl", " "),
        ("nested-wrapper", "meta.text.agl", "meta.text.agl source.agl", " "),
        ("unknown-wrapper", "source.agl", "unknown.scope.agl", " "),
    )
    with tempfile.TemporaryDirectory() as directory:
        malformed = Path(directory) / "malformed.tmLanguage.json"
        for name, original, replacement, source in probes:
            malformed.write_text(grammar.replace(original, replacement), encoding="utf-8")
            try:
                run_corpus(malformed, (CorpusCase(name, source),))
            except ValueError:
                continue
            raise AssertionError(f"native TextMate accepted {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("definition", type=Path, help="generated TextMate JSON")
    args = parser.parse_args()
    definition: Path = args.definition
    run_corpus(definition)
    assert_unknown_scopes_rejected(definition)
    print(
        f"TextMate 9.2.0/Oniguruma 2.0.1: {len(CORPUS)} cases match grammar_eval; "
        "unknown scopes rejected"
    )


if __name__ == "__main__":
    main()
