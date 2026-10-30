"""Parser callbacks keep per-rule positions and per-parse provenance isolated."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

import pytest
from lark import GrammarError, v_args
from lark.exceptions import VisitError
from lark.tree import Meta

from agnette.agl.parser import AglSyntaxError, parse_program_seeded, parse_type_entry_seeded
from agnette.agl.parser import parser as parser_module
from agnette.agl.parser.parser import parse_program
from agnette.agl.parser.transform import AstBuilder
from agnette.agl.syntax import Call, IntLit, Program
from agnette.agl.syntax.spans import UNKNOWN_SOURCE, SourceId, SourceSpan
from agnette.agl.syntax.visitor import SyntaxNode, walk


class _LocatedNode(Protocol):
    @property
    def node_id(self) -> int: ...

    @property
    def span(self) -> SourceSpan: ...


def _nodes(root: SyntaxNode) -> list[_LocatedNode]:
    nodes: list[_LocatedNode] = []
    walk(root, lambda node: nodes.append(cast(_LocatedNode, node)))
    return nodes


def test_repeated_rules_keep_each_occurrences_full_span() -> None:
    text = "f(11, 222)\nf(3333, 44)\n"
    source = SourceId("<repeated>")
    nodes = _nodes(parse_program(text, source=source))
    repeated = [node for node in nodes if isinstance(node, (Call, IntLit))]
    assert [node.span for node in repeated] == [
        SourceSpan(1, 1, 1, 11, 0, 10),
        SourceSpan(1, 3, 1, 5, 2, 4),
        SourceSpan(1, 7, 1, 10, 6, 9),
        SourceSpan(2, 1, 2, 12, 11, 22),
        SourceSpan(2, 3, 2, 7, 13, 17),
        SourceSpan(2, 9, 2, 11, 19, 21),
    ]
    assert all(node.span.source is source for node in nodes)
    assert [text[node.span.start_offset : node.span.end_offset] for node in repeated] == [
        "f(11, 222)",
        "11",
        "222",
        "f(3333, 44)",
        "3333",
        "44",
    ]


@pytest.mark.parametrize(
    ("invalid", "error_range"),
    [
        ("f(11, a = 22, 333)", (14, 17)),
        ("let x =", (6, 7)),
        ('"unterminated', (13, 13)),
    ],
)
def test_sources_and_seeded_ids_stay_isolated_across_failures(
    invalid: str, error_range: tuple[int, int]
) -> None:
    text = "f(11, 222)\nf(3333, 44)\n"
    first_source = SourceId("<first>")
    error_source = SourceId("<error>")
    second_source = SourceId("<second>")
    first, next_id = parse_program_seeded(text, start_id=100, source=first_source)
    first_nodes = _nodes(first)
    first_ids = [node.node_id for node in first_nodes]

    with pytest.raises(AglSyntaxError) as caught:
        parse_program_seeded(invalid, start_id=9000, source=error_source)
    span = caught.value.span
    assert span.source is error_source
    assert (span.start_offset, span.end_offset) == error_range

    second, final_id = parse_program_seeded(text, start_id=next_id, source=second_source)
    second_nodes = _nodes(second)
    second_ids = [node.node_id for node in second_nodes]
    assert len(first_ids) == len(set(first_ids))
    assert len(second_ids) == len(set(second_ids))
    assert all(100 <= node_id < next_id for node_id in first_ids)
    assert all(next_id <= node_id < final_id for node_id in second_ids)
    assert set(first_ids).isdisjoint(second_ids)
    assert all(node.span.source is first_source for node in first_nodes)
    assert all(node.span.source is second_source for node in second_nodes)
    assert [node.span for node in first_nodes] == [node.span for node in second_nodes]

    default_nodes = _nodes(parse_program(text))
    assert all(node.span.source is UNKNOWN_SOURCE for node in default_nodes)
    assert [node.node_id for node in default_nodes] == [node_id - 100 for node_id in first_ids]


def test_type_and_program_callbacks_do_not_share_builder_state() -> None:
    type_source = SourceId("<type>")
    program_source = SourceId("<program>")
    attributes, parsed_type, next_id = parse_type_entry_seeded(
        "Pair[Left, Right]", start_id=500, source=type_source
    )
    assert attributes == ()
    type_nodes = _nodes(parsed_type)
    assert all(node.span.source is type_source for node in type_nodes)
    assert all(500 <= node.node_id < next_id for node in type_nodes)

    program, program_next_id = parse_program_seeded(
        "f(1, 2)", start_id=next_id, source=program_source
    )
    program_nodes = _nodes(program)
    assert all(node.span.source is program_source for node in program_nodes)
    assert all(next_id <= node.node_id < program_next_id for node in program_nodes)

    _, repeated_type, repeated_next_id = parse_type_entry_seeded("Pair[Left, Right]", start_id=500)
    repeated_nodes = _nodes(repeated_type)
    assert repeated_next_id == next_id
    assert repeated_type == parsed_type
    assert [node.node_id for node in repeated_nodes] == [node.node_id for node in type_nodes]
    assert [node.span for node in repeated_nodes] == [node.span for node in type_nodes]
    assert all(node.span.source is UNKNOWN_SOURCE for node in repeated_nodes)
    assert all(node.span.source is type_source for node in type_nodes)


def test_per_rule_arguments_override_class_arguments_without_redecorating_inherited_rules() -> None:
    # The local stub types v_args only as a class decorator; its public API also
    # decorates methods without changing their direct-call signature.
    def meta_rule[**P, R](method: Callable[P, R]) -> Callable[P, R]:
        decorate = cast(Callable[[Callable[P, R]], Callable[P, R]], v_args(meta=True))
        return decorate(method)

    @v_args(inline=True)
    class PerRuleBuilder(AstBuilder):
        @meta_rule
        def start(self, meta: Meta, args: list[object]) -> Program:
            return super().start(meta, args)

    text = "f(11, 222)\nf(3333, 44)\n"
    expected = parse_program(text)
    actual = PerRuleBuilder().transform(parser_module._PARSER.parse(text))
    assert isinstance(actual, Program)
    assert actual == expected
    assert [node.span for node in _nodes(actual)] == [node.span for node in _nodes(expected)]
    assert [node.node_id for node in _nodes(actual)] == [node.node_id for node in _nodes(expected)]


@pytest.mark.parametrize("error_type", [GrammarError, RuntimeError])
def test_callback_error_dispatch_is_unchanged(error_type: type[Exception]) -> None:
    failure = error_type("callback failed")

    @v_args(meta=True)
    class FailedBuilder(AstBuilder):
        def start(self, meta: Meta, args: list[object]) -> Program:
            raise failure

    expected = GrammarError if isinstance(failure, GrammarError) else VisitError
    with pytest.raises(expected) as caught:
        FailedBuilder().transform(parser_module._PARSER.parse("f(1, 2)"))
    if isinstance(caught.value, GrammarError):
        assert caught.value is failure
    else:
        assert caught.value.orig_exc is failure
