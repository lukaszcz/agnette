"""Install generated AgL definitions and user-preserving Pandoc defaults."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol, cast

# Keep direct script execution and module execution on the same package imports.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agnette.config.home import agm_home_dir
from tools.install_agm_config import build_parser


class _InstallArgs(Protocol):
    force: bool
    prefix: str | None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/install_highlighting.py", parents=[build_parser()], add_help=False
    )
    args = cast(_InstallArgs, parser.parse_args(argv))
    if args.prefix is None:
        home = agm_home_dir(home=Path.home())
        data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        bin_dir = Path(
            subprocess.run(
                ["uv", "tool", "dir", "--bin"], capture_output=True, text=True, check=True
            ).stdout.removesuffix("\n")
        )
    else:
        prefix = Path(args.prefix).resolve()
        home = agm_home_dir(home=prefix, env={"AGM_HOME": str(prefix / ".agm")})
        data = prefix / ".local" / "share"
        bin_dir = prefix / "bin"

    highlighting = home / "highlighting"
    for target, filename in (("kde", "agl.xml"), ("textmate", "agl.tmLanguage.json")):
        destination = highlighting / filename
        subprocess.run(
            [str(bin_dir / "agm"), "dev", "highlight", target, "--output", str(destination)],
            check=True,
        )
        print(f"Installed {destination}")

    kde = data / "org.kde.syntax-highlighting" / "syntax" / "agl.xml"
    kde.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(highlighting / "agl.xml", kde)
    print(f"Installed {kde}")

    defaults = data / "pandoc" / "defaults" / "agl.yaml"
    if defaults.exists() and not args.force:
        print(f"Skipped {defaults}")
    else:
        defaults.parent.mkdir(parents=True, exist_ok=True)
        # JSON strings are YAML scalars, including paths with quotes or trailing spaces.
        definition = json.dumps(str((highlighting / "agl.xml").resolve()), ensure_ascii=False)
        defaults.write_text(f"syntax-definitions: [{definition}]\n", encoding="utf-8")
        print(f"Installed {defaults}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
