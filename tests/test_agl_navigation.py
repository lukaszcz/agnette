"""Go-to-definition over scope and type-check results."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.modules.roots import RootSet
from agnette.agl.navigation import Location, definitions, source_offset
from agnette.agl.pipeline import PipelineDriver

_STDLIB = Path(__file__).resolve().parents[1] / "packages/stdlib"

_LIB = """\
record Point
  x: int
  y: int

def Point::norm(self) -> int = self.x + self.y

enum Shape
  | Circle(r: int)
  | Square(side: int)

def area(s: Shape) -> int =
  case s of
    | Circle(r) => r * r
    | Square(side) => side * side

scope geo
  def one() -> int = 1
end geo

exception Oops extends Exception

def keep[T](value: T) -> T = value

def fallback(o: Option[int]) -> int =
  case o of
    | None => 0
    | Some(value) => value
"""

_MAIN = """\
import lib::*

def twice(n: int) -> int =
  let m = n + n
  m

program def main() -> unit =
  let p = Point(x = 1, y = 2)
  print(twice(p.x) + p.norm() + area(Shape::Circle(r = 3)) + geo::one())
  for i in [1, 2] do
    print(i)
  done
  try
    print(1)
  catch Oops as err =>
    print(err)
"""

_MORE = """\
import lib
import lib::{area as surface}
import lib as l
use lib::geo as g
import std/json

type Num = int
type Figure = lib::Shape

infixl +++ at prio + + 1
def +++(a: int, b: int) -> int = a + b

enum Wrapped
  | Inner(v: int)
  | lib::Point

def zone::two() -> int = 2
var zone::total: int = 0

program def main() -> unit =
  var count: Num = 0
  count := count + "abc".size() + zone::two()
  zone::total := 1
  let s = lib::Shape::Square(side = 2)
  let f: Figure = Figure::Circle(r = 1)
  let k: lib::Shape::Circle = lib::Shape::Circle(r = 1)
  print(l::area(s) + surface(f) + g::one() + lib::geo::one() + (1 +++ 2))
  let w: Wrapped = Inner(v = 1)
  print(w is Inner)
  try
    print(json::parse("1"))
  catch AgentParseError as e =>
    print(e.message)
  case w of
    | Wrapped::Inner(v) => print(v)
    | lib::Point(x, y) => print(x)
