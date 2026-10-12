"""Static-compile guard for every AgL snippet in the reference docs.

Every ```agl fenced block under ``docs/agl/reference/*.md`` is discovered and
run through AgL's full static pipeline (lex → parse → scope → typecheck →
matchcompile → lower), with no agent ever executed. This keeps the
documentation's examples from silently rotting as the language evolves.

Adjacent ``agl-context`` comments supply hidden source lines prepended to the
body. An ``agl-check`` comment anywhere in that metadata block declares the
combined source's expectation:

* ``<!-- agl-check: fragment -->`` — an illustrative fragment or deliberately
  incomplete example (references undefined names, imports absent modules, …).
  It must be rejected even with its hidden context.
* ``<!-- agl-check: error -->`` — a deliberately-rejected program that
  demonstrates a static error. It MUST fail the static pipeline (the exact
  message is not asserted).

A block with no marker is the default and MUST statically compile, so a newly
added doc example is checked automatically unless it is explicitly opted out.

"Statically compiles" means the pipeline reached a lowered program. A program
that lowers but would fail only at run time — for example a required
``program def`` value parameter left unbound here — still counts as
compiling.
"""

from __future__ import annotations

import io
import textwrap
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest

from agnette.agl import PipelineDriver
from agnette.agl.highlight.context import HiddenContext, fence_metadata
from agnette.agl.runtime.agents import AgentRequest

_REFERENCE_DIR = Path(__file__).resolve().parents[1] / "docs" / "agl" / "reference"


@dataclass(frozen=True)
class Snippet:
    """One ```agl fenced block extracted from a reference document."""

    path: Path
    line: int  # 1-based source line of the opening fence
    marker: str | None  # "fragment", "error", or None (must-compile)
    source: str

    @property
    def id(self) -> str:
        return f"{self.path.name}:{self.line}"


def _discover_snippets(directory: Path = _REFERENCE_DIR) -> list[Snippet]:
    """Collect every ```agl block and its compilation expectation."""
    snippets: list[Snippet] = []
    for path in sorted(directory.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            metadata = fence_metadata(lines, index)
            if metadata is None:
                index += 1
                continue
            fence_line = index + 1
            body: list[str] = []
            index += 1
            while index < len(lines):
                content = metadata.container.content(lines[index])
                if content is None:
                    break
                index += 1
                if metadata.closes(content):
                    break
                body.append(content)
            snippets.append(
                Snippet(
                    path=path,
                    line=fence_line,
                    marker=metadata.check,
                    source=HiddenContext(metadata.context).prepend(
                        textwrap.dedent("\n".join(body))
                    ),
                )
            )
    return snippets


_SNIPPETS = _discover_snippets()


@pytest.mark.parametrize("prefix", ["", "    ", "> ", "  > >   "])
@pytest.mark.parametrize("header", ["agl", "{.agl}", ".agl"])
def test_document_context_is_compiled(tmp_path: Path, prefix: str, header: str) -> None:
    """Hidden declarations participate in the same static check as the body."""
    document = tmp_path / "example.md"
    document.write_text(
        "\n".join(
            prefix + line
            for line in [
                "<!-- agl-context: record Point -->",
                "<!-- agl-context:   x: int -->",
                "```" + header,
                "let p = Point(x = 1)",
                "```",
                "",
                "<!-- agl-context: let x = 1 -->",
                "<!-- agl-check: error -->",
                "```" + header,
                'let n: int = "wrong"',
                "```",
            ]
        ),
        encoding="utf-8",
    )
    snippets = _discover_snippets(tmp_path)
    assert len(snippets) == 2
    assert snippets[0].marker is None
    assert snippets[1].marker == "error"
    for snippet in snippets:
        test_reference_doc_snippet(snippet)
    assert not _statically_compiles("let p = Point(x = 1)")[0]


@pytest.mark.parametrize("delimiter", ["```", "~~~"])
@pytest.mark.parametrize("gap", ["", " ", "\t"])
@pytest.mark.parametrize(("prefix", "blank"), [("", ""), ("> ", ">"), ("  > >   ", "  >>")])
def test_document_fence_variants_compile(
    tmp_path: Path, delimiter: str, gap: str, prefix: str, blank: str
) -> None:
    document = tmp_path / "example.md"
    document.write_text(
        "\n".join(
            [
                prefix + "<!-- agl-context: let x = 1 -->",
                prefix + delimiter + gap + "agl",
                prefix + "let y = x",
                blank,
                prefix + "let z = y",
                prefix + delimiter,
            ]
        ),
        encoding="utf-8",
    )
    snippets = _discover_snippets(tmp_path)
    assert len(snippets) == 1
    assert snippets[0].source == "let x = 1\nlet y = x\n\nlet z = y"
    test_reference_doc_snippet(snippets[0])


def test_compiling_fragment_with_context_must_drop_marker(tmp_path: Path) -> None:
    document = tmp_path / "example.md"
    document.write_text(
        "<!-- agl-check: fragment -->\n<!-- agl-context: let x = 1 -->\n```agl\nlet y = x\n```\n",
        encoding="utf-8",
    )
    snippet = _discover_snippets(tmp_path)[0]
    with pytest.raises(AssertionError):
        test_reference_doc_snippet(snippet)


def test_reference_docs_do_not_skip_snippets() -> None:
    """Every reference snippet must have an executable test expectation."""
    skipped = [snippet.id for snippet in _SNIPPETS if snippet.marker == "skip"]
    assert not skipped, f"reference snippets must not opt out of testing: {skipped}"


def _unused_agent(request: AgentRequest) -> str:
    """A default agent that static checking never invokes.

    Registering one mirrors ``agm exec``'s default runner floor: it satisfies
    each program's declared agents and the built-in ``ask`` so a block that
    calls an agent still reaches lowering without any agent running.
    """
    return ""


def _statically_compiles(source: str) -> tuple[bool, list[str]]:
    """Return whether *source* reaches a lowered program, plus any diagnostics.

    Runs the full static pipeline without executing anything.
    ``check_prepared`` never resolves or validates a program's arguments,
    so a required value parameter with no default is silently accepted here
    and only lowering failures are reported.
    """
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        driver = PipelineDriver(
            resolve_agent_spec=None, agent_dispatcher=_unused_agent, get_sandbox_context=None
        )
        prepared = driver.prepare_program(source)
        result = driver.check_prepared(prepared)
    diagnostics = [diag.message for diag in result.diagnostics]
    return result.ok, diagnostics


@pytest.mark.parametrize("snippet", _SNIPPETS, ids=[snippet.id for snippet in _SNIPPETS])
def test_reference_doc_snippet(snippet: Snippet) -> None:
    compiles, diagnostics = _statically_compiles(snippet.source)
    if snippet.marker in {"error", "fragment"}:
        assert not compiles, (
            f"{snippet.id}: marked 'agl-check: {snippet.marker}' but it statically compiled"
        )
    else:
        assert compiles, (
            f"{snippet.id}: expected a static compile but got diagnostics: {diagnostics}"
        )
