"""Function ``@param`` parameters as host parameters of `agm exec`."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from agnette.agm.cli_support.args import ExecArgs
from agnette.agm.commands.exec import run
from agnette.packages.layout import MODULE_TREE_DIRNAME
from tests._help_helpers import print_exec_help

_REVIEW = """\
record Counter
  start: int

def Counter::bump(self, @param by: int = 1) -> int = self.start + by

def make(start: int) -> Counter = Counter(start = start)

def refine(@param max-steps: int = 4, label: text = "r") -> text = "%{label}:%{max-steps}"

def nested() -> text = refine()

def gather(@param items: array[int] = [1]) -> int =
  items.push(items.size() + 1)
  items.size()

def spare(@param unused: int = 1) -> int = unused

program def inner(@param limit: int = 1) -> unit = print "limit:%{limit}"

scope Sc
  def deep(@param depth: int = 2) -> int = depth
end Sc
"""

_MAIN = """\
import devel/review

program def main() -> unit =
  print review::nested()
  print review::make(10).bump()
  print review::refine(label = "x")
  print review::refine(7)
  print review::gather()
  print review::gather()
  review::inner()
"""


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    (root / "devel").mkdir(parents=True)
    (root / "devel" / "review.agl").write_text(_REVIEW)
    return root


def _entry(tmp_path: Path, source: str = _MAIN) -> Path:
    entry = tmp_path / "main.agl"
    entry.write_text(source)
    return entry


def _run(entry: Path, library: Path, *tokens: str, program: str | None = None) -> None:
    run(
        ExecArgs(
            file=str(entry),
            program=program,
            argument_tokens=["--no-trace", *tokens],
            module_paths=[str(library)],
        )
    )


def _lines(
    capsys: pytest.CaptureFixture[str],
    entry: Path,
    library: Path,
    *tokens: str,
    program: str | None = None,
) -> list[str]:
    _run(entry, library, *tokens, program=program)
    return capsys.readouterr().out.splitlines()


_DEFAULTS = ["r:4", "11", "x:4", "r:7", "2", "2", "limit:1"]


class TestCommandLine:
    def test_defaults_hold_without_a_host_value(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")

        assert _lines(capsys, _entry(tmp_path), library) == _DEFAULTS

    @pytest.mark.parametrize(
        "flag", ["--review.refine.max-steps", "--devel.review.refine.max-steps", "--max-steps"]
    )
    def test_a_flag_reaches_nested_calls_but_not_explicit_arguments(
        self,
        flag: str,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")

        lines = _lines(capsys, _entry(tmp_path), library, flag, "9")

        assert lines == ["r:9", "11", "x:9", "r:7", "2", "2", "limit:1"]

    @pytest.mark.parametrize("flag", ["--review.Counter.bump.by", "--by"])
    def test_a_flag_reaches_a_method_parameter(
        self,
        flag: str,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")

        lines = _lines(capsys, _entry(tmp_path), library, flag, "5")

        assert lines[1] == "15"

    def test_a_scope_qualifier_alone_is_no_spelling(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")

        with pytest.raises(SystemExit):
            _lines(capsys, _entry(tmp_path), library, "--refine.max-steps", "9")

    def test_a_seeded_mutable_default_is_a_fresh_copy_per_call(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")

        lines = _lines(capsys, _entry(tmp_path), library, "--items", "[5, 6]")

        assert lines[4:6] == ["3", "3"]

    def test_an_unreachable_function_parameter_is_no_flag(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")

        with pytest.raises(SystemExit):
            _lines(capsys, _entry(tmp_path), library, "--unused", "2")
        capsys.readouterr()

        assert print_exec_help(
            tokens=["--help"],
            file=str(_entry(tmp_path)),
            code=None,
            module_paths=[str(library)],
        )
        output = capsys.readouterr().out
        assert "Parameters of devel/review" in output
        assert "--max-steps" in output
        assert "unused" not in output


_DEEP = "import devel/review\n\nprogram def main() -> unit = print review::Sc::deep()\n"


class TestConfiguration:
    def test_the_declaration_route_sets_the_default(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[devel.review.refine]\nmax-steps = 6\n")

        assert _lines(capsys, _entry(tmp_path), library)[0] == "r:6"

    def test_the_shorter_module_suffix_names_the_same_route(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[review.refine]\nmax-steps = 6\n")

        assert _lines(capsys, _entry(tmp_path), library)[0] == "r:6"

    def test_a_method_route_includes_the_receiver(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[review.Counter.bump]\nby = 3\n")

        assert _lines(capsys, _entry(tmp_path), library)[1] == "13"

    @pytest.mark.parametrize(
        "config", ["[review.Sc.deep]\ndepth = 7\n", "[devel.review.Sc.deep]\ndepth = 7\n"]
    )
    def test_a_scope_region_function_route_includes_the_scope(
        self,
        config: str,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home(config)
        entry = _entry(tmp_path, _DEEP)

        assert _lines(capsys, entry, library) == ["7"]
        assert _lines(capsys, entry, library, "--review.Sc.deep.depth", "9") == ["9"]

    def test_the_program_route_beats_the_declaration_route(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home(
            '[devel.review.refine]\nmax-steps = 6\n\n[main.main]\n"review.refine.max-steps" = 8\n'
        )

        assert _lines(capsys, _entry(tmp_path), library)[0] == "r:8"

    def test_the_command_line_beats_the_program_route(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[main.main]\nmax-steps = 8\n")

        lines = _lines(capsys, _entry(tmp_path), library, "--max-steps", "2")

        assert lines[0] == "r:2"

    def test_an_unreached_function_route_draws_no_warning(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[devel.review.spare]\nunused = 3\n")

        _run(_entry(tmp_path), library)

        assert capsys.readouterr().err == ""


_SHADOWED = """\
import devel/review

