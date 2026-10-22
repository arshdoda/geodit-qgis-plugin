"""Pull watermark rules.

``feat-list`` filters ``edited_on >= last_fetched`` and pages newest-first by
``(edited_on, id)``. The server stamps ``edited_on`` with ``timezone.now()``
*before* its transaction commits, so a slow writer can commit a row whose
``edited_on`` is older than a drain that already passed that position. The
next watermark therefore trails the pull's start by a margin longer than any
writer transaction; re-delivered rows are applied idempotently.

api-v2 (W2) also puts its own next ``last_fetched`` on every page: its time
before that request's queries, minus its 6-minute overlap, read off the clock
that stamps ``edited_on``. The earliest of ours and every page's is stored, so
ours keeps its 15 minutes and the server can hold the window open (e.g. while
a long import commits). Ours is always in the minimum, so a page without one
(an older server) changes nothing.
"""

from __future__ import annotations

from typing import Iterable, Optional

MARGIN_MS = 15 * 60 * 1000
# attr_head reaches clients through a cache (up to 1 h stale before api-v2's
# A1b fix) — values for a newly learnt field may already have been delivered
# (and dropped) under the old schema, so learning a new key rewinds the layer.
SCHEMA_REWIND_MS = 75 * 60 * 1000


def next_watermark(pull_start_ms: int, server_marks: Iterable[Optional[int]] = ()) -> int:
    marks = [int(m) for m in server_marks if m is not None]
    return max(0, min([int(pull_start_ms) - MARGIN_MS, *marks]))


def rewind(watermark: Optional[int], by_ms: int = SCHEMA_REWIND_MS) -> Optional[int]:
    if watermark is None:
        return None
    return max(0, int(watermark) - by_ms)
