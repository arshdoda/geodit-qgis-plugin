"""Classify ``mobile/feat-batch`` per-op results.

HTTP is 200 for any valid body; each op reports ``{op, id, ok, error_code}``
where ``error_code`` is the failing op's HTTP status. The server's contract
(api-v2 ``docs/map/MAP_MOBILE_API.md`` §20):

* 400 / 404 / 422 are permanent, anything else transient;
* a delete that is ``ok`` or 404 is gone server-side;
* an update 404 means the feature was deleted on the server (or the layer
  vanished) — under last-write-wins the server delete stands, because the
  batch cannot update a soft-deleted row;
* a create 404 means the layer itself is no longer live;
* a 403 means the project's Page access doesn't let this user make that
  change (desktop only). It is not the feature's fault, so the change is
  neither parked nor retried blindly: it stays pending until the user's
  permissions allow it (a denied delete is restored locally instead).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Mapping, Tuple

PERMANENT_CODES = frozenset({400, 404, 422})


@dataclass
class ResultBuckets:
    acked: List[int] = field(default_factory=list)  # create/update ok → base := sent snapshot
    deleted: List[int] = field(default_factory=list)  # delete ok / 404 → drop base row
    discard: List[int] = field(default_factory=list)  # update 404 → local copy to __discarded
    orphaned_layer: bool = False  # create 404 → layer no longer live
    rejected: List[Tuple[int, int]] = field(default_factory=list)  # (gid, code) 400/422
    denied: List[int] = field(default_factory=list)  # 403: Page access forbids this change
    retry: List[int] = field(default_factory=list)  # transient or unanswered


def bucket_results(sent_ops: Iterable[Mapping], results: Iterable[Mapping]) -> ResultBuckets:
    by_id = {int(op["id"]): op for op in sent_ops}
    out = ResultBuckets()
    answered = set()
    for res in results:
        try:
            gid = int(res.get("id"))
        except (TypeError, ValueError):
            continue
        op = by_id.get(gid)
        # Ignore echoes for ids we didn't send, and duplicates.
        if op is None or gid in answered:
            continue
        answered.add(gid)
        kind = op["op"]
        code = res.get("error_code")
        if res.get("ok"):
            (out.deleted if kind == "delete" else out.acked).append(gid)
        elif kind == "delete" and code == 404:
            out.deleted.append(gid)
        elif kind == "update" and code == 404:
            out.discard.append(gid)
        elif kind == "create" and code == 404:
            out.orphaned_layer = True
            out.retry.append(gid)
        elif code == 403:
            out.denied.append(gid)
        elif code in (400, 422):
            out.rejected.append((gid, int(code)))
        else:
            out.retry.append(gid)
    out.retry.extend(gid for gid in by_id if gid not in answered)
    return out
