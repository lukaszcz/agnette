"""A REPL entry's source is lexed and parsed once from prompt to result."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from lark import Tree
from lark.lexer import Token

from agnette.agl.lexer import LexedSource, lex_source
from agnette.agl.lexer.scanner import _Scanner
from agnette.agl.parser import parser as parser_module
from agnette.agl.parser.transform import AstBuilder
from agnette.agl.repl.loop import has_runnable_statements, is_incomplete
from agnette.agl.repl.plain_console import run_plain_console
from agnette.agl.repl.session import ReplSession
from agnette.agl.semantics.values import IntValue
from agnette.agl.syntax.nodes import Program
from tests.test_agl_repl_console import drive


def _session() -> ReplSession:
    session = ReplSession()
    session.open()
    return session


class _Scans:
    """Counts the strict scanner passes made while patched in, wherever they start."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real = _Scanner.scan

        def counting(scanner: _Scanner) -> Iterator[Token]:
            # Editor highlighting scans in recovery mode as its own, display-only pass.
            if not getattr(scanner, "_recover", False):
                self.count += 1
            return real(scanner)

        monkeypatch.setattr(_Scanner, "scan", counting)


class _Parses:
    """Counts the program parser's parses while patched in."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real = parser_module._PARSER.parse

        def counting(text: str) -> Tree:
            self.count += 1
            return real(text)

        monkeypatch.setattr(parser_module._PARSER, "parse", counting)


class _Builds:
    """Records the item count of each program the AST builder converts while patched in."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.items: list[int] = []
        real = AstBuilder.transform

        def recording(builder: AstBuilder, tree: Tree) -> object:
            result = real(builder, tree)
            if isinstance(result, Program):
                self.items.append(len(result.body.items))
            return result

        monkeypatch.setattr(AstBuilder, "transform", recording)


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> _Builds:
    return _Builds(monkeypatch)


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> _Parses:
    return _Parses(monkeypatch)


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> _Scans:
    return _Scans(monkeypatch)


@pytest.mark.parametrize("text", ["int", "int -> int", "dict[text, int]", "Option"])
def test_a_bare_type_entry_is_lexed_once(text: str, scans: _Scans) -> None:
    session = _session()
    scans.count = 0

    result = session.eval_entry(text)

    assert result.kind == "type"
    assert scans.count == 1


@pytest.mark.parametrize("text", ["1 +", "nope", "1 / 0"])
def test_a_rejected_entry_is_lexed_once(text: str, scans: _Scans) -> None:
    session = _session()
    scans.count = 0

    assert not session.eval_entry(text).ok
    assert scans.count == 1


def test_a_file_that_is_no_program_is_lexed_once(tmp_path: Path, scans: _Scans) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text("int -> int\n")
    scans.count = 0

    results = session.load_file(path)

    assert [r.kind for r in results] == ["type"]
    assert scans.count == 1


def test_a_file_that_is_no_program_loads_into_a_session_that_was_never_opened(
    tmp_path: Path,
) -> None:
    path = tmp_path / "t.agl"
    path.write_text("int -> int\n")

    results = ReplSession().load_file(path)

    assert [(r.ok, r.kind) for r in results] == [(True, "type")]


def test_a_syntax_error_file_reports_the_error_at_file_positions(
    tmp_path: Path, scans: _Scans
) -> None:
    session = _session()
    path = tmp_path / "bad.agl"
    path.write_text("let a = 1\nlet b = (\n")
    scans.count = 0

    (result,) = session.load_file(path)

    assert not result.ok
    assert {d.source_label for d in result.diagnostics} == {str(path)}
    assert scans.count == 1


@pytest.mark.parametrize("text", ["1 + 2", "let a = 1", "int", "1 / 0"])
def test_the_plain_console_lexes_an_entry_once_from_prompt_to_result(
    text: str, scans: _Scans
) -> None:
    session = _session()
    scans.count = 0

    run_plain_console(session, stdin=io.StringIO(text + "\n"), stdout=io.StringIO())

    assert scans.count == 1


