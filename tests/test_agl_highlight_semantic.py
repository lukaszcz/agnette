"""Scope-only highlighting, hidden context, and lexical fallback contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from agnette.agl.highlight.categories import Category, Span
from agnette.agl.highlight.context import HiddenContext
from agnette.agl.highlight.lexical import lexical_spans
from agnette.agl.highlight.semantic import semantic_spans
from agnette.agl.modules.ids import ModuleId
from agnette.agl.modules.roots import RootSet
from agnette.agl.pipeline import PipelineDriver


@pytest.fixture(scope="session")
def highlight_roots(tmp_path_factory: pytest.TempPathFactory) -> RootSet:
    library = tmp_path_factory.mktemp("highlight-library")
    (library / "shapes.agl").write_text(
        "record Point\n"
        "  x: int\ntype Coordinate = Point\n"
        "def make(x: int) -> Point = Point(x)\nlet origin = Point(0)\n"
        "record Box[T]\n  value: T\n\n"
        "scope nested\n  def nested() = 1\nend nested\n\n"
        "exception Problem extends Exception\n",
        encoding="utf-8",
    )
    (library / "forward.agl").write_text("export shapes::{make as build}\n", encoding="utf-8")
    roots = RootSet(
        roots=frozenset({library}),
        stdlib_roots=frozenset({Path(__file__).resolve().parents[1] / "packages/stdlib"}),
    )
    prepared = PipelineDriver.prepare_program("import shapes\nlet warm = 1\n", roots=roots)
    assert prepared.resolved is not None
    return roots


def _partition(source: str, spans: list[Span]) -> None:
    cursor = 0
    for start, end, _ in spans:
        assert start == cursor
        assert start < end <= len(source)
        cursor = end
    assert cursor == len(source)


def _category(source: str, spans: list[Span], needle: str, *, last: bool = True) -> Category:
    matches = [category for start, end, category in spans if source[start:end] == needle]
    return matches[-1] if last else matches[0]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import shapes\ndef build(p: shapes::Coordinate) -> shapes::Point = shapes::make(1)\n",
            {
                "Coordinate": Category.TYPE,
                "Point": Category.TYPE,
                "make": Category.FUNCTION,
                "build": Category.FUNCTION,
            },
        ),
        (
            "import shapes::{Point as P, make as create, Coordinate as C, origin}\n"
            "def build(p: C) -> P = create(origin.x)\n",
            {
                "C": Category.TYPE,
                "P": Category.TYPE,
                "create": Category.FUNCTION,
                "origin": Category.LOCAL,
            },
        ),
        (
            "import shapes::* hiding make\ndef make() = 0\nlet p = Point(1)\nlet x = make()\n",
            {"Point": Category.CONSTRUCTOR, "make": Category.FUNCTION},
        ),
        (
            "def example(x: Option[int]) =\n  print(Some(1))\n  render(x)\n",
            {
                "Option": Category.TYPE,
                "Some": Category.CONSTRUCTOR,
                "print": Category.BUILTIN_CALL,
                "render": Category.BUILTIN_CALL,
                "x": Category.PARAMETER,
            },
        ),
        (
            "import shapes\ndef example(Point: int, make: int) =\n"
            "  let int = Point\n  let Some = make\n  [int, Some, Point, make]\n",
            {
                "Point": Category.PARAMETER,
                "make": Category.PARAMETER,
                "int": Category.LOCAL,
                "Some": Category.LOCAL,
            },
        ),
        (
            "def parse(x: int) = x\nlet result = parse(1)\n",
            {"parse": Category.FUNCTION, "result": Category.LOCAL, "x": Category.PARAMETER},
        ),
    ],
)
def test_resolved_names(
    source: str, expected: dict[str, Category], highlight_roots: RootSet
) -> None:
    spans = semantic_spans(source, roots=highlight_roots)
    _partition(source, spans)
    for name, category in expected.items():
        assert _category(source, spans, name) == category
    lexical = lexical_spans(source)
    names = {Category.NAME, Category.TYPE, Category.CONSTRUCTOR, Category.BUILTIN_CALL}
    for start, end, category in lexical:
        if category not in names:
            assert all(c == category for a, b, c in spans if a < end and b > start)


@pytest.mark.parametrize("ending", ["", "\n", "\r", "\r\n"])
def test_hidden_context_offsets(ending: str, highlight_roots: RootSet) -> None:
    context = HiddenContext("import shapes::{Point, make}" + ending)
    body = "# only the body\r\nlet p: Point = make(1)\r\n"
    spans = semantic_spans(body, context, roots=highlight_roots)
    _partition(body, spans)
    assert _category(body, spans, "Point") == Category.TYPE
    assert _category(body, spans, "make") == Category.FUNCTION
    assert context.prepend(body).endswith(body)
    assert context.body_spans([(0, len(context.prefix) + 3, Category.TEXT)]) == [
        (0, 3, Category.TEXT)
    ]
    assert context.body_spans([(0, len(context.prefix), Category.TEXT)]) == []


@pytest.mark.parametrize("body", ["", "Some(", 'Point("unterminated', "missing(Point)"])
def test_seeded_fragment_fallback(body: str, highlight_roots: RootSet) -> None:
    context = HiddenContext("record Point\n  x: int\nenum Choice =\n  | Some(value: int)\n")
    types, constructors = context.declared_names
    assert types == frozenset({"Point", "Choice"})
    assert constructors == frozenset({"Point", "Some"})
    spans = semantic_spans(body, context, roots=highlight_roots)
    _partition(body, spans)
    assert spans == lexical_spans(body, types, constructors)


def test_qualified_hidden_declarations_seed_fragment_fallback(
    highlight_roots: RootSet,
) -> None:
    context = HiddenContext("record S::Point\n  x: int\nenum S::Color\n  | Red\n")
    body = "Point("
    types, constructors = context.declared_names

    spans = semantic_spans(body, context, roots=highlight_roots)

    assert types == frozenset({"Point", "Color"})
    assert constructors == frozenset({"Point", "Red"})
    assert spans == lexical_spans(body, types, constructors)
    assert _category(body, spans, "Point") == Category.CONSTRUCTOR


@pytest.mark.parametrize(
    "source", ["let x = missing\n", "import missing\n", "let x = %\n", "def f(\n"]
)
def test_invalid_program_falls_back(source: str, highlight_roots: RootSet) -> None:
    spans = semantic_spans(source, roots=highlight_roots)
    assert spans == lexical_spans(source)
    _partition(source, spans)


@pytest.fixture
def scope_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("highlighting crossed the scope-only boundary")

    monkeypatch.setattr("agnette.agl.typecheck.program.check_program", forbidden)
    monkeypatch.setattr(PipelineDriver, "run_prepared", forbidden)
    monkeypatch.setattr("agnette.agl.eval.ir_interpreter.IrInterpreter.run", forbidden)


def test_default_resolver_and_scope_only(scope_only: None) -> None:
    source = 'let wrong: int = "not an int"\nlet value = Some(1)\n'
    spans = semantic_spans(source)
    assert _category(source, spans, "Some") == Category.CONSTRUCTOR
    assert _category(source, spans, "wrong") == Category.LOCAL


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("let f = fn(x: int) => x\n", {"f": Category.LOCAL, "x": Category.PARAMETER}),
        (
            "def update() =\n  var count = 0\n  count := 1\n  count\n",
            {"count": Category.LOCAL},
        ),
        ("@param\nlet count = 1\nlet x = count\n", {"count": Category.PARAMETER}),
        ("let session = Session::open()\n", {"open": Category.BUILTIN_CALL}),
        ('let message = "${HOME}"\n', {"${HOME}": Category.ENV_HOLE}),
        ('let message = "${HOME}"\nlet tail = 1\n', {"${HOME}": Category.ENV_HOLE}),
        (
            "def f(value: Option[int]) =\n  case value of\n  | Some(x) => x\n  | None => 0\n",
            {"Some": Category.NAME, "None": Category.NAME, "x": Category.LOCAL},
        ),
        (
            "def f(x: int) =\n  case x of\n  | _ as y => y\n",
            {"y": Category.LOCAL},
        ),
        (
            "enum A =\n  | Shared\nenum B =\n  | Shared\n"
            "def f(x: A) =\n  case x of\n  | Shared() => 0\n",
            {"Shared": Category.NAME},
        ),
    ],
)
def test_binding_forms_and_scope_candidates(
    source: str, expected: dict[str, Category], highlight_roots: RootSet
) -> None:
    spans = semantic_spans(source, roots=highlight_roots)
    _partition(source, spans)
    for name, category in expected.items():
        assert _category(source, spans, name) == category


def test_type_owner_qualifier_is_not_colored_as_a_module(highlight_roots: RootSet) -> None:
    source = "import std/option::{Option as Maybe}\nlet x = Maybe::Some(1)\n"
    spans = semantic_spans(source, roots=highlight_roots)

    assert _category(source, spans, "Maybe::") == Category.TYPE
    assert _category(source, spans, "Some") == Category.CONSTRUCTOR
    _partition(source, spans)


def test_attribute_arguments_do_not_steal_declaration_spans(highlight_roots: RootSet) -> None:
    source = 'let x = "description"\ndef f(@doc(x) x: int) = x\n'
    spans = semantic_spans(source, roots=highlight_roots)
    assert [c for a, b, c in spans if source[a:b] == "x"] == [
        Category.LOCAL,
        Category.NAME,
        Category.PARAMETER,
        Category.PARAMETER,
    ]
    _partition(source, spans)


def test_pattern_alias_shadows_its_constructor(highlight_roots: RootSet) -> None:
    source = (
        "enum Shade =\n  | Marked\n"
        "def f(x: Shade) =\n  case x of\n  | Marked() as Marked => Marked\n"
    )
    spans = semantic_spans(source, roots=highlight_roots)
    assert [c for a, b, c in spans if source[a:b] == "Marked"] == [
        Category.CONSTRUCTOR,
        Category.NAME,
        Category.LOCAL,
        Category.LOCAL,
    ]
    _partition(source, spans)


def test_generic_parameter_shadows_hidden_constructor(highlight_roots: RootSet) -> None:
    source = "def f[T](x: T) = x\n"
    context = HiddenContext("enum E =\n  | T\n")
    spans = semantic_spans(source, context, roots=highlight_roots)
    _partition(source, spans)
    assert [c for a, b, c in spans if source[a:b] == "T"] == [Category.TYPE, Category.TYPE]


def test_library_resolution_is_reused(highlight_roots: RootSet) -> None:
    first = PipelineDriver.prepare_program("let first = 1\n", roots=highlight_roots)
    semantic_spans("let middle = Some(2)\n", roots=highlight_roots)
    second = PipelineDriver.prepare_program("let second = 3\n", roots=highlight_roots)
    assert first.resolved is not None and second.resolved is not None
    library = ModuleId.from_path("std/option")
    assert first.resolved.modules[library] is second.resolved.modules[library]


def test_unexpected_frontend_failure_is_total(
    monkeypatch: pytest.MonkeyPatch, highlight_roots: RootSet
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unavailable frontend")

    monkeypatch.setattr(PipelineDriver, "parse_entry", fail)
    source = "let x = 1\n"
    assert semantic_spans(source, roots=highlight_roots) == lexical_spans(source)


@pytest.mark.parametrize(
    ("source", "name", "expected"),
    [
        (
            "import shapes\nlet box = shapes::Box[int]::Box(1)\n",
            "Box",
            [Category.TYPE, Category.CONSTRUCTOR],
        ),
        (
            "def dict[K, V]::K(self) = 1\n",
            "K",
            [Category.TYPE, Category.FUNCTION],
        ),
        (
            "record R\n  None: int\ndef f(x: R) =\n  case x of\n  | R(None) => None\n",
            "None",
            [Category.NAME, Category.NAME, Category.NAME],
        ),
        (
            "record R\n  value: Option[int]\ndef f(x: R) =\n  case x of\n  | R(None) => None\n",
            "None",
            [Category.NAME, Category.NAME],
        ),
        (
            "record R\n  value: int\ndef f(x: R) =\n  case x of\n  | R(value) => value\n",
            "value",
            [Category.NAME, Category.LOCAL, Category.LOCAL],
        ),
        (
            "import shapes\ndef f(x: shapes::Point) = x is shapes::Point\n",
            "Point",
            [Category.TYPE, Category.CONSTRUCTOR],
        ),
        (
            "import shapes\nenum E =\n  | shapes::Point\n",
            "Point",
            [Category.TYPE],
        ),
        (
            "exception Base extends Exception\nexception Derived extends Base\n"
            "def f() = try 1 catch Base as Base => Base\n",
            "Base",
            [Category.TYPE, Category.TYPE, Category.TYPE, Category.LOCAL, Category.LOCAL],
        ),
        (
            "record Point\n  x: int\ndef f() = for Point in [1] do print(Point) done\n",
            "Point",
            [Category.TYPE, Category.LOCAL, Category.LOCAL],
        ),
        (
            "import shapes::{make as Point, Coordinate as make, origin as int}\n",
            "Point",
            [Category.FUNCTION],
        ),
        (
            "import shapes::{make as Point, Coordinate as make, origin as int}\n",
            "make",
            [Category.FUNCTION, Category.TYPE],
        ),
        (
            "import shapes::{make as Point, Coordinate as make, origin as int}\n",
            "int",
            [Category.LOCAL],
        ),
        (
            "import shapes as int\nlet p = int::Point(1)\n",
            "int",
            [Category.MODULE],
        ),
        (
            "import shapes as shapes\n",
            "shapes",
            [Category.MODULE, Category.MODULE],
        ),
        (
            "import shapes::{nested::nested as N}\nimport forward::{build as b}\n",
            "nested",
            [Category.FUNCTION],
        ),
        (
            "import shapes::{nested::nested as N}\nimport forward::{build as b}\n",
            "b",
            [Category.FUNCTION],
        ),
        (
            "import std/value::{render as display}\n",
            "display",
            [Category.BUILTIN_CALL],
        ),
        (
            "def f() = try 1 catch _ => 0\n",
            "f",
            [Category.FUNCTION],
        ),
        (
            "def f() = try 1 catch _ as error => error\n",
            "error",
            [Category.LOCAL, Category.LOCAL],
        ),
        (
            "import shapes\nlet int = shapes::origin.int\n",
            "int",
            [Category.LOCAL, Category.NAME],
        ),
        (
            "import shapes\nlet p = shapes::Point(int = 1)\n",
            "int",
            [Category.NAME],
        ),
        (
            "enum E =\n  | Fresh\ndef f(x: E) = x is Fresh\n",
            "Fresh",
            [Category.CONSTRUCTOR, Category.CONSTRUCTOR],
        ),
        (
            "enum A =\n  | Shared\nenum B =\n  | Shared\ndef f(x: A) = x is Shared\n",
            "Shared",
            [Category.CONSTRUCTOR, Category.CONSTRUCTOR, Category.NAME],
        ),
        (
            "def Point::Point(Point: int) = Point\n",
            "Point",
            [Category.FUNCTION, Category.PARAMETER, Category.PARAMETER],
        ),
        (
            "import shapes::{Problem as lower}\nexception derived extends lower\n"
            "def f() = try 1 catch lower as int => int\n",
            "lower",
            [Category.TYPE, Category.TYPE, Category.TYPE],
        ),
        (
            "import shapes::{nested as Point}\n",
            "Point",
            [Category.NAME],
        ),
        (
            "def f(x: Option[int]) =\n  case x of\n  | Option::Some(value) => value\n",
            "Some",
            [Category.CONSTRUCTOR],
        ),
        (
            "builtin def f(x: int) -> int\n",
            "f",
            [Category.BUILTIN_CALL],
        ),
        (
            "def f() =\n  var obj = Some(1)\n  obj.int := 2\n  obj with int = 1\n",
            "int",
            [Category.NAME, Category.NAME],
        ),
        (
            "def f(x: Option[int]) =\n  case x of\n  | Some(int = value) => value\n",
            "int",
            [Category.TYPE, Category.NAME],
        ),
        (
            "def f[T]{Eq T}(x: T) = x\n",
            "T",
            [Category.TYPE, Category.TYPE, Category.TYPE],
        ),
        *[
            (
                f"def f[{kind}]{{{kind} {kind}}}(x: {kind}) = x\n",
                kind,
                [Category.TYPE, Category.NAME, Category.TYPE, Category.TYPE],
            )
            for kind in ("Eq", "Hashable")
        ],
        *[
            (source, "T", [Category.TYPE, Category.TYPE])
            for source in (
                "record R[T]\n  value: T\n",
                "enum E[T] =\n  | V(value: T)\n",
                "type Alias[T] = Option[T]\n",
            )
        ],
        (
            "export std/value::{render as int}\n",
            "int",
            [Category.BUILTIN_CALL],
        ),
        (
            "export std/value::{render as int}\n",
            "render",
            [Category.BUILTIN_CALL],
        ),
        (
            "export shapes::{nested as Point}\n",
            "Point",
            [Category.NAME],
        ),
        (
            "export forward::{build as int}\n",
            "int",
            [Category.FUNCTION],
        ),
        (
            "export shapes hiding make\n",
            "make",
            [Category.FUNCTION],
        ),
        (
            "use A::{f as int}\n\nscope A\n  def f() = 1\nend A\n",
            "int",
            [Category.FUNCTION],
        ),
        (
            "use A::* hiding f\n\nscope A\n  def f() = 1\nend A\n",
            "f",
            [Category.FUNCTION, Category.FUNCTION],
        ),
        (
            "use A as int\n\nscope A\n  def f() = 1\nend A\n",
            "int",
            [Category.MODULE],
        ),
        (
            "use A::f as int\n\nscope A\n  def f() = 1\nend A\n",
            "int",
            [Category.FUNCTION],
        ),
        (
            "import shapes\nuse shapes::{make as int}\n",
            "int",
            [Category.FUNCTION],
        ),
        (
            "infixl <+> at 6\ndef <+>(x: int, y: int) = x\nlet n = 1 <+> 2\n",
            "<+>",
            [Category.FUNCTION, Category.FUNCTION, Category.FUNCTION],
        ),
        (
            "infixl <+> at 6\nlet <+> = fn(x: int, y: int) => x\n",
            "<+>",
            [Category.LOCAL, Category.LOCAL],
        ),
        (
            "infixl <+> at prio > + 1\ndef <+>(x: int, y: int) = x\n",
            "+",
            [Category.OPERATOR],
        ),
        (
            "import std/value\nuse std/value::{render as int}\n",
            "render",
            [Category.BUILTIN_CALL],
        ),
        (
            "import std/value\nuse std/value::* hiding render\n",
            "render",
            [Category.BUILTIN_CALL],
        ),
        (
            "import std/value\nuse /std/value as int\n",
            "int",
            [Category.MODULE],
        ),
        (
            "use A::{R as int}\n\nscope A\n  record R\nend A\n",
            "int",
            [Category.TYPE],
        ),
        (
            "scope int\n\n  scope int\n    def f() = 1\n  end int\nend int\n",
            "int",
            [Category.NAME, Category.NAME, Category.NAME, Category.NAME],
        ),
        (
            "enum Scope =\n  | V\n\nscope Scope\n  def f() = 1\nend Scope\n",
            "Scope",
            [Category.TYPE, Category.NAME, Category.NAME],
        ),
        (
            "scope int::Some\n  def f() = 1\nend int::Some\n",
            "Some",
            [Category.NAME, Category.NAME],
        ),
        (
            "scope int\nend int\n",
            "int",
            [Category.NAME, Category.NAME],
        ),
        (
            "use S as X\nuse X::f as first\nuse T as X\n"
            "\n"
            "scope S\n  def f() = 1\nend S\n"
            "\n"
            "scope T\n  let f = 1\nend T\n",
            "first",
            [Category.FUNCTION],
        ),
        (
            "use S as X\nuse X::{f as first}\nuse T as X\n"
            "\n"
            "scope S\n  def f() = 1\nend S\n"
            "\n"
            "scope T\n  let f = 1\nend T\n",
            "first",
            [Category.FUNCTION],
        ),
        (
            "use S as X\nuse X::f as first\nuse T as X\n\n"
            "scope S\n  def f() = 1\nend S\n\n"
            "scope T\n  let f = 1\nend T\n",
            "f",
            [Category.FUNCTION, Category.FUNCTION, Category.LOCAL],
        ),
        (
            "use S as X\nuse X::* hiding f\nuse T as X\n\n"
            "scope S\n  def f() = 1\nend S\n\n"
            "scope T\n  let f = 1\nend T\n",
            "f",
            [Category.FUNCTION, Category.FUNCTION, Category.LOCAL],
        ),
        (
            "import std/value\nuse std/value::render as g\n",
            "std/value",
            [Category.MODULE, Category.NAME],
        ),
        (
            "import std/value\nuse std/value::render as g\n",
            "render",
            [Category.BUILTIN_CALL],
        ),
        (
            "import std/value\nuse std/value::render as g\n",
            "g",
            [Category.BUILTIN_CALL],
        ),
        (
            "use int as X\n\nscope int\n  def f() = 1\nend int\n",
            "int",
            [Category.NAME, Category.NAME, Category.NAME],
        ),
        (
            "infixl <+> at 6\ninfixl <*> at prio <+> + 1\n"
            "def <+>(x: int, y: int) = x\ndef <*>(x: int, y: int) = y\n",
            "<+>",
            [Category.FUNCTION, Category.FUNCTION, Category.FUNCTION],
        ),
        (
            "infixl <+> at 6\ndef <+>(x: int, y: int) = x\n\nscope S\n  let <+> = 1\nend S\n",
            "<+>",
            [Category.FUNCTION, Category.FUNCTION, Category.LOCAL],
        ),
        (
            "infixl <+> at 6\nlet <+> = fn(x: int, y: int) => x\n"
            "\n"
            "scope S\n  def <+>(x: int, y: int) = x\nend S\n",
            "<+>",
            [Category.LOCAL, Category.LOCAL, Category.FUNCTION],
        ),
        (
            "infixl <+> at 6\n\nscope S\n  def <+>(x: int, y: int) = x\nend S\n",
            "<+>",
            [Category.NAME, Category.FUNCTION],
        ),
        (
            "import shapes::{Box as B}\nlet b: B[B[int]] = B(B(1))\n",
            "B",
            [
                Category.TYPE,
                Category.TYPE,
                Category.TYPE,
                Category.CONSTRUCTOR,
                Category.CONSTRUCTOR,
            ],
        ),
    ],
)
def test_ast_name_sites(
    source: str,
    name: str,
    expected: list[Category],
    highlight_roots: RootSet,
    scope_only: None,
) -> None:
    prepared = PipelineDriver.prepare_program(source, roots=highlight_roots)
    assert prepared.resolved is not None
    spans = semantic_spans(source, roots=highlight_roots)
    _partition(source, spans)
    assert [c for a, b, c in spans if source[a:b] == name] == expected
