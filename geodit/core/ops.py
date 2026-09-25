"""Turn local changes into ``mobile/feat-batch`` operations.

Wire contract (``apps/map/schema.py`` MobileFeatBatch*Schema, api-v2):

* ``create`` — ``{op, id, shp_id, geom, attrs}``; ``id`` is our minted snowflake,
  ``geom`` hex WKB (a missing SRID means 4326), ``attrs`` the non-empty values.
* ``update`` — ``{op, id, shp_id, attrs[, geom]}``; ``attrs`` is a PATCH (only the
  keys we changed; ``null`` deletes the key) and ``geom`` is sent only when the
  geometry changed, so an attribute-only desktop edit can't revert a geometry
  edit made concurrently in the field.
* ``delete`` — ``{op, id, shp_id}``.

Snowflakes go out as strings (JSON numbers lose precision above 2**53).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional

from .wkb import wkb_to_hex

MAX_ATTR_VALUE_LEN = 300  # g_feat_*_attr.value CharField(300)
MAX_OPS_PER_BATCH = 50
# The server refuses a request body over 2.5 MB (Django's
# DATA_UPLOAD_MAX_MEMORY_SIZE) with a 500, which reads as transient and would
# be retried forever: batches stay well under it, and one geometry that can't
# fit on its own is held back instead of sent.
MAX_BATCH_BYTES = 1_000_000
MAX_GEOM_HEX_CHARS = 2_000_000  # ~1 MB of WKB (hex doubles it)


@dataclass
class LocalRow:
    fid: int
    gid: int
    geom_wkb: Optional[bytes]
    attrs: Dict[str, Optional[str]] = field(default_factory=dict)


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value)
    return value if value != "" else None


def create_op(shp_id: int, row: LocalRow) -> dict:
    attrs = {k: v for k, v in ((k, _clean(v)) for k, v in row.attrs.items()) if v is not None}
    return {
        "op": "create",
        "id": str(row.gid),
        "shp_id": int(shp_id),
        "geom": wkb_to_hex(row.geom_wkb) if row.geom_wkb is not None else None,
        "attrs": attrs,
    }


def update_op(shp_id: int, row: LocalRow, *, geom_changed: bool, changed_keys: Iterable[str]) -> Optional[dict]:
    attrs = {k: _clean(row.attrs.get(k)) for k in changed_keys}
    if not geom_changed and not attrs:
        return None
    op = {"op": "update", "id": str(row.gid), "shp_id": int(shp_id), "attrs": attrs}
    if geom_changed:
        op["geom"] = wkb_to_hex(row.geom_wkb) if row.geom_wkb is not None else None
    return op


def delete_op(shp_id: int, gid: int) -> dict:
    return {"op": "delete", "id": str(gid), "shp_id": int(shp_id)}


def payload_sha1(op: Mapping) -> str:
    """Stable content hash of an op — a rejected op is retried only once its
    content changes (the user fixed it)."""
    blob = json.dumps(op, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(blob.encode("ascii"), usedforsecurity=False).hexdigest()


def hold_reason(op: Mapping, attr_keys: Iterable[str]) -> Optional[str]:
    """Why an op can't be sent as-is (the server would reject it), else None.

    Geometry validity and the survey-area fence need GEOS and are checked by
    the engine; this covers what is knowable from the payload alone.
    """
    kind = op.get("op")
    if kind in ("create", "update"):
        if kind == "create" or "geom" in op:
            if not op.get("geom"):
                return "Feature has no geometry"
            if len(op["geom"]) > MAX_GEOM_HEX_CHARS:
                return "Geometry is too large to upload (over 1 MB) — simplify it in QGIS"
        allowed = set(attr_keys)
        for key, value in (op.get("attrs") or {}).items():
            if key not in allowed:
                return f"Field '{key}' no longer exists on the server"
            if value is not None and len(value) > MAX_ATTR_VALUE_LEN:
                return f"Field '{key}' is longer than {MAX_ATTR_VALUE_LEN} characters"
    return None


def op_size(op: Mapping) -> int:
    return len(json.dumps(op, separators=(",", ":")))


def chunk_ops(ops: List[dict], size: int = MAX_OPS_PER_BATCH, max_bytes: int = MAX_BATCH_BYTES) -> List[List[dict]]:
    """Batches of at most ``size`` ops and about ``max_bytes`` of JSON (an op
    bigger than that on its own still goes alone)."""
    batches: List[List[dict]] = []
    current: List[dict] = []
    used = 0
    for op in ops:
        n = op_size(op)
        if current and (len(current) >= size or used + n > max_bytes):
            batches.append(current)
            current, used = [], 0
        current.append(op)
        used += n
    if current:
        batches.append(current)
    return batches
