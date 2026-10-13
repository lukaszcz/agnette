"""Compare bat/syntect output for a Sublime syntax with the portable grammar reference."""

import argparse
import subprocess
import sys
from pathlib import Path

# Keep direct script execution and module execution on the same package imports.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.highlight_engine_support import CORPUS
from tools.highlight_sublime_support import run_corpus
from tools.install_highlighting import bat_executable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("definition", type=Path, help="generated Sublime syntax")
    args = parser.parse_args()
    definition: Path = args.definition
    bat = bat_executable()
    if bat is None:
        raise RuntimeError("The optional Sublime suite requires bat")
    version = subprocess.run(
        [bat, "--version"], capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()
    run_corpus(definition)
    print(f"{version}/syntect: {len(CORPUS)} cases match grammar_eval")


if __name__ == "__main__":
    main()
