"""Generic string-parsing helpers shared across AgM layers."""

from __future__ import annotations

import decimal
import re
from threading import TIMEOUT_MAX


def format_timeout(seconds: float | decimal.Decimal) -> str:
    """Render parsed seconds in syntax accepted by :func:`parse_timeout`."""
    return f"{format(decimal.Decimal(str(seconds)), 'f')}s"


def normalize_option_text(value: object) -> str | None:
    """Normalize a raw ``Option[text]`` config value; ``None`` means no value.

    A non-blank string passes through verbatim; a positive number becomes its
    text (``60`` -> ``"60"``); a blank string, a non-positive number, or anything
    else is none. The one reading of a configured ``Option[text]`` setting
    (``timeout``, ``trace-file``): config files, package manifests, and the REPL.
    """
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, (float, decimal.Decimal)) and value > 0:
        return format_timeout(value)
    return None


def parse_timeout(value: str) -> float:
    """Parse a human-readable timeout string into seconds.

    Supports plain numbers (treated as seconds) and durations with
    suffixes: ``s`` (seconds), ``m`` (minutes), ``h`` (hours).

    Examples::

        parse_timeout("30")    -> 30.0
        parse_timeout("30s")   -> 30.0
        parse_timeout("10m")   -> 600.0
        parse_timeout("2h")    -> 7200.0
    """
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|h)?", value.strip())
    if match is None:
        raise ValueError(f"invalid timeout format: {value!r}")
    amount = float(match.group(1))
    unit: str = match.group(2) or ""
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 3600
    return amount


def parse_positive_timeout(value: str) -> float:
    """:func:`parse_timeout` restricted to a positive delay a thread wait can represent."""
    seconds = parse_timeout(value)
    if not 0 < seconds <= TIMEOUT_MAX:
        raise ValueError(f"timeout {value!r} is not positive and representable")
    return seconds
