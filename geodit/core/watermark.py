"""Pull watermark rules.

``feat-list`` filters ``edited_on >= last_fetched`` and pages newest-first by
``(edited_on, id)``. The server stamps ``edited_on`` with ``timezone.now()``
*before* its transaction commits, so a slow writer can commit a row whose
``edited_on`` is older than a drain that already passed that position. The
next watermark therefore trails the pull's start by a margin longer than any
writer transaction; re-delivered rows are applied idempotently.
"""

from __future__ import annotations

from typing import Optional

MARGIN_MS = 15 * 60 * 1000
# attr_head reaches clients through a cache (up to 1 h stale before api-v2's
# A1b fix) — values for a newly learnt field may already have been delivered
# (and dropped) under the old schema, so learning a new key rewinds the layer.
SCHEMA_REWIND_MS = 75 * 60 * 1000


def next_watermark(pull_start_ms: int) -> int:
    return max(0, int(pull_start_ms) - MARGIN_MS)


def rewind(watermark: Optional[int], by_ms: int = SCHEMA_REWIND_MS) -> Optional[int]:
    if watermark is None:
        return None
    return max(0, int(watermark) - by_ms)
