from __future__ import annotations

import pytest

from agnette.util.posix_shell import HoleKind, QuoteState, hole_quote_states

U = QuoteState.UNQUOTED
S = QuoteState.SINGLE
D = QuoteState.DOUBLE
X = QuoteState.UNKNOWN
WORD = HoleKind.WORD
OPAQUE = HoleKind.OPAQUE


@pytest.mark.parametrize(
    ("fragments", "expected"),
    [
        # No holes, one hole, plain states.
        (["echo hi"], ()),
        (["echo ", ""], (U,)),
        (["echo '", "'"], (S,)),
        (['echo "', '"'], (D,)),
        (["echo 'a' ", ""], (U,)),
        (['echo "a" ', ""], (U,)),
        (["a ", " b ", " c"], (U, U)),
        (["'", "' '", "'"], (S, S)),
        (["'a' ", " 'b", "'"], (U, S)),
        # Quote characters are literals inside the other quote kind.
        (["echo '\"", "\"'"], (S,)),
        (["echo \"'", "'\""], (D,)),
        (["echo \"it's ", '"'], (D,)),
        # Backslash escapes: unquoted and in double quotes, none in single quotes.
        (["echo \\'", ""], (U,)),
        (['echo \\"', ""], (U,)),
        (["echo \\\\'", "'"], (S,)),
        (['echo "a\\"', '"'], (D,)),
        (['echo "a\\\\" ', ""], (U,)),
        (["echo 'a\\' ", ""], (U,)),
        (["echo '\\", "'"], (S,)),
        (['echo "\\$(', '"'], (D,)),
        (['echo "\\`', '"'], (D,)),
        (["echo \\ #", ""], (U,)),
        # A backslash right before a hole escapes the hole: not modelled.
        (["echo \\", " 'a", "'"], (X, X)),
        (['echo "\\', '"'], (X,)),
        (["echo \\", ""], (X,)),
        # Backslash-newline continuation is not modelled.
        (["echo \\\n#'", ""], (X,)),
        (["echo \\\n", ""], (X,)),
        (['echo "\\\n', '"'], (X,)),
        (['echo "$\\\n(', '"'], (X,)),
        (["echo '\\\n", "'"], (S,)),
        # Substitutions and expansions stop the judgement, in any unescaped state.
        (["echo $(", ""], (X,)),
        (["echo '", "' $(x) '", "'"], (S, X)),
        (['echo "$(', '"'], (X,)),
        (["echo `", "`"], (X,)),
        (['echo "`', '"'], (X,)),
        (["echo ${x} '", "'"], (X,)),
        (['echo "${x}', '"'], (X,)),
        (["echo $((1)) '", "'"], (X,)),
        (["echo $'a\\'' '", "'"], (X,)),
        (["echo $", ""], (X,)),
        (["echo '$(", "'"], (S,)),
        (["echo '`${", "'"], (S,)),
        (["echo $x $1 $# $@ '", "'"], (S,)),
        (['echo "$x" ', ""], (U,)),
        # Heredocs and here-strings stop the judgement; `<<` is literal in quotes.
        (["cat <<EOF\n'", "'\nEOF"], (X,)),
        (["cat <<-EOF\n'", ""], (X,)),
        (["cat <<< '", "'"], (X,)),
        (["cat <<'EOF'\n", ""], (X,)),
        (["cat < f '", "'"], (S,)),
        # A hole may be empty, so `<` right before one may join the next `<` into a heredoc.
        (["cat <", "<EOF '", "'"], (X, X)),
        (["cat <", " '", "'"], (X, X)),
        (["echo '<", "'"], (S,)),
        (['echo "<', '"'], (D,)),
        (["echo '<<' '", "'"], (S,)),
        (['echo "<<" "', '"'], (D,)),
        # `#` starts a comment at the start of a word, so the rest is not judged.
        (["# it's ", ""], (X,)),
        (["echo # it's ", ""], (X,)),
        (["echo\t#'", "'"], (X,)),
        (["echo\n#'", ""], (X,)),
        (["echo ;#'", ""], (X,)),
        (["a &&#'", ""], (X,)),
        (["a |#'", ""], (X,)),
        (["(#'", ""], (X,)),
        (["a >#'", ""], (X,)),
        (["a 'x' ", " # '", "'"], (U, X)),
        # `#` inside a word, a quote, or after an escape is literal.
        (["echo a#b '", "'"], (S,)),
        (["echo '#' '", "'"], (S,)),
        (['echo "#" "', '"'], (D,)),
        (["echo 'a'#b '", "'"], (S,)),
        (['echo "a"#b \'', "'"], (S,)),
        (["echo \\ #b '", "'"], (S,)),
        (["echo \\##b '", "'"], (S,)),
        (["echo $#x '", "'"], (S,)),
        (["echo '# it'\"s\" ", ""], (U,)),
        (["echo '", "# '"], (S,)),
        # A hole is an opaque word: `#` right after it may or may not start a word.
        (["echo ", "#'", ""], (U, X)),
        (["echo a", "#'", ""], (U, X)),
        (["echo '", "#'", ""], (S, U)),
        (["echo ", " #'", ""], (U, X)),
        (["echo ", "x #'", ""], (U, X)),
        (["echo ", "x#'", "'"], (U, S)),
        # Unterminated quotes keep their state to the end.
        (["echo 'a ", " b ", ""], (S, S)),
        (['echo "a ', " b ", ""], (D, D)),
        (["echo 'a\n", "\n", ""], (S, S)),
        # Adjacent holes.
        (["echo '", "", "'"], (S, S)),
        (["echo ", "", ""], (U, U)),
        (["", ""], (U,)),
        # Once unknown, always unknown, even after a would-be close.
        (["echo $(x) ", " '", "' ", ""], (X, X, X)),
    ],
)
def test_hole_quote_states(fragments: list[str], expected: tuple[QuoteState, ...]) -> None:
    kinds = [WORD] * (len(fragments) - 1)
    assert hole_quote_states(fragments, kinds) == expected


@pytest.mark.parametrize(
    ("fragments", "kinds", "expected"),
    [
        # An opaque hole is itself judged where it stands; nothing after it is.
        (["echo '", "' '", "'"], [OPAQUE, WORD], (S, X)),
        (["echo ", " '", "'"], [OPAQUE, WORD], (U, X)),
        (["echo ", " ", " '", "'"], [WORD, OPAQUE, WORD], (U, U, X)),
        (["echo '", "'"], [OPAQUE], (S,)),
        (["echo ", "", ""], [OPAQUE, OPAQUE], (U, X)),
        # Word holes keep the state in the quotes they sit in.
        (["echo '", "' \"", '" ', ""], [WORD, WORD, WORD], (S, D, U)),
        (["echo ", " ", ""], [WORD, WORD], (U, U)),
    ],
)
def test_opaque_holes_stop_the_judgement_after_themselves(
    fragments: list[str], kinds: list[HoleKind], expected: tuple[QuoteState, ...]
) -> None:
    assert hole_quote_states(fragments, kinds) == expected