@pytest.mark.parametrize("text", ["1 + 2", "let a = 1", "int"])
def test_the_rich_console_lexes_an_entry_once_from_prompt_to_result(
    text: str, scans: _Scans
) -> None:
    def scanned(keystrokes: str) -> int:
        scans.count = 0
        drive(keystrokes, session=ReplSession(default_stdlib=False))
        return scans.count

    # Opening the session lexes its own bootstrap source.
    assert scanned(text + "\r\x04") - scanned("\x04") == 1


@pytest.mark.parametrize("text", ["int;", "Option[int];", "int -> int;"])
def test_a_bare_type_entry_may_end_with_a_semicolon(text: str) -> None:
    session = _session()

    result = session.eval_entry(text)

    assert result.kind == "type"
    assert result.ok


def test_an_unknown_name_followed_by_a_semicolon_is_rejected_not_crashed() -> None:
    session = _session()

    result = session.eval_entry("nope;")

    assert not result.ok


@pytest.mark.parametrize("text", ["let x = 42", "record R", "ask $"])
def test_classifying_an_entry_from_its_lex_scans_nothing_more(text: str, scans: _Scans) -> None:
    lexed = lex_source(text)
    scans.count = 0

    assert is_incomplete(lexed) == is_incomplete(text)
    assert has_runnable_statements(lexed)
    assert scans.count == 1  # only the str spelling above lexes


def test_lexing_the_same_text_again_is_a_new_pass(scans: _Scans) -> None:
    scans.count = 0

    lex_source("1 + 2")
    lex_source("1 + 2")

    assert scans.count == 2


@pytest.mark.parametrize("text", ["1 + 2", "let a = 1", "let = 5", "int", "1 / 0"])
def test_the_plain_console_parses_an_entry_once_from_prompt_to_result(
    text: str, parses: _Parses
) -> None:
    session = _session()
    parses.count = 0

    run_plain_console(session, stdin=io.StringIO(text + "\n"), stdout=io.StringIO())

    assert parses.count == 1


@pytest.mark.parametrize("text", ["1 + 2", "let a = 1", "let = 5", "int"])
def test_the_rich_console_parses_an_entry_once_from_prompt_to_result(
    text: str, parses: _Parses
) -> None:
    def parsed(keystrokes: str) -> int:
        parses.count = 0
        drive(keystrokes, session=ReplSession(default_stdlib=False))
        return parses.count

    # Opening the session parses its own bootstrap source.
    assert parsed(text + "\r\x04") - parsed("\x04") == 1


@pytest.mark.parametrize("text", ["def f() -> int =\n\t1\n", "let = 5", "let a = 1\n\tbad"])
def test_an_entry_classified_first_reports_what_an_unclassified_one_does(text: str) -> None:
    def report(lexed: LexedSource | None) -> tuple[object, ...]:
        result = _session().eval_entry(text, lexed=lexed)
        return (
            result.ok,
            [d.message for d in result.diagnostics],
            [w.message for w in result.warnings],
        )

    classified = lex_source(text)
    is_incomplete(classified)

    assert report(classified) == report(None)


def test_a_loaded_file_is_lexed_and_parsed_once(
    tmp_path: Path, scans: _Scans, parses: _Parses
) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text("let a = 1\nlet b = 2\ndef f() -> int = a + b\nf()\n")
    scans.count = parses.count = 0

    results = session.load_file(path)

    assert [r.ok for r in results] == [True] * 4
    assert (scans.count, parses.count) == (1, 1)


@pytest.mark.parametrize(
    "source",
    [
        "let a = 1\ndef h() -> int =\n\t3\nlet b = 2\nprint(a + b)\n",
        "let a = 1\nlet c = app/config ::x\nlet b = 2\nprint(a + b)\n",
    ],
    ids=("tab", "spaced-qualifier"),
)
def test_a_loaded_file_with_advisories_is_lexed_and_parsed_once(
    tmp_path: Path, scans: _Scans, parses: _Parses, source: str
) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text(source)
    scans.count = parses.count = 0

    session.load_file(path)

    assert (scans.count, parses.count) == (1, 1)


def test_a_loaded_entry_reports_file_positions_after_other_entries_on_its_line(
    tmp_path: Path,
) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text("let a = 1; let b: text = 1\nlet c = 2\n")

    results = session.load_file(path)

    assert [r.ok for r in results] == [True, False]
    (diagnostic,) = results[1].diagnostics
    assert (diagnostic.line, diagnostic.column) == (1, 12)
    assert diagnostic.source_label == str(path)


