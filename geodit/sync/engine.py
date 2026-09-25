"""One sync tick for one project — runs in a worker thread.

Order: layer list → survey area → local housekeeping → push → pull → prune.
Housekeeping applies the project's Page access before anything is sent: when
the user may not delete features, rows they deleted locally are restored from
the last-synced copy (it has to happen before the pull, which would otherwise
skip the row as "deleted locally" and move the watermark past any server edit
to it). Pushing first gets
local edits to the server soonest, gives "deleted on the server while edited
here" a single code path (the update's 404), and lets the pull that follows
see our own writes come back. Under last-write-wins the outcome is the same as
pulling first.

Mirrors the Android client (geodit-mobile-v3 ``SyncFeaturesUseCase`` /
``UploadFeatureUseCase`` / ``SurveyAreaGuard``) against the api-v2
``mobile/*`` map endpoints.
"""

from __future__ import annotations

import contextlib
import os
import time
import traceback
from typing import Dict, List, Optional, Set, Tuple

from osgeo import ogr

from ..core.fetchplan import feat_list_fetch_plan
from ..core.ops import LocalRow, chunk_ops, create_op, delete_op, hold_reason, payload_sha1, update_op
from ..core.results import bucket_results
from ..core.schema import LayerInfo, SurveyAreaInfo, fold_attrs, parse_shp_list
from ..core.watermark import next_watermark
from ..core.wkb import GTYPE_TO_MULTI, WkbError, geometry_type, hex_to_wkb2d, to_multi
from ..net.client import ApiClient
from ..net.errors import (
    ApiError,
    NetworkError,
    NotFound,
    PermissionDenied,
    PlanExpired,
    RateLimited,
    ServerError,
    SessionExpired,
    ValidationFailed,
)
from ..store import paths
from ..store.gpkg import StoreError, open_gpkg
from ..store.layer_store import STATE_ACTIVE, STATE_ORPHANED, LayerStore
from ..store.project_store import ProjectStore, SurveyPolygon
from .context import CancelFn, EventFn, LayerEvent, LayerReport, ProgressFn, SyncContext, SyncReport

ATTR_IDS_PER_CALL = 100
# A create or a geometry change syncs when it touches the assigned area: the
# same test `feat-list` uses to deliver a feature (it intersects a polygon).
OUTSIDE_AREA = "Outside your survey area — at least part of it must be inside your survey area to sync"


class Canceled(Exception):
    pass


def rejection_message(code: int) -> str:
    if code == 422:
        return "The server rejected a value (too long or malformed)"
    return "The server rejected this feature (invalid geometry, wrong type or unknown field)"


