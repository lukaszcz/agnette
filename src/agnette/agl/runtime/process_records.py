"""Outcome records of the process host: the outcome types and their wire framing.

A child delivers one record on its private pipe: a one-byte tag, an 8-byte big-endian body
length, and the body, so a truncated record is detectable. The parent classifies what it read
with the child's exit status into an :data:`Outcome`.
"""

from __future__ import annotations

import os
import traceback
from dataclasses import dataclass

__all__ = [
    "Delivered",
    "Ended",
    "Failed",
    "Outcome",
    "classify",
    "delivered_record",
    "failed_record",
    "parse_record",
]

_DELIVERED = b"D"
# A delivered failure: the body is the 8-byte length of the summary, the summary, the payload.
_DELIVERED_FAILURE = b"X"
_FAILED = b"F"
_LENGTH_SIZE = 8
_HEADER_SIZE = 1 + _LENGTH_SIZE


@dataclass(frozen=True, slots=True)
class Delivered:
    """The child returned *payload*.

    A companion marks the payload as a delivered failure (say, an exception it carries) with a
    one-line *failure* summary. The host never inspects the payload: the summary is what
    :meth:`ProcessHost.end` reports if nobody consumed the outcome. A child that delivers a
    failure tears down as one that raised.
    """

    payload: bytes
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class Failed:
    """The child's callable raised a Python ``Exception``."""

    message: str

    def describe(self) -> str:
        """How the child ended, as ``failed: <message>``."""
        return f"failed: {self.message}"


@dataclass(frozen=True, slots=True)
class Ended:
    """The child ended without a complete record: by exit status or by signal."""

    exit_code: int | None
    signal: int | None

    def describe(self) -> str:
        """How the child ended, by signal or by exit status."""
        if self.exit_code is None:
            return f"was terminated by signal {self.signal}"
        return f"exited with status {self.exit_code}"


Outcome = Delivered | Failed | Ended


def _frame(tag: bytes, body: bytes) -> bytes:
    return tag + len(body).to_bytes(_LENGTH_SIZE, "big") + body


def delivered_record(delivered: Delivered) -> bytes:
    """The record delivering *delivered*; a summary that is not valid Unicode is escaped."""
    if delivered.failure is None:
        return _frame(_DELIVERED, delivered.payload)
    summary = delivered.failure.encode("utf-8", errors="backslashreplace")
    body = len(summary).to_bytes(_LENGTH_SIZE, "big") + summary + delivered.payload
    return _frame(_DELIVERED_FAILURE, body)


def parse_record(data: bytes | bytearray) -> Delivered | Failed | None:
    """The record *data* holds, or ``None`` if it is not exactly one complete record."""
    with memoryview(data) as view:
        if len(view) < _HEADER_SIZE:
            return None
        tag = bytes(view[:1])
        body = view[_HEADER_SIZE:]
        if len(body) != int.from_bytes(view[1:_HEADER_SIZE], "big"):
            return None
        if tag == _DELIVERED:
            return Delivered(bytes(body))
        if tag == _DELIVERED_FAILURE:
            if len(body) < _LENGTH_SIZE:
                return None
            summary_end = _LENGTH_SIZE + int.from_bytes(body[:_LENGTH_SIZE], "big")
            if summary_end > len(body):
                return None
            summary = str(body[_LENGTH_SIZE:summary_end], "utf-8", errors="replace")
            return Delivered(bytes(body[summary_end:]), summary)
        if tag == _FAILED:
            return Failed(str(body, "utf-8", errors="replace"))
        return None


def classify(data: bytes | bytearray, status: int) -> Outcome:
    """The outcome of a finished child: a complete record wins over its exit status."""
    record = parse_record(data)
    if record is not None:
        return record
    code = os.waitstatus_to_exitcode(status)
    return Ended(code, None) if code >= 0 else Ended(None, -code)


def _describe(exc: Exception) -> str:
    """*exc* as Python reports it (``Type: message``); its type's name if that fails."""
    try:
        return "".join(traceback.format_exception_only(exc)).rstrip()
    except Exception:
        return type(exc).__qualname__


def failed_record(exc: Exception) -> bytes:
    """The record reporting *exc*; text that is not valid Unicode is escaped, not lost."""
    return _frame(_FAILED, _describe(exc).encode("utf-8", errors="backslashreplace"))
