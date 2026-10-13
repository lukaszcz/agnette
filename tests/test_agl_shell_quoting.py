"""``std/exec`` shell quoting functions round-trip through a real POSIX shell."""

from __future__ import annotations

import json
import subprocess
from functools import cache

from tests.agl.ir_harness import evaluate_ir_output

TRICKY = [
    "plain",
    "",
    " ",
    "a b",
    "'",
    "''",
    "a'",
    "'b",
    'it\'s "quoted"',
    "$(echo pwned)",
    "`echo pwned`",
    "$HOME ${HOME}",
    "a; echo pwned",
    "a && b || c | d > e",
    "*.txt ? [ab] ~",
    "line\nbreak\n",
    "back\\slash \\n",
    "100%",
    "tab\there",
    "-n",
    "é😀",
]
SEPARATOR = "\x01"
SEPARATOR_ESCAPE = "\\u0001"


def _literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("%", "\\%")


@cache
def _quoted_words() -> tuple[str, list[str]]:
    """Return ``shell-join`` of all values and ``shell-quote`` of each value, one by one."""
    values = ", ".join(_literal(value) for value in TRICKY)
    source = (
        "program def main() -> unit =\n"
        f"  let values = [{values}]\n"
        f'  print(shell-join(values) ++ "{SEPARATOR_ESCAPE}")\n'
        "  for value in values do\n"
        f'    print(shell-quote(value) ++ "{SEPARATOR_ESCAPE}")\n'
    )
    chunks = evaluate_ir_output(source).split(SEPARATOR + "\n")
    assert chunks.pop() == ""
    return chunks[0], chunks[1:]


def _words_through_sh(words: str) -> list[str]:
    result = subprocess.run(
        ["sh", "-c", f"printf '%s\\0' {words}"], capture_output=True, check=True, timeout=10
    )
    return result.stdout.decode().split("\0")[:-1]


def test_shell_join_round_trips_through_sh() -> None:
    assert _words_through_sh(_quoted_words()[0]) == TRICKY


def test_shell_quote_round_trips_through_sh() -> None:
    assert _words_through_sh(" ".join(_quoted_words()[1])) == TRICKY
