"""Library IR reuse preserves REPL identities without sharing runtime state."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agnette.agl.eval.ir_interpreter import Frame, IrInterpreter
from agnette.agl.ir.program import ExecutableProgram
from agnette.agl.ir.validate import validate_ir
from agnette.agl.lower import LinkImage, lower_repl_program
from agnette.agl.lower import module as module_cache
from agnette.agl.lower import program as lowering
from agnette.agl.matchcompile import MatchCompiledProgram, compile_program_matches
from agnette.agl.modules.ids import ModuleId
from agnette.agl.repl import ReplSession
from agnette.agl.scope.program import resolve_program
from agnette.agl.scope.symbols import ScopeNode
from agnette.agl.semantics.values import IntValue
from agnette.agl.syntax.resources import ResourceError
from agnette.agl.typecheck.env import TypeEnvironment
from agnette.agl.typecheck.program import check_program
from tests._agl_helpers import agl_roots
from tests.agl.module_graph import load_graph


def _compiled(root: Path, source: str, *, default_stdlib: bool = True) -> MatchCompiledProgram:
    graph = load_graph(
        source, entry_path=None, roots=agl_roots(root), default_stdlib=default_stdlib
    )
    resolved = resolve_program(
        graph,
        entry_repl_session_scope=ScopeNode(node_id=-1, parent=None, scope_path=()),
    )
    result = compile_program_matches(check_program(resolved, entry_seed_env=TypeEnvironment()))
    assert result.compiled is not None
    return result.compiled


@pytest.fixture
def programs(monkeypatch: pytest.MonkeyPatch) -> list[ExecutableProgram]:
    captured: list[ExecutableProgram] = []

    def record(program: ExecutableProgram, *, deep: bool = False) -> None:
        validate_ir(program, deep=deep)
        captured.append(program)

    monkeypatch.setattr(lowering, "validate_ir", record)
    return captured


def test_bootstrap_ir_is_reused_with_different_entry_allocations(
    tmp_path: Path, programs: list[ExecutableProgram]
) -> None:
    first = ReplSession(cwd=tmp_path)
    second = ReplSession(cwd=tmp_path)
    assert first.open() == second.open() == ()
    try:
        assert first.eval_entry("let x = 1\nlet y = 2\nx + y").value == IntValue(3)
        initial = programs[-1]
        assert second.eval_entry("40 + 2").value == IntValue(42)
        later = programs[-1]
        libraries = set(initial.modules) - {initial.entry_module}
        assert libraries
        assert all(initial.modules[mid] is later.modules[mid] for mid in libraries)
        assert initial.modules[initial.entry_module] is not later.modules[later.entry_module]
    finally:
        first.close()
        second.close()


def test_retry_after_partial_library_initialization_keeps_closures_and_locals(
    tmp_path: Path, programs: list[ExecutableProgram]
) -> None:
    (tmp_path / "worker.agl").write_text(
        "def add(n: int, m: int) -> int =\n"
        "  let total = n + m\n"
        "  let plus = fn(x: int) -> int => total + x\n"
        "  plus(1)\n"
    )
    source = "import worker\nworker::add(20, 21)"
    compiled = _compiled(tmp_path, source)
    image = LinkImage()
    initial = lower_repl_program(compiled, image=image, source_text=source).program
    snapshot = image.snapshot_state()
    # An interrupted module was allocated but never marked linked. Relowering
    # must not overwrite descriptors still retained in the image.
    retried = lower_repl_program(compiled, image=image, source_text=source).program
    assert initial.functions.items() <= retried.functions.items()
    image.restore_state(snapshot)
    repeated = lower_repl_program(compiled, image=image, source_text=source).program
    assert repeated.functions == retried.functions
    assert repeated.symbols == retried.symbols
    cached_image = LinkImage()
    lower_repl_program(compiled, image=cached_image, source_text=source)
    cached_retry = lower_repl_program(compiled, image=cached_image, source_text=source).program
    assert cached_retry.functions == retried.functions
    assert cached_retry.symbols == retried.symbols
    interp = IrInterpreter(repeated)
    interp.run()
    assert interp.module_initializer_values[repeated.entry_module][-1] == IntValue(42)


def test_edited_unmarked_library_capture_reuses_only_its_checked_version(tmp_path: Path) -> None:
    library = tmp_path / "worker.agl"
    (tmp_path / "worker.py").write_text("def query(contract, question): return 42\n")
    extern = "extern def query[T](question: text) -> T\n"
    library.write_text(
        extern + "record Old\n  value: int = 1\n"
        "def answer() -> int = Old().value\n"
        'def unused() -> int = exec("unused")\n'
        'def unused-target() -> Old = query("unused")\n'
        "let obsolete = 1\n"
    )
    source = "import worker\nworker::answer()"
    image = LinkImage()
    previous = lower_repl_program(
        _compiled(tmp_path, source), image=image, source_text=source
    ).program
    (tmp_path / "shared.agl").write_text("record Token\n  value: int = 42\n")
    library.write_text(
        "import shared\n" + extern + "record Box\n  token: shared::Token = shared::Token()\n"
        "enum Result\n  | shared::Token\n"
        "def answer(value: int = Box().token.value) -> int = value\n"
        'def unused() -> Box = exec("unused")\n'
        'def unused-target() -> Box = query("unused")\n'
    )
    compiled = _compiled(tmp_path, source)
    changed = lower_repl_program(compiled, image=image, source_text=source).program
    assert previous.functions.items() <= changed.functions.items()
    assert previous.symbols.items() <= changed.symbols.items()
    assert previous.nominals.keys() <= changed.nominals.keys()
    reused = lower_repl_program(compiled, image=LinkImage(), source_text=source).program
    worker = ModuleId(("worker",))
    assert reused.modules[worker] is changed.modules[worker]
    old_functions = {fid for fid, fn in previous.functions.items() if fn.module_id == worker}
    old_symbols = {
        sid
        for sid, symbol in previous.symbols.items()
        if symbol.owner == worker or symbol.owner in old_functions
    }
    old_nominals = {
        nid for nid, nominal in previous.nominals.items() if nominal.module_id == worker
    }
    assert not old_functions & reused.functions.keys()
    assert not old_symbols & reused.symbols.keys()
    assert not old_nominals & reused.nominals.keys()
    assert reused.contracts and not previous.contracts.keys() & reused.contracts.keys()
    assert reused.target_contracts
    assert not previous.target_contracts.keys() & reused.target_contracts.keys()
    modules = dict(compiled.checked.modules)
    modules[worker] = replace(modules[worker])
    pristine = lower_repl_program(
        replace(compiled, checked=replace(compiled.checked, modules=modules)),
        image=LinkImage(),
        source_text=source,
    ).program
    assert reused.functions == pristine.functions
    assert reused.symbols == pristine.symbols
    assert reused.nominals == pristine.nominals
    assert reused.contracts == pristine.contracts
    assert reused.target_contracts == pristine.target_contracts
    interp = IrInterpreter(reused)
    interp.run()
    assert interp.module_initializer_values[reused.entry_module][-1] == IntValue(42)


def test_cached_library_functions_contracts_defaults_and_mutables_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "worker.agl").write_text(
        "record Box\n  value: int = 10\n"
        "var numbers: array[int] = [0]\n"
        "def tick() -> int =\n"
        "  numbers[0] := numbers[0] + 1\n"
        "  let base = Box().value\n"
        "  let plus = fn(n: int) -> int => base + n\n"
        "  plus(numbers[0])\n"
        'def unused-contract() -> int = exec("unused")\n'
    )
    source = "import worker\nworker::tick()"
    compiled = _compiled(tmp_path, source)

    def forbid_persistence(*args: object, **kwargs: object) -> None:
        pytest.fail("session IR must stay in memory")

    monkeypatch.setattr("agnette.agl.artifact_serialization.save", forbid_persistence)
    first = lower_repl_program(compiled, image=LinkImage(), source_text=source).program
    second = lower_repl_program(compiled, image=LinkImage(), source_text=source).program
    worker = ModuleId(("worker",))
    assert first.modules[worker] is second.modules[worker]
    assert first.contracts and first.contracts == second.contracts
    assert first.functions == second.functions
    assert first.sources == second.sources
    assert first.nominals == second.nominals
    for program in (first, second):
        interp = IrInterpreter(program)
        interp.run()
        assert interp.module_initializer_values[program.entry_module][-1] == IntValue(11)


def test_new_checked_owner_does_not_reuse_session_ir(tmp_path: Path) -> None:
    (tmp_path / "worker.agl").write_text("def answer() -> int = 42\n")
    source = "import worker\nworker::answer()"
    compiled = _compiled(tmp_path, source)
    worker = ModuleId(("worker",))
    first = lower_repl_program(compiled, image=LinkImage(), source_text=source).program
    modules = dict(compiled.checked.modules)
    modules[worker] = replace(modules[worker])
    changed = replace(compiled, checked=replace(compiled.checked, modules=modules))
    second = lower_repl_program(changed, image=LinkImage(), source_text=source).program
    assert first.modules[worker] is not second.modules[worker]


def test_resource_cache_hit_revalidates_and_tracks_the_resource_root(tmp_path: Path) -> None:
    (tmp_path / "worker.agl").write_text(
        'builtin def resource(path: text) -> text\ndef asset() -> text = resource("asset.txt")\n'
    )
    asset = tmp_path / "asset.txt"
    asset.write_text("present")
    source = "import worker"
    compiled = _compiled(tmp_path, source, default_stdlib=False)
    first = lower_repl_program(compiled, image=LinkImage(), source_text=source).program
    worker = ModuleId(("worker",))
    second = lower_repl_program(compiled, image=LinkImage(), source_text=source).program
    assert first.modules[worker] is second.modules[worker]
    target = tmp_path / "changed.txt"
    target.write_text("changed")
    asset.unlink()
    asset.symlink_to(target)
    retargeted = lower_repl_program(compiled, image=LinkImage(), source_text=source).program
    assert retargeted.modules[worker] is not first.modules[worker]
    assert retargeted.functions != first.functions
    asset.unlink()
    with pytest.raises(ResourceError):
        lower_repl_program(compiled, image=LinkImage(), source_text=source)
    asset.write_text("restored")
    other = tmp_path / "other"
    other.mkdir()
    (other / "asset.txt").write_text("other")
    roots = dict(compiled.checked.resource_roots)
    roots[worker] = other
    relocated = replace(compiled, checked=replace(compiled.checked, resource_roots=roots))
    third = lower_repl_program(relocated, image=LinkImage(), source_text=source).program
    assert third.modules[worker] is not first.modules[worker]
    assert third.functions != first.functions


def test_dependency_retry_handles_are_part_of_the_cache_context(tmp_path: Path) -> None:
    (tmp_path / "dependency.agl").write_text("def answer() -> int = 42\n")
    (tmp_path / "worker.agl").write_text(
        "import dependency\ndef answer() -> int = dependency::answer()\n"
    )
    source = "import worker\nworker::answer()"
    compiled = _compiled(tmp_path, source)
    cached = lower_repl_program(compiled, image=LinkImage(), source_text=source).program
    image = LinkImage()
    dependency_source = "import dependency"
    dependency = _compiled(tmp_path, dependency_source)
    lower_repl_program(dependency, image=image, source_text=dependency_source)
    # Keep a retried dependency but not its original allocation handles.
    retried = lower_repl_program(dependency, image=image, source_text=dependency_source).program
    frame: Frame = {}
    IrInterpreter(retried, base_frame=frame).run()
    image.mark_linked(set(dependency.checked.modules) - {dependency.checked.entry_id})
    linked = lower_repl_program(compiled, image=image, source_text=source).program
    worker = ModuleId(("worker",))
    assert cached.modules[worker] is not linked.modules[worker]
    interp = IrInterpreter(linked, base_frame=frame)
    interp.run()
    assert interp.module_initializer_values[linked.entry_module][-1] == IntValue(42)


def test_link_failure_rolls_back_allocations_before_a_retry(tmp_path: Path) -> None:
    (tmp_path / "worker.agl").write_text(
        'builtin def resource(path: text) -> text\ndef asset() -> text = resource("asset.txt")\n'
    )
    session = ReplSession(cwd=tmp_path, default_stdlib=False)
    assert session.open() == ()
    try:
        rejected = session.eval_entry("import worker")
        assert not rejected.ok and rejected.diagnostics
        (tmp_path / "asset.txt").write_text("present")
        accepted = session.eval_entry("import worker\nworker::asset()")
        assert accepted.ok, accepted.diagnostics
    finally:
        session.close()


def test_bounded_session_cache_eviction_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module_cache, "_SESSION_CAPACITY", 1)
    (tmp_path / "worker.agl").write_text("def answer() -> int = 42\n")
    source = "import worker\nworker::answer()"
    compiled = _compiled(tmp_path, source)
    for _ in range(2):
        program = lower_repl_program(compiled, image=LinkImage(), source_text=source).program
        interp = IrInterpreter(program)
        interp.run()
        assert interp.module_initializer_values[program.entry_module][-1] == IntValue(42)


def test_new_import_after_entries_and_reset_does_not_share_library_values(
    tmp_path: Path,
) -> None:
    module = tmp_path / "counter.agl"
    source = (
        "var count = 0\n"
        "def tick(n: int) -> int =\n"
        "  let bump = fn(x: int) -> int => x + n\n"
        "  count := bump(count)\n"
        "  count\n"
    )
    module.write_text(source)
    sessions = [ReplSession(cwd=tmp_path) for _ in range(2)]
    try:
        for index, session in enumerate(sessions):
            assert session.open() == ()
            for n in range(index + 1):
                assert session.eval_entry(f"let before{n} = {n}").ok
            result = session.eval_entry("import counter\ncounter::tick(2)")
            assert result.ok, result.diagnostics
            assert result.value == IntValue(2)
        assert sessions[0].eval_entry("counter::tick(3)").value == IntValue(5)
        sessions[1].reset()
        module.write_text(source.replace("var count = 0", "var count = 10"))
        result = sessions[1].eval_entry("import counter\ncounter::tick(2)")
        assert result.ok, result.diagnostics
        assert result.value == IntValue(12)
        assert sessions[0].eval_entry("counter::tick(1)").value == IntValue(6)
    finally:
        for session in sessions:
            session.close()


def test_entry_builtin_declarations_separate_library_cache_contexts(
    tmp_path: Path, programs: list[ExecutableProgram]
) -> None:
    sessions = [ReplSession(cwd=tmp_path) for _ in range(3)]
    try:
        for session in sessions:
            assert session.open() == ()
        declared = sessions[0].eval_entry(
            "scope Custom\n"
            "  builtin record ExecResult\n"
            "    stdout: text\n"
            "    exit-code: int\n"
            "    stderr: text\n"
            "    timed-out: bool\n"
            "end Custom\n"
        )
        assert declared.ok, declared.diagnostics
        custom = programs[-1]
        assert sessions[1].eval_entry("1").ok
        plain = programs[-1]
        assert sessions[2].eval_entry("2").ok
        repeated = programs[-1]
        prelude = ModuleId(("std", "prelude"))
        assert custom.builtin_nominals != plain.builtin_nominals
        assert custom.modules[prelude] is not plain.modules[prelude]
        assert plain.modules[prelude] is repeated.modules[prelude]
    finally:
        for session in sessions:
            session.close()
