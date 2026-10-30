"""Share static coverage analysis, never collectors or measured execution."""

from __future__ import annotations

import os
import pickle
from collections.abc import Mapping
from pathlib import Path
from types import CodeType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from coverage.bytecode import TBranchTrails

# Control-flow and line-mapping inputs; constant values and names do not affect the result.
type _CodeKey = tuple[str, bytes, int, bytes, bytes]
type _Offsets = dict[int, int]
type _Analysis = tuple[_Offsets, _Offsets, TBranchTrails]
type _Snapshot = tuple[dict[str, _Offsets], dict[_CodeKey, _Analysis]]

ENVIRONMENT_KEY = "_AGM_TEST_COVERAGE_METADATA"
_ANALYSIS_WORKERS = 4  # Bound interpreter memory and startup CPU.
_path: Path | None = None
_installed = False


def configure(path: Path) -> None:
    """Bind the snapshot created by this pytest invocation's controller."""
    global _path
    _path = path


def launcher_environment(environment: dict[str, str]) -> dict[str, str]:
    """Pass only the configured suite snapshot, not an inherited host value."""
    environment.pop(ENVIRONMENT_KEY, None)
    if _path is not None:
        environment[ENVIRONMENT_KEY] = str(_path)
    return environment


def configure_server() -> None:
    """Consume the launcher's private snapshot path before serving children."""
    path = os.environ.pop(ENVIRONMENT_KEY, None)
    if path is not None:
        configure(Path(path))


def _code_key(code: CodeType) -> _CodeKey:
    return (
        code.co_filename,
        code.co_code,
        code.co_firstlineno,
        code.co_linetable,
        code.co_exceptiontable,
    )


def _analyze(path: Path) -> _Snapshot:
    """Build only serializable static analysis in a fresh interpreter."""
    from coverage import sysmon

    filename = str(path)
    multiline = sysmon.get_multiline_map(filename)
    analyses: dict[_CodeKey, _Analysis] = {}
    pending = [compile(path.read_bytes(), filename, "exec", dont_inherit=True)]
    while pending:
        code = pending.pop()
        pending.extend(value for value in code.co_consts if isinstance(value, CodeType))
        # Sysmon deliberately ignores deferred annotation execution.
        if code.co_name == "__annotate__":
            continue
        trails = sysmon.branch_trails(code, multiline)
        analyses[_code_key(code)] = (
            sysmon.bytes_to_lines(code),
            sysmon.always_jumps(code),
            {offset: dict(branches) for offset, branches in trails.items()},
        )
    return {filename: multiline}, analyses


def create(directory: Path) -> Path:
    """Analyze checkout bytecode without importing or executing application code."""
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    root = Path(__file__).resolve().parents[1]
    paths = (
        path
        for base in (root / "src", root / "packages" / "stdlib" / "src")
        for path in base.rglob("*.py")
    )
    source_maps: dict[str, _Offsets] = {}
    analyses: dict[_CodeKey, _Analysis] = {}
    # Spawn avoids forking the threaded xdist controller. Bound retained results too.
    with ProcessPoolExecutor(
        max_workers=_ANALYSIS_WORKERS, mp_context=get_context("spawn")
    ) as pool:
        for maps, file_analyses in pool.map(_analyze, paths, buffersize=2 * _ANALYSIS_WORKERS):
            source_maps.update(maps)
            analyses.update(file_analyses)
    snapshot = directory / "coverage-metadata.pickle"
    snapshot.write_bytes(pickle.dumps((source_maps, analyses), protocol=5))
    return snapshot


def install() -> bool:
    """Use immutable analysis from the suite's private snapshot when configured."""
    global _installed
    if _path is None:
        return False
    if _installed:
        return True
    from coverage import sysmon

    # Only the controller writes this per-invocation, private temporary file.
    source_maps, analyses = cast(_Snapshot, pickle.loads(_path.read_bytes()))
    original_multiline = sysmon.get_multiline_map
    original_lines = sysmon.bytes_to_lines
    original_jumps = sysmon.always_jumps
    original_trails = sysmon.branch_trails

    def get_multiline_map(filename: str) -> _Offsets:
        result = source_maps.get(filename)
        return result if result is not None else original_multiline(filename)

    def bytes_to_lines(code: CodeType) -> _Offsets:
        result = analyses.get(_code_key(code))
        return result[0] if result is not None else original_lines(code)

    def always_jumps(code: CodeType) -> _Offsets:
        result = analyses.get(_code_key(code))
        return result[1] if result is not None else original_jumps(code)

    def branch_trails(code: CodeType, multiline_map: Mapping[int, int]) -> TBranchTrails:
        result = analyses.get(_code_key(code))
        if result is not None and multiline_map is source_maps.get(code.co_filename):
            return result[2]
        return original_trails(code, multiline_map)

    sysmon.get_multiline_map = get_multiline_map
    sysmon.bytes_to_lines = bytes_to_lines
    sysmon.always_jumps = always_jumps
    sysmon.branch_trails = branch_trails
    _installed = True
    return True
