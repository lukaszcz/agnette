"""Cold-import workflows for Agnette's language package."""

from __future__ import annotations

import subprocess
import sys


def test_top_level_package_runs_workflows_without_eager_pipeline_import() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import agnette.agl\n"
            "assert 'agnette.agl.pipeline' not in sys.modules\n"
            "from agnette.agl import PipelineDriver\n"
            "assert PipelineDriver is not None\n"
            "from agnette.agl.lexer import tokenize\n"
            "assert tokenize('let n = 42')\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
