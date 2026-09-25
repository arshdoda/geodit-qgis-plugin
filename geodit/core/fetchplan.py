"""Per-layer ``mobile/feat-list`` fetch plan — a port of the Android client's
``featListFetchPlan`` (geodit-mobile-v3 ``SyncFeaturesUseCase.kt``).

``feat-list`` ANDs two selectors: "intersects one of these assigned survey-area
polygons" and ``edited_on >= last_fetched``. Assigning a polygon writes a join
row and bumps no ``edited_on``, so a newly assigned polygon's existing features
are all older than the layer's watermark. Polygons this layer was already
drained for keep the cheap incremental watermark; polygons never drained go
out with no watermark (a full fetch of just those). The server takes at most
50 polygon ids per request, so each bucket is chunked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set, Tuple

MAX_GEOM_IDS_PER_REQUEST = 50


@dataclass(frozen=True)
class FeatListFetch:
    geom_ids: Tuple[int, ...]
    # ``None`` = time-agnostic full fetch (sent as ``last_fetched=0``).
    last_fetched: Optional[int]


def _chunks(ids: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


def feat_list_fetch_plan(
    assigned_geom_ids: Iterable[int],
    drained_geom_ids: Set[int],
    last_fetched: Optional[int],
    *,
    chunk: int = MAX_GEOM_IDS_PER_REQUEST,
) -> List[FeatListFetch]:
    assigned = list(dict.fromkeys(int(g) for g in assigned_geom_ids))
    if not assigned:
        return []
    if last_fetched is None:
        buckets = [(assigned, None)]
    else:
        known = [g for g in assigned if g in drained_geom_ids]
        fresh = [g for g in assigned if g not in drained_geom_ids]
        buckets = []
        if known:
            buckets.append((known, last_fetched))
        if fresh:
            buckets.append((fresh, None))
    return [FeatListFetch(tuple(part), watermark) for ids, watermark in buckets for part in _chunks(ids, chunk)]