program def main(max-steps: int = 1) -> unit =
  print max-steps
  print review::nested()
"""


class TestShadowing:
    def test_a_program_argument_takes_the_bare_spelling(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")

        lines = _lines(capsys, _entry(tmp_path, _SHADOWED), library, "--max-steps", "2")

        assert lines == ["2", "r:4"]

    def test_the_qualified_spelling_still_reaches_the_function_parameter(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")

        lines = _lines(
            capsys, _entry(tmp_path, _SHADOWED), library, "--review.refine.max-steps", "3"
        )

        assert lines == ["1", "r:3"]

    def test_help_lists_the_shadowed_parameter_only_qualified(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")

        assert print_exec_help(
            tokens=["--help"],
            file=str(_entry(tmp_path, _SHADOWED)),
            code=None,
            module_paths=[str(library)],
        )

        output = capsys.readouterr().out
        own, _, library_section = output.partition("Parameters of devel/review")
        assert "--max-steps" in own
        assert "--review.refine.max-steps" not in own
        assert "--review.refine.max-steps" in library_section
        assert "--max-steps " not in library_section


_PROGRAMS = """\
program def inner(@param limit: int = 1) -> unit = print "limit:%{limit}"

program def outer() -> unit = inner()
"""


_RECURSIVE = """\
program def again(@param limit: int = 1, depth: int = 0) -> unit =
  print "limit:%{limit}"
  if depth < 1 => again(depth = depth + 1) else => ()

program def outer() -> unit = again()
"""


class TestProgramArguments:
    def test_an_own_param_argument_is_one_parameter_of_its_command(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        entry = _entry(tmp_path, _PROGRAMS)

        assert _lines(capsys, entry, library, "--limit", "4", program="inner") == ["limit:4"]
        assert print_exec_help(tokens=["--help"], file=str(entry), code=None, program="inner")
        assert capsys.readouterr().out.count("--limit") == 1

    def test_the_value_reaches_calls_from_other_programs(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        entry = _entry(tmp_path, _PROGRAMS)

        assert _lines(capsys, entry, library, "--limit", "4", program="outer") == ["limit:4"]

    def test_the_declaration_route_of_a_reached_program_applies(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[devel.review.inner]\nlimit = 5\n")

        assert _lines(capsys, _entry(tmp_path), library)[-1] == "limit:5"

    def test_a_program_route_value_applies_to_its_own_command_only(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[main.inner]\nlimit = 6\n")
        entry = _entry(tmp_path, _PROGRAMS)

        assert _lines(capsys, entry, library, program="inner") == ["limit:6"]
        assert _lines(capsys, entry, library, program="outer") == ["limit:1"]

    def test_the_value_reaches_the_entry_call_and_a_recursive_call(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("[main.again]\nlimit = 6\n")
        entry = _entry(tmp_path, _RECURSIVE)

        assert _lines(capsys, entry, library, program="again") == ["limit:6", "limit:6"]
        assert _lines(capsys, entry, library, "--limit", "3", program="again") == [
            "limit:3",
            "limit:3",
        ]
        assert _lines(capsys, entry, library, program="outer") == ["limit:1", "limit:1"]


class TestManifestConfig:
    """A package manifest ``[config]`` ranks below file program routes, above declaration routes."""

    @staticmethod
    def _entry(tmp_path: Path) -> Path:
        alpha = tmp_path / "alpha"
        (alpha / MODULE_TREE_DIRNAME).mkdir(parents=True)
        (alpha / "package.toml").write_text(
            '[package]\nname = "alpha"\nversion = "1.0.0"\n\n[config.tools.review]\nmax-steps = 5\n'
        )
        entry = alpha / MODULE_TREE_DIRNAME / "main.agl"
        entry.write_text(
            'def refine(@param max-steps: int = 4) -> text = "r:%{max-steps}"\n\n'
            '@command("tools review")\nprogram def main() -> unit = print refine()\n'
        )
        return entry

    @pytest.mark.parametrize(
        ("file_config", "expected"),
        [
            ("", "r:5\n"),
            ("[alpha.main.refine]\nmax-steps = 9\n", "r:5\n"),
            ("[tools.review]\nmax-steps = 7\n", "r:7\n"),
        ],
        ids=["manifest", "over_declaration_route", "under_program_route"],
    )
    def test_the_manifest_tier_sets_a_function_parameter(
        self,
        file_config: str,
        expected: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home(file_config)
        entry = self._entry(tmp_path)

        _run(entry, tmp_path)

        captured = capsys.readouterr()
        assert captured.out == expected
        assert captured.err == ""


_ANONYMOUS_FUNCTIONS = """\
def f(@param n: int = 1) -> int = n

