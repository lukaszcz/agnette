"""Whole-program binding preparation shares facts, never mutable environments."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.modules.ids import ModuleId
from agnette.agl.repl import ReplSession
from agnette.agl.scope.program import resolve_program
from agnette.agl.semantics.values import IntValue, TextValue, Value
from agnette.agl.typecheck.program import check_program
from tests._agl_helpers import agl_roots
from tests.agl.ir_harness import make_inline_graph_from_files, run_inline_ir, write_module_file


@pytest.mark.parametrize("change", ["entry", "dependency", "root"])
def test_changed_sources_and_roots_replace_binding_seed_types(tmp_path: Path, change: str) -> None:
    root = tmp_path / "first"
    write_module_file(root, "dependency", "let value = 3\n")
    library = "import dependency\ndef get() = dependency::value\n"
    write_module_file(root, "library", library)
    source = "import library\nlet answer = library::get()\n"

    def run() -> Value:
        result, _ = run_inline_ir(
            source, roots=agl_roots(root, include_stdlib=False), default_stdlib=False
        )
        assert result.ok, result.diagnostics
        return result.bindings["answer"]

    assert run() == IntValue(3)
    assert run() == IntValue(3)
    if change == "entry":
        source = 'import library\nlet answer = "new"\n'
    else:
        if change == "root":
            root = tmp_path / "second"
            write_module_file(root, "library", library)
        write_module_file(root, "dependency", 'let value = "new"\n')
    assert run() == TextValue("new")
    assert run() == TextValue("new")


def test_rehydrated_imports_keep_fresh_envs_and_directional_candidate_publication(
    tmp_path: Path,
) -> None:
    graph = make_inline_graph_from_files(
        tmp_path,
        {
            "base": "let start = 1\ndef get() = start\n",
            "consumer": "import base\ndef doubled() = base::get() * 2\n",
            "entry": "import consumer\nlet answer = consumer::doubled()\n",
        },
        default_stdlib=False,
    )
    resolved = resolve_program(graph)
    original = check_program(resolved)
    restored = check_program(
        resolved,
        cached_checked_modules={
            mid: module.image()
            for mid, module in original.modules.items()
            if mid != original.entry_id
        },
    )
    base = restored.modules[ModuleId.from_path("base")]
    consumer = restored.modules[ModuleId.from_path("consumer")]
    entry = restored.modules[restored.entry_id]
    for mid, module in restored.modules.items():
        assert module.type_env is not original.modules[mid].type_env
        assert module.type_env.type_table is entry.type_env.type_table
    get_id = next(iter(base.published_signatures or {}))
    doubled_id = next(iter(consumer.published_signatures or {}))
    assert consumer.type_env.binding_type_of(get_id) == base.type_env.binding_type_of(get_id)
    assert entry.type_env.binding_type_of(doubled_id) == consumer.type_env.binding_type_of(
        doubled_id
    )
    assert base.type_env.get_binding_type(doubled_id) is None


def test_repl_seed_and_retired_nominals_survive_shared_imported_bindings(tmp_path: Path) -> None:
    write_module_file(
        tmp_path,
        "library",
        "record Box\n  value: int\nlet base = 5\ndef get() = Box(value = base)\n",
    )
    session = ReplSession(cwd=tmp_path, default_stdlib=False)
    for source in (
        "import library",
        "record Box\n  old: int",
        "let previous = Box(old = 7)",
        "record Box\n  fresh: text",
        'let current = Box(fresh = "new")',
    ):
        result = session.eval_entry(source)
        assert result.ok, result.diagnostics
    assert session.eval_entry("previous.old + library::get().value").value == IntValue(12)
    assert session.eval_entry("current.fresh").value == TextValue("new")
    assert not session.eval_entry("current.old").ok
    assert session.eval_entry("previous.old").value == IntValue(7)