"""

_FILES = {
    "lib.agl": _LIB,
    "main.agl": _MAIN,
    "more.agl": _MORE,
    "reexport.agl": "export lib::{area as measure}\n",
}


def _cursor(text: str, needle: str, delta: int) -> int:
    assert text.count(needle) == 1, needle
    return text.index(needle) + delta


def _find(
    tmp_path: Path, entry: str, needle: str, delta: int = 0, files: dict[str, str] = _FILES
) -> list[Location]:
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    entry_path = (tmp_path / entry).resolve()
    source = files[entry]
    parsed = PipelineDriver.parse_entry(source, entry_path=entry_path)
    roots = RootSet(roots=frozenset({tmp_path.resolve()}), stdlib_roots=frozenset({_STDLIB}))
    prepared = PipelineDriver.prepare_parsed_entry(parsed, roots=roots)
    assert prepared.resolved is not None, prepared.diagnostics
    checked = PipelineDriver.typecheck_resolved(prepared.resolved)
    return definitions(prepared.resolved, checked, source, _cursor(source, needle, delta))


def _at(tmp_path: Path, name: str, needle: str, delta: int = 0) -> Location:
    text = _FILES[name]
    offset = _cursor(text, needle, delta)
    line = text.count("\n", 0, offset) + 1
    line_start = text.rfind("\n", 0, offset) + 1
    line_text = text[line_start:].split("\n", 1)[0]
    return Location((tmp_path / name).resolve(), line, offset - line_start + 1, line_text)


@pytest.mark.parametrize(
    ("entry", "needle", "delta", "target", "target_needle", "target_delta"),
    [
        # Locals, parameters, and loop and catch binders.
        ("main.agl", "n + n", 0, "main.agl", "n: int", 0),
        ("main.agl", "m\n\nprogram", 0, "main.agl", "m = n", 0),
        ("main.agl", "print(i)", 6, "main.agl", "for i", 4),
        ("main.agl", "print(err)", 6, "main.agl", "as err", 3),
        ("main.agl", "as err", 3, "main.agl", "as err", 3),
        # Declarations name themselves; the end of a name still selects it.
        ("main.agl", "def twice", 4, "main.agl", "def twice", 4),
        ("main.agl", "twice(p.x)", 5, "main.agl", "def twice", 4),
        # Imported functions, constructors, types, and scopes.
        ("main.agl", "area(Shape", 0, "lib.agl", "def area", 4),
        ("main.agl", "Point(x = 1", 0, "lib.agl", "record Point", 7),
        ("main.agl", "Shape::Circle(r = 3)", 0, "lib.agl", "enum Shape", 5),
        ("main.agl", "Shape::Circle(r = 3)", 7, "lib.agl", "Circle(r: int)", 0),
        ("main.agl", "geo::one", 0, "lib.agl", "scope geo", 6),
        ("main.agl", "geo::one", 5, "lib.agl", "def one", 4),
        ("main.agl", "catch Oops", 6, "lib.agl", "exception Oops", 10),
        # Type-directed members: fields and methods.
        ("main.agl", "p.x)", 0, "main.agl", "let p", 4),
        ("main.agl", "p.x)", 2, "lib.agl", "x: int", 0),
        ("main.agl", "p.norm", 2, "lib.agl", "Point::norm", 7),
        ("lib.agl", "self.x", 5, "lib.agl", "x: int", 0),
        # Patterns, annotations, and scope closers.
        ("lib.agl", "Circle(r) =>", 0, "lib.agl", "Circle(r: int)", 0),
        ("lib.agl", "r * r", 0, "lib.agl", "Circle(r) =>", 7),
        ("lib.agl", "s: Shape", 3, "lib.agl", "enum Shape", 5),
        ("lib.agl", "end geo", 4, "lib.agl", "scope geo", 6),
        ("lib.agl", "scope geo", 6, "lib.agl", "scope geo", 6),
        ("more.agl", "Inner(v) =>", 6, "more.agl", "Inner(v) =>", 6),
        ("more.agl", "print(x)", 6, "more.agl", "Point(x, y)", 6),
        ("more.agl", "is Inner", 3, "more.agl", "| Inner(v: int)", 2),
        ("more.agl", "Wrapped::Inner(v)", 9, "more.agl", "| Inner(v: int)", 2),
        # Imports, exports, aliases, and module paths.
        ("main.agl", "import lib", 7, "lib.agl", "record Point", 0),
        ("more.agl", "import lib\n", 7, "lib.agl", "record Point", 0),
        ("more.agl", "import lib as l", 14, "lib.agl", "record Point", 0),
        ("more.agl", "l::area", 0, "lib.agl", "record Point", 0),
        ("more.agl", "area as", 0, "lib.agl", "def area", 4),
        ("more.agl", "surface}", 0, "lib.agl", "def area", 4),
        ("more.agl", "surface(f", 0, "lib.agl", "def area", 4),
        ("reexport.agl", "area as", 0, "lib.agl", "def area", 4),
        ("reexport.agl", "measure", 0, "lib.agl", "def area", 4),
        ("reexport.agl", "lib::", 0, "lib.agl", "record Point", 0),
        # Qualified paths, type aliases, operators, and assignment.
        ("more.agl", "lib::Shape::Square", 5, "lib.agl", "enum Shape", 5),
        ("more.agl", "lib::Shape::Square", 12, "lib.agl", "Square(side: int)", 0),
        ("more.agl", "lib::geo::one", 5, "lib.agl", "scope geo", 6),
        ("more.agl", "| lib::Point\n", 2, "lib.agl", "record Point", 0),
        ("more.agl", "| lib::Point\n", 7, "lib.agl", "record Point", 7),
        ("more.agl", "Num = 0", 0, "more.agl", "type Num", 5),
        ("more.agl", "Figure::Circle", 0, "more.agl", "type Figure", 5),
        ("more.agl", "Figure::Circle", 8, "lib.agl", "Circle(r: int)", 0),
        ("more.agl", "Circle = lib", 0, "lib.agl", "Circle(r: int)", 0),
        ("more.agl", "+++ at", 0, "more.agl", "def +++", 4),
        ("more.agl", "(1 +++", 3, "more.agl", "def +++", 4),
        ("more.agl", "count :=", 0, "more.agl", "var count", 4),
        ("more.agl", "zone::total :=", 6, "more.agl", "var zone::total", 10),
        ("more.agl", "zone::total :=", 0, "more.agl", "def zone::two", 4),
    ],
)
def test_definitions_resolve_names(
    tmp_path: Path,
    entry: str,
    needle: str,
    delta: int,
    target: str,
    target_needle: str,
    target_delta: int,
) -> None:
    found = _find(tmp_path, entry, needle, delta)
    assert found == [_at(tmp_path, target, target_needle, target_delta)]


@pytest.mark.parametrize(
    ("entry", "needle", "delta", "declaration"),
    [
        ("main.agl", "print(i)", 0, "print"),
        ("more.agl", "size()", 0, "size"),
        ("more.agl", "json::parse", 6, "parse"),
        ("more.agl", "json::parse", 0, "import"),
        ("more.agl", "e.message", 2, "message"),
        ("lib.agl", "extends Exception", 8, "exception Exception"),
    ],
)
def test_definitions_reach_the_standard_library(
    tmp_path: Path, entry: str, needle: str, delta: int, declaration: str
) -> None:
    [location] = _find(tmp_path, entry, needle, delta)
    assert location.path.is_relative_to(_STDLIB)
    assert declaration in location.text


@pytest.mark.parametrize(
    ("entry", "needle", "delta"),
    [
        ("main.agl", "program def", 0),
        ("main.agl", "[1, 2]", 1),
        ("main.agl", "*\n\ndef", 2),
        ("main.agl", "print(err)\n", 11),
        ("main.agl", "n: int", 3),
        ("lib.agl", "-> T", 3),
        ("more.agl", "g::one", 0),
        ("more.agl", "use lib::geo as g", 16),
        ("more.agl", "Inner(v = 1)", 6),
        ("more.agl", "prio +", 5),
    ],
)
def test_definitions_ignore_unresolved_positions(
    tmp_path: Path, entry: str, needle: str, delta: int
) -> None:
    assert _find(tmp_path, entry, needle, delta) == []


def test_definitions_need_a_file_backed_entry() -> None:
    source = "def f(n: int) -> int = n\n"
    prepared = PipelineDriver.prepare_parsed_entry(PipelineDriver.parse_entry(source))
    assert prepared.resolved is not None
    assert definitions(prepared.resolved, None, source, source.index("= n") + 2) == []


def test_definitions_fall_back_to_scope_when_type_checking_fails(tmp_path: Path) -> None:
    files = {
        **_FILES,
        "main.agl": _MAIN + '  let bad: int = "text"\n  bad\n',
        "lib.agl": _LIB + 'def broken() -> int = "text"\n',
    }
    assert _find(tmp_path, "main.agl", "twice(p.x)", files=files) == [
        _at(tmp_path, "main.agl", "def twice", 4)
    ]
    assert _find(tmp_path, "main.agl", "p.x)", 2, files=files) == []
    # A bare pattern is a binder unless it could also name a constructor.
    assert _find(tmp_path, "lib.agl", "Circle(r) =>", 7, files=files) == [
        _at(tmp_path, "lib.agl", "Circle(r) =>", 7)
    ]
    assert _find(tmp_path, "lib.agl", "None =>", files=files) == []


def test_definitions_select_bare_constructor_patterns(tmp_path: Path) -> None:
    [location] = _find(tmp_path, "lib.agl", "None =>")
    assert location.path.is_relative_to(_STDLIB)
    assert "None" in location.text


@pytest.mark.parametrize(
    ("line", "column", "expected"),
    [(1, 1, 0), (2, 3, 6), (2, 4, 7), (3, 1, None), (2, 5, None), (0, 1, None), (1, 0, None)],
)
def test_source_offset(line: int, column: int, expected: int | None) -> None:
    assert source_offset("abc\ndef", line, column) == expected