def g(@param n: int = 2) -> int = n

program def main() -> unit =
  print f()
  print g()
"""

_ANONYMOUS_MODULE_AND_FUNCTION = """\
@param let n: int = 5

def f(@param n: int = 1) -> int = n

program def main() -> unit =
  print n
  print f()
"""


class TestAnonymousEntry:
    """A loose entry's parameters are addressable by their scope-qualified spelling."""

    @pytest.mark.parametrize(
        ("tokens", "expected"),
        [
            ((), ["1", "2"]),
            (("--f.n", "7"), ["7", "2"]),
            (("--g.n", "8"), ["1", "8"]),
        ],
    )
    def test_functions_sharing_a_name_are_set_by_their_qualified_spelling(
        self,
        tokens: tuple[str, ...],
        expected: list[str],
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        entry = _entry(tmp_path, _ANONYMOUS_FUNCTIONS)

        assert _lines(capsys, entry, library, *tokens) == expected

    def test_a_program_argument_still_shadows_a_root_module_parameter(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        entry = _entry(
            tmp_path,
            "@param let n: int = 5\n\n"
            "program def main(n: int = 0) -> unit =\n  print n\n  print ::n\n",
        )

        assert _lines(capsys, entry, library, "--n", "3") == ["3", "5"]

    def test_the_program_table_takes_the_qualified_spelling(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home('[main.main]\n"g.n" = 8\n')
        entry = _entry(tmp_path, _ANONYMOUS_FUNCTIONS)

        assert _lines(capsys, entry, library) == ["1", "8"]

    def test_the_shared_bare_name_is_ambiguous_and_names_every_spelling(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        entry = _entry(tmp_path, _ANONYMOUS_FUNCTIONS)

        with pytest.raises(SystemExit):
            _run(entry, library, "--n", "3")

        error = capsys.readouterr().err
        assert "--f.n" in error
        assert "--g.n" in error
        assert "()" not in error

    @pytest.mark.parametrize(
        ("tokens", "expected"),
        [
            (("--n", "9"), ["9", "1"]),
            (("--f.n", "3"), ["5", "3"]),
        ],
    )
    def test_a_module_parameter_keeps_the_bare_name_over_a_function_parameter(
        self,
        tokens: tuple[str, ...],
        expected: list[str],
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")
        entry = _entry(tmp_path, _ANONYMOUS_MODULE_AND_FUNCTION)

        assert _lines(capsys, entry, library, *tokens) == expected

    def test_help_lists_each_parameter_under_its_shortest_won_spelling(
        self,
        tmp_path: Path,
        library: Path,
        capsys: pytest.CaptureFixture[str],
        config_home: Callable[[str], None],
    ) -> None:
        config_home("")

        assert print_exec_help(
            tokens=["--help"],
            file=str(_entry(tmp_path, _ANONYMOUS_MODULE_AND_FUNCTION)),
            code=None,
            module_paths=[str(library)],
        )

        output = capsys.readouterr().out
        assert "--n " in output
        assert "--f.n " in output


def test_every_reused_name_is_settable_through_its_qualified_spelling(
    capsys: pytest.CaptureFixture[str], config_home: Callable[[str], None]
) -> None:
    """The corpus program ``param_names_reused_across_routes`` only checks defaults."""
    config_home("")
    program = Path(__file__).parent / "agl/programs/params/param_names_reused_across_routes.agl"
    flags = {"n": 10, "f.n": 20, "g.n": 30, "Review.n": 40, "Review.h.n": 50}

    run(
        ExecArgs(
            file=str(program),
            argument_tokens=[
                "--no-trace",
                *(token for name, value in flags.items() for token in (f"--{name}", str(value))),
            ],
        )
    )

    assert capsys.readouterr().out.split() == ["10", "20", "30", "40", "50"]