def test_a_loaded_entry_with_a_tab_warns_at_its_file_position(tmp_path: Path) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text("let a = 1\ndef f() -> int =\n\ta\nf()\n")

    results = session.load_file(path)

    assert all(r.ok for r in results)
    (warning,) = results[1].warnings
    assert (warning.line, warning.column) == (3, 1)


def test_loaded_entries_with_tabs_warn_at_their_file_positions(tmp_path: Path) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text("let a = 1\ndef f() -> int =\n\ta\nlet b = 2\ndef g() -> int =\n\tb\ng()\n")

    results = session.load_file(path)

    assert all(r.ok for r in results)
    assert [(w.line, w.column) for r in results for w in r.warnings] == [(3, 1), (6, 1)]


def test_a_loaded_entry_with_a_spaced_qualifier_reports_file_positions(tmp_path: Path) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text("let a = 1\nlet z = 2\nlet b = app/config ::x\nlet c = 3\n")

    results = session.load_file(path)

    assert [r.ok for r in results] == [True, True, False]
    (diagnostic,) = results[2].diagnostics
    assert (diagnostic.line, diagnostic.column) == (3, 9)


def _transcript(*entries: str) -> str:
    return "# agm:repl-transcript:v1\n" + "".join(
        f"# agm:entry:{len(entry)}\n{entry}\n" for entry in entries
    )


_TRANSCRIPTS = {
    "complete": _transcript("let a = 1", "let b = 2", "def f() -> int = a + b", "f()"),
    "halted": _transcript("let a = 1", 'let b: int = "x"', "let c = 1\t", "let d = 2"),
    "halted-in-last": _transcript("let a = 1", 'let b: int = "x"'),
}


_TRANSCRIPT_WORK = {"complete": (4, 4), "halted": (4, 3), "halted-in-last": (2, 2)}


@pytest.mark.parametrize("name", sorted(_TRANSCRIPTS))
def test_each_entry_of_a_loaded_transcript_is_lexed_and_parsed_once(
    tmp_path: Path, scans: _Scans, parses: _Parses, name: str
) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text(_TRANSCRIPTS[name])
    scans.count = parses.count = 0

    session.load_file(path)

    # Each entry is lexed; one that never ran is parsed only if it has an advisory.
    assert (scans.count, parses.count) == _TRANSCRIPT_WORK[name]


def test_a_halted_load_recognizes_attributes_of_the_unrun_items_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agnette.agl.scope import attributes
    from agnette.agl.syntax.nodes import Program
    from agnette.agl.syntax.visitor import SyntaxNode

    walked: list[SyntaxNode] = []
    real = attributes.walk

    def recording(node: SyntaxNode, callback: Callable[[SyntaxNode], None]) -> None:
        walked.append(node)
        real(node, callback)

    session = _session()
    path = tmp_path / "t.agl"
    path.write_text('let a = 1\nlet b: int = "x"\nlet c = 1\nlet d = 2\n')
    monkeypatch.setattr(attributes, "walk", recording)

    session.load_file(path)

    # Each run entry's program is recognized whole; only the two unrun items beyond the failure.
    assert len([n for n in walked if not isinstance(n, Program)]) == 2


_MULTI_ITEM = _transcript("let a = 1; let b = 2", "let c = a + b", "c")
_PLAIN_FILES = {
    "complete": "let a = 1\nlet b = 2\ndef f() -> int = a + b\nf()\n",
    "attributes": '@!allow("tab-indentation")\nlet a = 1\nlet b = 2\n',
    "attributes-alone": '@!doc("m")\n',
}


@pytest.mark.parametrize("name", sorted(_PLAIN_FILES))
def test_a_loaded_file_is_converted_to_ast_once(tmp_path: Path, builds: _Builds, name: str) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text(_PLAIN_FILES[name])
    builds.items.clear()

    results = session.load_file(path)

    assert all(r.ok for r in results)
    # One conversion per entry, each of the one item the entry holds.
    assert builds.items == ([0] if name == "attributes-alone" else [1] * len(results))


