"""Compare Pandoc 3.x/skylighting output with the portable grammar reference."""

import argparse
import subprocess
import sys
from pathlib import Path

# Keep direct script execution and module execution on the same package imports.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.highlight_engine_support import CORPUS
from tools.highlight_kde_support import run_corpus


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("definition", type=Path, help="generated KDE XML")
    args = parser.parse_args()
    definition: Path = args.definition
    version = subprocess.run(
        ["pandoc", "--version"], capture_output=True, text=True, check=True, timeout=10
    ).stdout.splitlines()[0]
    if not version.startswith("pandoc 3."):
        raise RuntimeError(f"The optional KDE suite requires Pandoc 3.x; found {version}")
    run_corpus(definition)
    print(f"{version}/skylighting: {len(CORPUS)} cases match grammar_eval (KDE style projection)")


if __name__ == "__main__":
    main()
