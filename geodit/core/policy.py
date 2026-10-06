"""Retry / pause policy: sync ticks, and the waits a 429 asks for at sign-in."""

from __future__ import annotations

import email.utils
import time
from typing import Optional

BACKOFF_STEPS_S = (30, 60, 120, 300, 600, 1800)
PLAN_EXPIRED_RETRY_S = 3600  # 402: the owner's plan lapsed — nothing to retry soon
MAX_RETRY_AFTER_S = 6 * 3600


def backoff_delay(consecutive_failures: int) -> int:
    if consecutive_failures <= 0:
        return 0
    idx = min(consecutive_failures, len(BACKOFF_STEPS_S)) - 1
    return BACKOFF_STEPS_S[idx]


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[int]:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        seconds = int(value)
    else:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        seconds = int(when.timestamp() - (now if now is not None else time.time()))
    return max(1, min(seconds, MAX_RETRY_AFTER_S))


def format_wait(seconds: int) -> str:
    """A countdown as the web shows it: "42s" under a minute, "4:05" from there
    (a sign-in lockout runs from a second to an hour)."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}:{seconds % 60:02d}"


def login_identity(username: Optional[str], phone: Optional[str], country_code: Optional[str]) -> str:
    """Who a sign-in attempt was for, as the server's per-account lockout keys
    it (trimmed, lower-cased): a 429 holds back this identity only, so another
    account can still sign in. The username wins, as in the login request."""
    if username:
        return username.strip().lower()
    return f"{(country_code or '').strip() or '+91'}{(phone or '').strip()}".lower()
