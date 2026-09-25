"""Plain data passed into and out of a sync tick.

A ``SyncTask`` runs in a worker thread, so everything it receives is plain
data: no ``QgsVectorLayer``, ``QgsProject``, ``iface`` or settings writes.
``is_modified`` is the one live hook — a thread-safe view of which layers have
uncommitted QGIS edits, kept current by main-thread signal handlers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple


class ModifiedLayers:
    """Thread-safe set of shp ids whose QGIS layer has uncommitted edits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: Set[int] = set()

    def set(self, shp_id: int, modified: bool) -> None:
        with self._lock:
            if modified:
                self._ids.add(int(shp_id))
            else:
                self._ids.discard(int(shp_id))

    def replace(self, ids) -> None:
        with self._lock:
            self._ids = {int(i) for i in ids}

    def __contains__(self, shp_id: object) -> bool:
        with self._lock:
            return shp_id in self._ids

    def snapshot(self) -> Set[int]:
        with self._lock:
            return set(self._ids)


@dataclass
class SyncContext:
    base_url: str
    data_root: str
    user_id: int
    project_id: int
    worker_id: int
    role: int
    modified: ModifiedLayers
    full_resync: bool = False
    push_enabled: bool = True
    # The project's Page access (Map row) for this user; the Owner has both.
    can_edit: bool = True  # add and edit features
    can_delete: bool = True  # delete features — when off, local deletes are restored
    discard_local: bool = False  # "Discard unsynced changes": revert every layer to the server copy


@dataclass
class LayerReport:
    shp_id: int
    name: str
    created: bool = False
    renamed: bool = False
    orphaned: bool = False
    schema_changed: bool = False
    schema_deferred: bool = False
    rows_changed: bool = False
    pull_skipped_unsaved: bool = False
    pushed: int = 0
    pulled: int = 0
    removed_remote: int = 0
    pruned: int = 0
    discarded_now: int = 0
    pending: int = 0
    discarded_total: int = 0
    restored: int = 0  # local deletes put back: deleting isn't allowed
    denied: int = 0  # changes the server refused (403): Page access
    reverted: int = 0  # local changes dropped by "Discard unsynced changes"
    revert_skipped_unsaved: bool = False
    held: List[Tuple[int, str]] = field(default_factory=list)  # (fid, reason)
    rejected: List[Tuple[int, int, str]] = field(default_factory=list)  # (fid, code, detail)


@dataclass
class SyncReport:
    project_id: int
    ok: bool = False
    busy: bool = False  # another QGIS instance is syncing this project
    canceled: bool = False
    # session_expired | plan_expired | rate_limited | network | server | forbidden | store | unknown
    error_kind: Optional[str] = None
    error: Optional[str] = None
    retry_after_s: Optional[int] = None
    plan_expired: bool = False
    no_survey_area: bool = False
    no_assignment: bool = False
    # No survey-area polygon is assigned to the user (either of the two above):
    # nothing is downloaded or pruned, the last-known area is kept, and only
    # changes already made are uploaded.
    area_blocked: bool = False
    area_checked: bool = False  # the survey-area step ran: the two flags above are this tick's answer
    survey_area_changed: bool = False
    permission_denied: bool = False  # the server refused changes: permissions may have changed
    server_offset_ms: int = 0
    layers: Dict[int, LayerReport] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    # Where the time went: phase → seconds, plus the HTTP requests made.
    timings: Dict[str, float] = field(default_factory=dict)
    requests: int = 0
    request_s: float = 0.0

    @property
    def pending_total(self) -> int:
        return sum(r.pending for r in self.layers.values())

    @property
    def held_total(self) -> int:
        return sum(len(r.held) for r in self.layers.values())

    @property
    def rejected_total(self) -> int:
        return sum(len(r.rejected) for r in self.layers.values())

    @property
    def discarded_total(self) -> int:
        return sum(r.discarded_total for r in self.layers.values())

    @property
    def restored_total(self) -> int:
        return sum(r.restored for r in self.layers.values())

    @property
    def denied_total(self) -> int:
        return sum(r.denied for r in self.layers.values())

    @property
    def reverted_total(self) -> int:
        return sum(r.reverted for r in self.layers.values())


@dataclass(frozen=True)
class LayerEvent:
    """Sent from the worker thread while a tick runs, so the main thread can
    show a finished part without waiting for the whole tick: the survey area
    (``kind="survey_area"``) right after it synced, each layer once its
    download step is over. Plain data only."""

    project_id: int
    kind: str  # "survey_area" | "layer"
    shp_id: int = 0
    name: str = ""
    created: bool = False
    rows_changed: bool = False
    area_blocked: bool = False
    no_survey_area: bool = False
    survey_area_changed: bool = False


ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]
EventFn = Callable[[LayerEvent], None]
