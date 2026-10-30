"""Version-owned explicit Unicode classes, with an exact unknown-version fallback."""

import sys
import unicodedata

from agnette.agl.highlight.grammar import character_class
from agnette.agl.operator_names import is_operator_name_char
from agnette.util.ident import is_identifier_start


def scan_unicode_classes() -> tuple[str, str]:
    """Compress the current platform's canonical character predicates into classes."""
    starts: list[str] = []
    operators: list[str] = []
    for point in range(sys.maxunicode + 1):
        character = chr(point)
        if is_identifier_start(character):
            starts.append(character)
        if is_operator_name_char(character):
            operators.append(character)
    return character_class(starts), character_class(operators)


def unicode_character_classes() -> tuple[str, str]:
    """Use literal data only for its UCD owner; never retain a fallback scan."""
    from agnette.agl.highlight.unicode_snapshot import UNICODE_SNAPSHOT

    if unicodedata.unidata_version == UNICODE_SNAPSHOT[0]:
        return UNICODE_SNAPSHOT[1]
    return scan_unicode_classes()
