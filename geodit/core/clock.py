"""Server clock.

Watermarks are compared server-side against ``edited_on`` and snowflakes carry a
timestamp, so both use the server's time: the local clock corrected by the
offset observed in each response's ``Date`` header (second precision, which is
plenty next to the 15-minute watermark margin).
"""

from __future__ import annotations

import email.utils
import threading
import time
from typing import Callable, Optional


class ServerClock:
    def __init__(self, offset_ms: int = 0, now: Callable[[], float] = time.time) -> None:
        self._offset_ms = int(offset_ms)
        self._now = now
        self._lock = threading.Lock()

    @property
    def offset_ms(self) -> int:
        with self._lock:
            return self._offset_ms

    def now_ms(self) -> int:
        return int(self._now() * 1000) + self.offset_ms

    def observe_date_header(self, value: Optional[str]) -> None:
        if not value:
            return
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return
        if when is None:
            return
        # A Date header is truncated to the second: +500 ms centres the error.
        offset = int(when.timestamp() * 1000) + 500 - int(self._now() * 1000)
        with self._lock:
            # Ignore sub-2 s jitter so the offset doesn't flap every response.
            if abs(offset - self._offset_ms) >= 2000:
                self._offset_ms = offset
