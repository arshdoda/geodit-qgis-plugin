"""DATETIME answers are local clock time.

Port of geodit-ui ``utils/timezone.ts``. A DATETIME value is the date and time
the surveyor saw, not an instant: the backend stores the digits in a UTC slot
and returns them as ``…Z`` (``"2026-09-23T10:00:00Z"`` means 10:00 on the
surveyor's clock) and ignores any offset a client sends. Everything here works
on the digits; the viewer's zone never enters.

Grammar (shared with Android ``WallClock`` and the export worker): ``YYYY``,
``YYYY-MM``, ``YYYY-MM-DD``, optionally ``T`` or a space + ``HH:mm[:ss[.frac]]``,
and — only after a time — an offset ``Z`` / ``±HH`` / ``±HHMM`` / ``±HH:MM``,
which is ignored.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any, Optional

_WALL_CLOCK = re.compile(
    r"^([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2})(?:[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.[0-9]{1,9})?)?(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)?)?)?)?$"
)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_EPOCH = _dt.datetime(1970, 1, 1)


@dataclass(frozen=True)
class WallClock:
    year: int
    month: int  # 1-based (the web's is 0-based, like `Date`)
    day: int
    hour: int
    minute: int
    second: int


def parse_wall_clock(value: Any) -> Optional[WallClock]:
    """The digits of a DATETIME value (missing parts are the start), or None."""
    if not isinstance(value, str):
        return None
    match = _WALL_CLOCK.match(value.strip())
    if not match:
        return None

    def part(i: int, fallback: int) -> int:
        return int(match.group(i)) if match.group(i) is not None else fallback

    wc = WallClock(part(1, 0), part(2, 1), part(3, 1), part(4, 0), part(5, 0), part(6, 0))
    # A date or time that doesn't exist (Feb 30, 24:00, :60) is refused rather
    # than rolled over. `Date.UTC` maps years 0–99 onto 1900–1999, so the web
    # refuses those too.
    if wc.year < 100:
        return None
    try:
        _dt.datetime(wc.year, wc.month, wc.day, wc.hour, wc.minute, wc.second)
    except ValueError:
        return None
    return wc


def encode_wall_clock(wc: WallClock) -> str:
    """``YYYY-MM-DDTHH:mm:ss``, no offset — what a DATETIME answer is sent as."""
    return f"{wc.year:04d}-{wc.month:02d}-{wc.day:02d}T{wc.hour:02d}:{wc.minute:02d}:{wc.second:02d}"


def normalize_wall_clock(value: Any) -> Optional[str]:
    """The value's digits re-encoded with no offset (a date alone → its midnight)."""
    wc = parse_wall_clock(value)
    return encode_wall_clock(wc) if wc else None


def wall_clock_key(value: Any) -> Optional[int]:
    """Comparison key for DATETIME rules: the digits read AS UTC, in epoch ms,
    truncated to the minute. A sort key, not an instant."""
    wc = parse_wall_clock(value)
    if not wc:
        return None
    delta = _dt.datetime(wc.year, wc.month, wc.day, wc.hour, wc.minute) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1000


def format_wall_clock(value: Any) -> Optional[str]:
    """``"23 Sep 2026, 10:00"`` — the digits for display, with no zone."""
    wc = parse_wall_clock(value)
    if not wc:
        return None
    return f"{wc.day:02d} {_MONTHS[wc.month - 1]} {wc.year:04d}, {wc.hour:02d}:{wc.minute:02d}"