def test_a_halted_load_converts_only_the_unrun_items_beyond_the_entries_run(
    tmp_path: Path, builds: _Builds
) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text('let a = 1\nlet b: int = "x"\nlet c = 1\nlet d = 2\n')
    builds.items.clear()

    results = session.load_file(path)

    assert [r.ok for r in results] == [True, False]
    assert builds.items == [1, 1, 2]


@pytest.mark.parametrize(
    ("transcript", "converted"),
    [
        (_MULTI_ITEM, [2, 1, 1]),
        # An unrun entry is converted, once, only if it has an advisory to govern.
        (_transcript("let a = 1; let b: int = 'x'", "let c = 1", "let d = 2"), [2]),
        (_transcript("let a = 1", 'let b: int = "x"', "let c = 1\t", "let d = 2"), [1, 1, 1]),
    ],
    ids=("complete", "halted-in-multi-item", "halted"),
)
def test_a_loaded_transcript_is_converted_to_ast_once(
    tmp_path: Path, builds: _Builds, transcript: str, converted: list[int]
) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text(transcript)
    builds.items.clear()

    session.load_file(path)

    assert builds.items == converted


def test_a_transcript_of_multi_item_entries_lexes_and_parses_each_entry_once(
    tmp_path: Path, scans: _Scans, parses: _Parses
) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text(_MULTI_ITEM)
    scans.count = parses.count = 0

    results = session.load_file(path)

    assert [r.ok for r in results] == [True] * 3
    assert (scans.count, parses.count) == (3, 3)


def test_a_transcript_entry_that_does_not_parse_fails_alone_after_one_lex_and_parse(
    tmp_path: Path, scans: _Scans, parses: _Parses
) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text(_transcript("let a = 1", "let b = = 1", "let c = 2"))
    scans.count = parses.count = 0

    results = session.load_file(path)

    assert [r.ok for r in results] == [True, False]
    assert (scans.count, parses.count) == (2, 2)


_DEEP = "(" * 1500 + "1" + ")" * 1500


@pytest.mark.parametrize(
    ("source", "oks"),
    [
        (f"let x = {_DEEP}\nlet y = 2\n", [False]),
        (f"let y = 2\nlet x = {_DEEP}\n", [True, False]),
        (f'let b: int = "x"\nlet c = 1\t\nlet x = {_DEEP}\n', [False]),
    ],
    ids=("first", "late", "unrun"),
)
def test_a_deeply_nested_item_fails_its_entry_without_crashing_the_load(
    tmp_path: Path, source: str, oks: list[bool]
) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text(source)

    results = session.load_file(path)

    assert [r.ok for r in results] == oks


@pytest.mark.parametrize("name", ["type_of", "info_of"])
def test_a_query_lexes_and_parses_its_text_once(name: str, scans: _Scans, parses: _Parses) -> None:
    session = _session()
    session.eval_entry("let a = 1")
    scans.count = parses.count = 0

    getattr(session, name)("a + 1" if name == "type_of" else "a")

    assert (scans.count, parses.count) == (1, 1)


_TYPE = "array[" * 200 + "int" + "]" * 200


def _denying_session() -> ReplSession:
    from agnette.agl.warning_policy import WarningPolicy

    session = ReplSession(warning_policy=WarningPolicy({"tab-indentation": "deny"}))
    session.open()
    return session


@pytest.mark.parametrize("transcribed", [False, True], ids=("plain", "transcript"))
@pytest.mark.parametrize(
    "later", [("let a = 1", "let b = 2\t"), ("# only a comment",)], ids=("items", "no-items")
)
def test_a_halted_load_whose_first_entry_is_a_type_reports_the_later_advisories(
    tmp_path: Path, transcribed: bool, later: tuple[str, ...]
) -> None:
    path = tmp_path / "t.agl"
    entries = (f"{_TYPE}\t", *later)
    path.write_text(_transcript(*entries) if transcribed else "\n".join(entries) + "\n")

    results = _denying_session().load_file(path)

    assert not results[0].ok
    assert len(results) == 1


def test_the_leading_allow_of_a_file_whose_first_entry_is_a_type_governs_later_entries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "t.agl"
    path.write_text(f'@!allow("tab-indentation")\n{_TYPE}\nlet a = 1\t\n')

    results = _denying_session().load_file(path)

    assert [r.ok for r in results] == [True, True]


