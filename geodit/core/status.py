"""What the Geodit panel says about the open project's sync — plain text, no
QGIS, so the wording is unit-tested.

``sync_status`` turns how the latest sync attempt ended, when the project last
synced and how many local changes wait into one status: a state (the dot's
colour), a headline and a detail line. Pending counts are only known from a
completed sync, so a failed attempt never claims that everything was uploaded.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

# How a sync attempt ended: a completed tick, a completed tick whose uploads the
# owner's expired plan refused, the server asking to slow down, or a failure.
OK, PLAN_EXPIRED, RATE_LIMITED, FAILED = "ok", "plan_expired", "rate_limited", "failed"

Status = Tuple[str, str, str]  # (state, headline, detail)
NOT_SYNCED: Status = ("idle", "Not synced yet", "Waiting for the first sync.")


def plural(n: int, one: str, many: Optional[str] = None) -> str:
    """``plural(1, "change")`` → "1 change", ``plural(3, "change")`` → "3 changes"."""
    return f"{n} {one if n == 1 else (many or one + 's')}"


def clock_text(ts: Optional[float]) -> str:
    """The local time of ``ts`` as the panel shows it ("14:05"); "" for none."""
    return time.strftime("%H:%M", time.localtime(ts)) if ts is not None else ""


def sync_status(
    outcome: str,
    *,
    last_synced: Optional[float],
    pending: Optional[int],
    can_edit: bool,
    area_blocked: bool,
) -> Status:
    """``last_synced``: when a sync last completed (None: never). ``pending``:
    local changes waiting to upload, as that sync counted them (None: unknown)."""
    when = f"Last synced {clock_text(last_synced)}" if last_synced is not None else "Not synced yet"
    waiting = f"{plural(pending, 'change')} waiting to upload" if pending else ""
    detail = " · ".join(part for part in (when, waiting) if part)
    if outcome == FAILED:
        return "error", "Sync problem", detail
    if outcome == RATE_LIMITED:
        return "paused", "Waiting for the server", detail
    if area_blocked:
        return "paused", "No survey area assigned", detail
    if outcome == PLAN_EXPIRED:
        return "paused", "Uploads paused", detail
    if pending and not can_edit:
        return "readonly", f"{plural(pending, 'local change')} can't be uploaded", when
    if pending:
        return "pending", waiting, when
    return "ok", "Up to date", when
