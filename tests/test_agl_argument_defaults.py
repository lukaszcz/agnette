"""Function defaults read earlier parameters at each call."""

from pathlib import Path

import pytest

from agnette.agl.semantics.values import IntValue
from agnette.agl.typecheck.env import AglTypeError
from tests.agl.ir_harness import evaluate_ir, evaluate_ir_with_externs, lower_inline_ir


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("f()", 8),
        ("f(12)", 13),
        ("f(x = 20)", 21),
        ("f(y = 30, x = 20)", 31),
        ("f(z = 40)", 40),
    ],
)
def test_defaults_follow_bound_values(call: str, expected: int) -> None:
    source = (
        "let x = 100\n"
        "def f(x: int = 7, y: int = x, z: int = y + 1) -> int = z\n"
        f"let result = {call}\n()\n"
    )
    assert evaluate_ir(source)["result"] == IntValue(expected)


def test_default_uses_callee_parameter_not_caller_parameter() -> None:
    source = (
        "def f(x: int = 7, y: int = x) -> int = y\n"
        "def caller(x: int) -> int = f()\n"
        "let result = caller(99)\n()\n"
    )
    assert evaluate_ir(source)["result"] == IntValue(7)


@pytest.mark.parametrize("call", ["f(9)", "partial(9)"])
def test_generic_defaults_through_direct_and_partial_calls(call: str) -> None:
    source = (
        "def f[T](x: T, y: T = x) -> T = y\n"
        "let partial: int -> int = f::[int](?)\n"
        f"let result = {call}\n()\n"
    )
    assert evaluate_ir(source)["result"] == IntValue(9)


@pytest.mark.parametrize("call", ["box.read()", "Box::read(box)"])
def test_method_default_reads_self(call: str) -> None:
    source = (
        "record Box\n  value: int\n"
        "def Box::read(self, value: int = self.value) -> int = value\n"
        "let box = Box(value = 23)\n"
        f"let result = {call}\n()\n"
    )
    assert evaluate_ir(source)["result"] == IntValue(23)


@pytest.mark.parametrize("call", ["f()", "f(13)", "partial(13)"])
def test_extern_defaults_read_parameters(tmp_path: Path, call: str) -> None:
    source = (
        "extern def f(x: int = 7, y: int = x, z: int = y + 1) -> int\n"
        "let partial: int -> int = f(?)\n"
        f"let result = {call}\n()\n"
    )
    result, _ = evaluate_ir_with_externs(source, "def f(x, y, z): return z\n", tmp_path)
    assert result["result"] == IntValue(8 if call == "f()" else 14)


def test_default_closure_captures_the_earlier_argument() -> None:
    source = (
        "def f(x: int, get: () -> int = (fn() => x)) -> () -> int = get\n"
        "let first = f(7)\n"
        "let second = f(12)\n"
        "let a = first()\n"
        "let b = second()\n()\n"
    )
    result = evaluate_ir(source)
    assert result["a"] == IntValue(7)
    assert result["b"] == IntValue(12)


def test_default_does_not_reevaluate_the_earlier_argument() -> None:
    source = (
        "var calls = 0\n"
        "def next() -> int =\n  calls := calls + 1\n  calls\n"
        "def f(x: int = next(), y: int = x) = y\n"
        "let a = f()\n"
        "let b = f()\n()\n"
    )
    result = evaluate_ir(source)
    assert result["a"] == IntValue(1)
    assert result["b"] == IntValue(2)
    assert result["calls"] == IntValue(2)


def test_parameter_default_is_typechecked_against_its_parameter() -> None:
    with pytest.raises(AglTypeError):
        lower_inline_ir("def f(x: int, y: text = x) -> text = y\n()\n")
