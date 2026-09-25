"""Retry / pause policy for sync ticks."""

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