_BLOCK_ENTRIES = {
    "def": ("let a = 1", "def f() -> int =\n  a + 1", "def g(x: int) -> int =\n  f() + x", "g(3)"),
    "nested": (
        "def f(x: int) -> int =\n  if x > 1 =>\n    let y = x * 2\n    y\n  else =>\n    0",
        "f(3)",
    ),
    "scope": ("scope S\n  def q() -> int = 9\nend S", "S::q()"),
    "verbatim": ("let s = $\n  raw\n  more", "let t = 2", "s"),
}


@pytest.mark.parametrize("name", sorted(_BLOCK_ENTRIES))
def test_a_transcript_of_block_entries_lexes_and_parses_each_entry_once(
    tmp_path: Path, scans: _Scans, parses: _Parses, name: str
) -> None:
    session = _session()
    path = tmp_path / "t.agl"
    path.write_text(_transcript(*_BLOCK_ENTRIES[name]))
    scans.count = parses.count = 0

    results = session.load_file(path)

    entries = len(_BLOCK_ENTRIES[name])
    assert [r.ok for r in results] == [True] * entries
    assert (scans.count, parses.count) == (entries, entries)


def test_a_transcript_pairs_each_entry_with_its_own_items(tmp_path: Path) -> None:
    saved = _session()
    for entry in ('@!allow("unused-module-doc")', "let a = 1; let b = a + 1", "let c = b * 10"):
        assert saved.eval_entry(entry).ok
    path = tmp_path / "t.agl"
    path.write_text(saved.dump_source())
    loaded = _session()

    results = loaded.load_file(path)

    assert [r.ok for r in results] == [True] * 3
    assert loaded.eval_entry("c").value == IntValue(20)


class _HeaderBuilds:
    """Counts the conversions to AST of a tree holding the file's first-line inner attribute."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real = AstBuilder.transform

        def counting(builder: AstBuilder, tree: Tree) -> object:
            if any(
                t.data == "inner_attribute" and (t.meta.line, t.meta.column) == (1, 1)
                for t in tree.iter_subtrees()
            ):
                self.count += 1
            return real(builder, tree)

        monkeypatch.setattr(AstBuilder, "transform", counting)


_HEADER = '@!allow("tab-indentation")\n'
# A bare type is first read as a program (an index expression), then, rejected, as a type entry:
# two readings of the same text, each a tree of its own that carries the header.
_HEADER_LOADS = {
    "type-ok": (f"{_HEADER}{_TYPE}\nlet a = 1\t\n", 2),
    "type-fails": (f"{_HEADER}{_TYPE}\t\nlet a = 1\n", 2),
    "program-ok": (f"{_HEADER}let z = 1\nlet a = 1\t\n", 1),
    "program-fails": (f"{_HEADER}let z = nope\nlet a = 1\t\n", 1),
    "item-unconvertible": (f"{_HEADER}use a::{{}}\nlet a = 1\t\n", 1),
}


@pytest.mark.parametrize("name", sorted(_HEADER_LOADS))
def test_a_loaded_files_header_is_converted_to_ast_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    source, conversions = _HEADER_LOADS[name]
    path = tmp_path / "t.agl"
    path.write_text(source)
    session = _denying_session()
    builds = _HeaderBuilds(monkeypatch)

    session.load_file(path)

    assert builds.count == conversions


def test_the_leading_allow_governs_the_advisories_after_a_type_entry_and_a_failing_entry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "t.agl"
    path.write_text(f'{_HEADER}{_TYPE}\nlet b: int = "x"\nlet c = 1\t\n')

    results = _denying_session().load_file(path)

    assert [r.ok for r in results] == [True, False]
    assert not any(w.warning_name == "tab-indentation" for r in results for w in r.warnings)
    assert not any(d.warning_name == "tab-indentation" for r in results for d in r.diagnostics)


def test_a_transcript_entry_ending_in_a_block_loads_beside_an_entry_that_does_not_parse(
    tmp_path: Path,
) -> None:
    path = tmp_path / "t.agl"
    path.write_text(_transcript("def g() -> int =\n  0", "let e = )", "g()"))

    results = _session().load_file(path)

    assert [(r.ok, r.name) for r in results] == [(True, "g"), (False, None)]