class SyncEngine:
    def __init__(
        self,
        ctx: SyncContext,
        client: ApiClient,
        *,
        is_canceled: Optional[CancelFn] = None,
        progress: Optional[ProgressFn] = None,
        on_event: Optional[EventFn] = None,
    ) -> None:
        self.ctx = ctx
        self.client = client
        self._is_canceled = is_canceled or (lambda: False)
        self._progress = progress or (lambda pct, text: None)
        self._on_event = on_event or (lambda event: None)
        self.folder = paths.project_dir(ctx.data_root, ctx.base_url, ctx.user_id, ctx.project_id)
        self._stores: Dict[int, LayerStore] = {}
        self._project: Optional[ProjectStore] = None
        self._warnings: List[str] = []

    # ----------------------------------------------------------------- entry
    def run(self) -> SyncReport:
        report = SyncReport(project_id=self.ctx.project_id)
        os.makedirs(self.folder, exist_ok=True)
        lock = paths.ProjectLock(self.folder)
        if not lock.try_lock():
            report.busy = True
            return report
        try:
            self._tick(report)
            report.ok = True
        except Canceled:
            report.canceled = True
        except SessionExpired as exc:
            report.error_kind, report.error = "session_expired", exc.message
        except PlanExpired as exc:
            report.error_kind, report.error, report.plan_expired = "plan_expired", exc.message, True
        except RateLimited as exc:
            report.error_kind, report.error = "rate_limited", exc.message
            report.retry_after_s = exc.retry_after or 60
        except NetworkError as exc:
            report.error_kind, report.error = "network", exc.message
        except ServerError as exc:
            report.error_kind, report.error = "server", exc.message
        except (PermissionDenied, NotFound) as exc:
            report.error_kind, report.error = "forbidden", exc.message
        except ApiError as exc:
            report.error_kind, report.error = "server", exc.message
        except StoreError as exc:
            report.error_kind, report.error = "store", str(exc)
        except Exception as exc:  # noqa: BLE001 - never let a tick kill QGIS
            report.error_kind = "unknown"
            report.error = f"{exc.__class__.__name__}: {exc}"
            report.warnings.append(traceback.format_exc())
        finally:
            for store in self._stores.values():
                store.close()
            self._stores.clear()
            if self._project is not None:
                self._project.close()
                self._project = None
            lock.unlock()
            report.server_offset_ms = self.client.clock.offset_ms
            report.requests = getattr(self.client, "requests", 0)
            report.request_s = getattr(self.client, "request_seconds", 0.0)
            report.warnings.extend(self._warnings)
        return report

    def _check_canceled(self) -> None:
        if self._is_canceled():
            raise Canceled()

    def _emit(self, event: LayerEvent) -> None:
        with contextlib.suppress(Exception):  # a display hook must never break a sync
            self._on_event(event)

    # ------------------------------------------------------------------ tick
    def _tick(self, report: SyncReport) -> None:
        ctx = self.ctx
        started = time.monotonic()
        self._progress(2, "Checking layers")
        self._project = ProjectStore.open_or_create(
            paths.project_gpkg(self.folder),
            {"project_id": str(ctx.project_id), "user_id": str(ctx.user_id), "server": ctx.base_url},
        )
        layers, survey_area = parse_shp_list(self.client.shp_list(ctx.project_id))
        active = self._reconcile_layers(layers, report)
        self._check_canceled()
        started = self._lap(report, "layers", started)

        self._progress(10, "Survey area")
        fence, geometry_changed, assigned, reshaped = self._sync_survey_area(survey_area, report)
        report.area_checked = True
        self._emit(
            LayerEvent(
                ctx.project_id,
                "survey_area",
                area_blocked=report.area_blocked,
                no_survey_area=report.no_survey_area,
                survey_area_changed=report.survey_area_changed,
            )
        )
        self._check_canceled()
        started = self._lap(report, "area", started)

        if ctx.discard_local:
            self._progress(15, "Discarding unsynced changes")
            self._revert_local(active, report)
        elif not ctx.can_delete:
            self._restore_local_deletes(active, report)
        self._check_canceled()

        if ctx.push_enabled and ctx.can_edit:
            for i, (info, store) in enumerate(active):
                self._progress(20 + 30 * i / max(1, len(active)), f"Uploading {info.name}")
                try:
                    self._push_layer(info, store, fence, survey_area is not None, report)
                except PlanExpired as exc:
                    # Reads keep working on an expired plan: stop pushing, still pull.
                    report.plan_expired = True
                    report.error_kind, report.error = "plan_expired", exc.message
                    break
                self._check_canceled()
        started = self._lap(report, "upload", started)

        for i, (info, store) in enumerate(active):
            self._progress(50 + 45 * i / max(1, len(active)), f"Downloading {info.name}")
            layer_report = report.layers[info.id]
            if info.id in ctx.modified:
                layer_report.pull_skipped_unsaved = True
                continue
            if assigned is not None:  # None: no area assigned — keep what we have, pull nothing
                if survey_area is None:
                    raise RuntimeError("survey area missing while an area is assigned")
                self._pull_layer(info, store, survey_area.id, assigned, reshaped, layer_report)
                if geometry_changed and not layer_report.created:
                    layer_report.pruned = store.prune_outside(fence)
                    layer_report.rows_changed = layer_report.rows_changed or bool(layer_report.pruned)
            discarded = store.run_pending_discards()
            if discarded:
                layer_report.discarded_now += discarded
                layer_report.rows_changed = True
            # The layer's last write of this tick: the main thread may show it now.
            self._emit(
                LayerEvent(
                    ctx.project_id,
                    "layer",
                    shp_id=info.id,
                    name=info.name,
                    created=layer_report.created,
                    rows_changed=layer_report.rows_changed,
                )
            )
            self._check_canceled()
        started = self._lap(report, "download", started)

        for info, store in active:
            layer_report = report.layers[info.id]
            layer_report.pending = store.detect_changes().pending
            layer_report.discarded_total = store.discarded_count()
        self._lap(report, "count", started)
        self._progress(100, "Done")

    @staticmethod
    def _lap(report: SyncReport, phase: str, since: float) -> float:
        now = time.monotonic()
        report.timings[phase] = now - since
        return now

    # ---------------------------------------------------------------- layers
    def _reconcile_layers(self, layers: List[LayerInfo], report: SyncReport) -> List[Tuple[LayerInfo, LayerStore]]:
        active: List[Tuple[LayerInfo, LayerStore]] = []
        server_ids = set()
        for info in layers:
            if info.g_type not in GTYPE_TO_MULTI:
                continue  # still ingesting — no feature table yet
            server_ids.add(info.id)
            layer_report = report.layers.setdefault(info.id, LayerReport(shp_id=info.id, name=info.name))
            store, created = LayerStore.open_or_create(
                paths.layer_gpkg(self.folder, info.id),
                info.id,
                g_type=info.g_type,
                name=info.name,
                attr_keys=info.attr_keys,
            )
            self._stores[info.id] = store
            layer_report.created = created
            layer_report.renamed = not created and store.meta("name") != info.name
            if layer_report.renamed or store.state != STATE_ACTIVE:
                store.set_meta({"state": STATE_ACTIVE, "name": info.name})
            change = store.reconcile_schema(info.attr_keys, can_alter=info.id not in self.ctx.modified)
            layer_report.schema_changed = change.altered or bool(change.retired_keys)
            layer_report.schema_deferred = change.deferred
            active.append((info, store))
        # Local layers the server no longer lists: keep the data, stop syncing.
        for name in sorted(os.listdir(self.folder)):
            if not (name.startswith("layer_") and name.endswith(".gpkg")):
                continue
            try:
                shp_id = int(name[len("layer_") : -len(".gpkg")])
            except ValueError:
                continue
            if shp_id in server_ids:
                continue
            path = paths.layer_gpkg(self.folder, shp_id)
            store = LayerStore(open_gpkg(path), path, shp_id)
            try:
                if store.state != STATE_ORPHANED:
                    store.set_meta({"state": STATE_ORPHANED})
                report.layers[shp_id] = LayerReport(
                    shp_id=shp_id, name=store.meta("name") or f"Layer {shp_id}", orphaned=True
                )
            finally:
                store.close()
        return active

    # --------------------------------------------------- local housekeeping
    def _restore_local_deletes(self, active: List[Tuple[LayerInfo, LayerStore]], report: SyncReport) -> None:
        """Deleting features isn't allowed: put back what the user deleted.
        A layer with unsaved QGIS edits is left alone (the engine never writes
        rows under QGIS's edit buffer) — its deletes wait until it is saved."""
        for info, store in active:
            if info.id in self.ctx.modified:
                continue
            restored = store.restore_deleted(store.locally_deleted())
            if restored:
                layer_report = report.layers[info.id]
                layer_report.restored += restored
                layer_report.rows_changed = True

    def _revert_local(self, active: List[Tuple[LayerInfo, LayerStore]], report: SyncReport) -> None:
        for info, store in active:
            layer_report = report.layers[info.id]
            if info.id in self.ctx.modified:
                layer_report.revert_skipped_unsaved = True
                continue
            restored, reverted, discarded = store.revert_local("Discarded: not uploaded")
            layer_report.reverted += restored + reverted + discarded
            layer_report.discarded_now += reverted + discarded
            if restored or reverted or discarded:
                layer_report.rows_changed = True

    # ----------------------------------------------------------- survey area
    def _sync_survey_area(self, sa: Optional[SurveyAreaInfo], report: SyncReport):
        """Full drain every tick: the list has no delete markers and assigning
        a polygon changes no ``edited_on``, so only a full snapshot is correct.

        No polygon for the user (no survey area, or none assigned to them —
        the client can't tell the two apart) blocks the area: the last-known
        polygons stay in ``project.gpkg`` so changes already made are still
        checked against them and uploaded, and nothing is pulled or pruned
        (the watermark and drained polygons stay put, so a reassignment
        resumes incrementally). Returns ``(fence, geometry_changed, assigned,
        reshaped)``; ``assigned`` is ``None`` while blocked."""
        project = self._project
        if project is None:
            raise RuntimeError("no project store is open")
        polys: List[SurveyPolygon] = []
        if sa is None:
            report.no_survey_area = True
        else:
            polys = self._drain_survey_area(sa, report)
            report.no_assignment = not polys
        if not polys:
            report.area_blocked = True
            return project.fence(), False, None, set()
        if sa is None:
            raise RuntimeError("survey-area layer missing while polygons were read")
        geometry_changed, anything_changed, reshaped = project.replace_survey_area(polys, sa.attr_keys)
        report.survey_area_changed = anything_changed
        return project.fence(), geometry_changed, [p.gid for p in polys], reshaped

    def _drain_survey_area(self, sa: SurveyAreaInfo, report: SyncReport) -> List[SurveyPolygon]:
        polys: List[SurveyPolygon] = []
        cursor: Optional[str] = None
        while True:
            self._check_canceled()
            page = self.client.sa_feat_page(self.ctx.project_id, sa.id, cursor)
            ids = [int(r["id"]) for r in page.results]
            attrs: Dict[int, Dict[str, str]] = {}
            if sa.attr_keys:
                rows: List[dict] = []
                for i in range(0, len(ids), ATTR_IDS_PER_CALL):
                    rows += self.client.sa_feat_attrs(self.ctx.project_id, sa.id, ids[i : i + ATTR_IDS_PER_CALL])
                attrs = fold_attrs(rows)
            for r in page.results:
                try:
                    wkb = to_multi(hex_to_wkb2d(r.get("geom") or ""))
                except WkbError:
                    report.warnings.append(f"Survey area polygon {r.get('id')} has unreadable geometry; skipped.")
                    continue
                gid = int(r["id"])
                polys.append(SurveyPolygon(gid, wkb, bool(r.get("completed")), attrs.get(gid, {})))
            if not page.cursor:
                break
            cursor = page.cursor
        return polys

    # ------------------------------------------------------------------ push
    def _geometry_problem(self, wkb: Optional[bytes], g_type: int, fence, check_fence: bool) -> Optional[str]:
        if not wkb:
            return "Feature has no geometry"
        try:
            base_type = geometry_type(wkb)
        except WkbError:
            return "Geometry can't be read"
        if base_type not in (g_type, GTYPE_TO_MULTI[g_type]):
            return "Geometry type doesn't match the layer"
        geom = ogr.CreateGeometryFromWkb(wkb)
        if geom is None or geom.IsEmpty():
            return "Feature has no geometry"
        if not geom.IsValid():
            return "Geometry is invalid (for example self-intersecting) — fix it in QGIS"
        if check_fence and fence is not None and not fence.Intersects(geom):
            return OUTSIDE_AREA
        return None

    def _push_layer(self, info: LayerInfo, store: LayerStore, fence, has_survey_area: bool, report: SyncReport) -> None:
        ctx = self.ctx
        layer_report = report.layers[info.id]
        g_type = int(info.g_type or 0)
        # QGIS syncs only what touches the user's assigned survey area — for
        # everyone: unlike the Android app, the team "offsite" flag is not
        # honoured here. With no survey area (or nothing assigned, and nothing
        # assigned before) new and moved features can't be placed;
        # attribute-only edits still go out.
        if fence is None:
            no_area = (
                "This project has no survey area yet — features can't be added or moved from QGIS"
                if not has_survey_area
                else "No survey area is assigned to you — features can't be added or moved"
            )
        else:
            no_area = None

        changes = store.detect_changes()
        minted = store.minted()
        parked = store.parked()
        new_set = set(changes.new_fids)

        gone = [fid for fid in minted if fid not in new_set]
        store.drop_minted([fid for fid in gone if not minted[fid][1]])
        deletes: List[Tuple[int, int]] = []
        if ctx.can_delete:
            deletes += list(changes.deleted)
            deletes += [(fid, minted[fid][0]) for fid in gone if minted[fid][1]]
        else:
            # Deleting isn't allowed. Server-known rows were restored before
            # the push (or wait for the layer's edits to be saved); our own
            # unconfirmed creates that the user deleted are simply forgotten —
            # if such a create did land, the pull brings the server copy back.
            store.drop_minted([fid for fid in gone if minted[fid][1]])

        waiting = {fid for fid, r in parked.items() if r["kind"] == "discard_pending"}
        new_fids = [f for f in changes.new_fids if f not in waiting]
        changed = [f for f in changes.changed_fids if f not in waiting]

        gid_of: Dict[int, int] = {f: minted[f][0] for f in new_fids if f in minted}
        gid_of.update(store.mint([f for f in new_fids if f not in minted], ctx.worker_id, self.client.clock.now_ms))
        gid_of.update(store.base_gids(changed))
        entries = [(f, gid_of[f], "create") for f in new_fids] + [
            (f, gid_of[f], "update") for f in changed if f in gid_of
        ]
        store.stage(entries)
        staged = store.staged_rows(f for f, _, _ in entries)
        diffs = store.staged_changes(f for f, _, op in entries if op == "update")
        attr_keys = [k for k, _ in store.synced()]

        ops: List[dict] = []
        fid_by_gid: Dict[int, int] = {}
        unstage: List[int] = []
        for fid, gid, kind in entries:
            row = staged.get(fid)
            if row is None:
                continue  # deleted between detection and staging: next tick
            local = LocalRow(fid, gid, row.wkb, row.attrs)
            if kind == "create":
                op = create_op(info.id, local)
                check_geom = True
            else:
                geom_changed, keys = diffs.get(fid, (False, []))
                op = update_op(info.id, local, geom_changed=geom_changed, changed_keys=keys)
                if op is None:
                    unstage.append(fid)
                    continue
                check_geom = geom_changed
            reason = hold_reason(op, attr_keys)
            if reason is None and check_geom:
                reason = no_area or self._geometry_problem(row.wkb, g_type, fence, True)
            if reason is None:
                previous = parked.get(fid)
                if previous and previous["kind"] == "rejected" and previous["payload_sha1"] == payload_sha1(op):
                    layer_report.rejected.append((fid, int(previous["error_code"] or 0), previous["detail"] or ""))
                    unstage.append(fid)
                    continue
            if reason is not None:
                layer_report.held.append((fid, reason))
                unstage.append(fid)
                continue
            ops.append(op)
            fid_by_gid[gid] = fid
        store.unstage(unstage)
        for fid, gid in deletes:
            ops.append(delete_op(info.id, gid))
            fid_by_gid[gid] = fid
        # Rows that were rejected before but have changed since: the new
        # content gets a fresh chance.
        sending = set(fid_by_gid.values())
        store.clear_parked(f for f, r in parked.items() if r["kind"] == "rejected" and f in sending)

        ops_by_gid = {int(op["id"]): op for op in ops}
        for batch in chunk_ops(ops):
            self._check_canceled()
            results = self._send_batch(batch)
            buckets = bucket_results(batch, results)
            layer_report.discarded_now += store.apply_push_results(
                buckets,
                fid_by_gid,
                ops_by_gid,
                can_write_layer=lambda: info.id not in ctx.modified,
                messages={gid: rejection_message(code) for gid, code in buckets.rejected},
            )
            layer_report.pushed += len(buckets.acked) + len(buckets.deleted)
            if buckets.denied:
                # Page access refused these changes: they stay pending (not
                # parked), and the plugin re-reads the user's permissions.
                layer_report.denied += len(buckets.denied)
                report.permission_denied = True
            for gid, code in buckets.rejected:
                layer_report.rejected.append((fid_by_gid[gid], code, rejection_message(code)))
            if buckets.discard:
                layer_report.rows_changed = True
            if buckets.orphaned_layer:
                layer_report.orphaned = True
                break

    def _send_batch(self, batch: List[dict]) -> List[dict]:
        """POST one batch. A malformed op fails the WHOLE request with 422
        (request-level validation), which would block every other op forever —
        so bisect until the bad op is isolated and report it as a per-op 422."""
        try:
            return self.client.feat_batch(self.ctx.project_id, batch)
        except ValidationFailed:
            if len(batch) == 1:
                op = batch[0]
                return [{"op": op["op"], "id": op["id"], "ok": False, "error_code": 422}]
            mid = len(batch) // 2
            return self._send_batch(batch[:mid]) + self._send_batch(batch[mid:])

    # ------------------------------------------------------------------ pull
    def _pull_layer(
        self,
        info: LayerInfo,
        store: LayerStore,
        survey_area_id: int,
        assigned: List[int],
        reshaped: Set[int],
        layer_report: LayerReport,
    ) -> None:
        ctx = self.ctx
        g_type = int(info.g_type or 0)
        watermark = None if ctx.full_resync else store.watermark()
        # A reshaped polygon covers features whose edited_on didn't move:
        # fetch it again in full, like a newly assigned one.
        plan = feat_list_fetch_plan(assigned, store.drained_geoms() - set(reshaped), watermark)
        pull_start = self.client.clock.now_ms()
        seen: Optional[Set[int]] = set() if ctx.full_resync else None
        for fetch in plan:
            cursor: Optional[str] = None
            while True:
                self._check_canceled()
                if info.id in ctx.modified:
                    # The user started editing mid-pull: stop writing rows under
                    # QGIS; the watermark stays put, so the next tick resumes.
                    layer_report.pull_skipped_unsaved = True
                    return
                page = self.client.feat_page(
                    ctx.project_id,
                    survey_area_id=survey_area_id,
                    geom_ids=fetch.geom_ids,
                    shp_id=info.id,
                    g_type=g_type,
                    last_fetched=fetch.last_fetched or 0,
                    cursor=cursor,
                )
                if page.results:
                    self._apply_page(info, store, g_type, page.results, layer_report, seen)
                if not page.cursor:
                    break
                cursor = page.cursor
        if seen is not None:
            removed = store.ghost_cleanup(seen)
            layer_report.removed_remote += removed
            layer_report.rows_changed = layer_report.rows_changed or bool(removed)
        store.finish_pull(next_watermark(pull_start), assigned)

    def _apply_page(
        self, info: LayerInfo, store: LayerStore, g_type: int, rows, layer_report: LayerReport, seen
    ) -> None:
        if seen is None:
            # The watermark overlap re-delivers recent rows on every tick: drop
            # those already applied as sent (a Full re-sync keeps them — every
            # live row must reach ``seen``).
            done = store.already_applied(rows)
            if done:
                rows = [r for r in rows if r.get("deleted") or int(r["id"]) not in done]
            if not rows:
                return
        live = [r for r in rows if not r.get("deleted")]
        ids = [int(r["id"]) for r in live]
        attr_rows: List[dict] = []
        if store.synced():
            for i in range(0, len(ids), ATTR_IDS_PER_CALL):
                attr_rows += self.client.feat_attrs(
                    self.ctx.project_id, shp_id=info.id, g_type=g_type, feat_ids=ids[i : i + ATTR_IDS_PER_CALL]
                )
        attrs = fold_attrs(attr_rows)
        wkb_by_gid: Dict[int, Optional[bytes]] = {}
        usable = []
        for r in rows:
            gid = int(r["id"])
            if r.get("deleted"):
                usable.append(r)
                continue
            try:
                wkb_by_gid[gid] = to_multi(hex_to_wkb2d(r.get("geom") or ""))
            except WkbError:
                self._warnings.append(f"{info.name}: feature {gid} has unreadable geometry; skipped.")
                continue
            usable.append(r)
        outcome = store.apply_pull_page(usable, attrs, wkb_by_gid, seen=seen)
        layer_report.pulled += outcome.inserted + outcome.updated
        layer_report.removed_remote += outcome.deleted
        layer_report.discarded_now += outcome.discarded
        layer_report.rows_changed = layer_report.rows_changed or outcome.rows_changed
