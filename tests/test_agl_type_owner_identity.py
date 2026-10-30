"""Declaration identity through real scope resolution, without constructor use sites."""

from __future__ import annotations

import pytest

from agnette.agl.scope.program import resolve_program
from tests.agl.module_graph import build_module_graph


@pytest.mark.parametrize(
    ("name", "identity"),
    [
        ("Base", "Base"),
        ("Middle", "Base"),
        ("Outer", "Base"),
        ("Box", "Box"),
        ("Generic", "Box"),
        ("OuterGeneric", "Box"),
        ("Specialized", "Specialized"),
        ("RenamedSpecialized", "Specialized"),
        ("Missing", "Missing"),
    ],
)
def test_identity_follows_renamings_but_preserves_distinct_types(name: str, identity: str) -> None:
    graph, _ = build_module_graph(
        "record Base\n"
        "  value: int\n"
        "type Middle = Base\n"
        "type Outer = Middle\n"
        "record Box[T]\n"
        "  value: T\n"
        "type Generic[T] = Box[T]\n"
        "type OuterGeneric[U] = Generic[U]\n"
        "type Specialized = Box[int]\n"
        "type RenamedSpecialized = Specialized\n",
        origin_path=None,
        default_stdlib=True,
    )
    resolved = resolve_program(graph)

    assert resolved.sources(graph.entry_id).identity((graph.entry_id, (), name)) == (
        graph.entry_id,
        (),
        identity,
    )
