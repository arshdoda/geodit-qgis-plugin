"""Synced layers in the QGIS project (main thread only).

Layers are ordinary GeoPackage layers marked with custom properties
``geodit/{server,user,project,kind,shp_id}``, so they are recognised again when
a saved QGIS project is reopened. This module adds/reloads/styles them, keeps
the thread-safe "uncommitted edits" set current for the sync engine, and shows
an error while editing when a feature is drawn or moved entirely outside the
assigned survey area (QGIS syncs only what touches it — the team "offsite" flag
is not honoured here).

A sync shows its parts as they finish (``apply_layer_event``: the survey area,
then each new layer); ``apply_report`` at the end reloads what changed. While no
survey area is assigned to the user (``area_blocked``) the layers are read-only:
what was already changed still uploads, nothing new can be edited.

It also applies the project's Page access (Map row) to the synced layers: no
Map write → the layers are read-only; no Map delete → deleting still works in
QGIS (there is no per-layer "no delete" switch), but a warning explains that
the sync engine restores deleted features.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsFieldConstraints,
    QgsFillSymbol,
    QgsGeometry,
    QgsLayerTreeGroup,
    QgsProject,
    QgsRendererCategory,
    QgsVectorLayer,
)

from ..core.projects import ProjectInfo
from ..store import paths
from ..store.ddl import discarded_table, layer_table
from ..sync.context import LayerEvent, ModifiedLayers, SyncReport

PROP = "geodit/"
KIND_LAYER = "layer"
KIND_SA = "survey_area"
KIND_DISCARDED = "discarded"
FENCE_TOLERANCE_DEG = 1e-9
_WARN_EVERY_S = 5.0
_FENCE_ERROR_EVERY_S = 1.5  # one error per drawing gesture, not one per vertex move
_SA_KEY = "survey_area"  # in ``_applied``, next to shp ids


def _is_null(value) -> bool:
    if value is None or value == "":
        return True
    is_null = getattr(value, "isNull", None)  # QVariant NULL on QGIS 3
    return bool(is_null()) if callable(is_null) else False


def _set_policy(layer: QgsVectorLayer, idx: int, setter: str, enum_name: str) -> None:
    method = getattr(layer, setter, None)
    enum = getattr(Qgis, enum_name, None)
    unset = getattr(enum, "UnsetField", None) if enum is not None else None
    if method is not None and unset is not None:
        method(idx, unset)


class LayerManager:
    def __init__(
        self,
        iface,
        modified: ModifiedLayers,
        *,
        on_saved: Callable[[], None],
        message: Callable[[str, int], None],
    ) -> None:
        self.iface = iface
        self.modified = modified
        self.on_saved = on_saved
        self.message = message
        self.server = ""
        self.user_id = 0
        self.project: Optional[ProjectInfo] = None
        self.folder = ""
        self.has_survey_area = True
        self._fence_engine = None
        self._fence_empty = True
        # No survey area assigned to the user: layers read-only, uploads only.
        self.area_blocked = False
        self._connections: List[tuple] = []
        self._reload_after_edit: Dict[str, bool] = {}  # layer id → schema changed
        self._configured: Set[str] = set()  # layer ids whose fields are set up this attach
        self._applied: Set[object] = set()  # shp ids / _SA_KEY already shown this tick
        self._readonly_pending: set = set()  # layer ids to re-check once editing stops
        self._delete_warned: set = set()  # layer ids warned this edit session
        self._watched: set = set()
        self._last_warning = 0.0
        self._last_fence_error = 0.0

    # ----------------------------------------------------------- lookup
    def _props_match(self, layer, kind: Optional[str] = None) -> bool:
        return (
            isinstance(layer, QgsVectorLayer)
            and layer.customProperty(PROP + "server") == self.server
            and str(layer.customProperty(PROP + "user")) == str(self.user_id)
            and self.project is not None
            and str(layer.customProperty(PROP + "project")) == str(self.project.id)
            and (kind is None or layer.customProperty(PROP + "kind") == kind)
        )

    def project_layers(self, kind: Optional[str] = None) -> List[QgsVectorLayer]:
        return [lyr for lyr in QgsProject.instance().mapLayers().values() if self._props_match(lyr, kind)]

    def layer_for(self, shp_id: int, kind: str = KIND_LAYER) -> Optional[QgsVectorLayer]:
        for lyr in self.project_layers(kind):
            if str(lyr.customProperty(PROP + "shp_id")) == str(shp_id):
                return lyr
        return None

    def survey_area_layer(self) -> Optional[QgsVectorLayer]:
        found = self.project_layers(KIND_SA)
        return found[0] if found else None

    @staticmethod
    def geodit_layers_in_project() -> List[QgsVectorLayer]:
        return [
            lyr
            for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and lyr.customProperty(PROP + "kind")
        ]

    # ---------------------------------------------------------- lifecycle
    def attach(self, base_url: str, user_id: int, project: ProjectInfo, folder: str) -> None:
        self.detach()
        self.server = paths.server_key(base_url)
        self.user_id = int(user_id)
        self.project = project
        self.folder = folder
        # Until a sync says otherwise, trust the project list.
        self.area_blocked = project.area_blocker is not None
        self._ensure_layers()
        self.rebuild_fence()

    def detach(self) -> None:
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connections.clear()
        self._reload_after_edit.clear()
        self._configured.clear()
        self._applied.clear()
        self._readonly_pending.clear()
        self._delete_warned.clear()
        self._watched.clear()
        self.modified.replace(())
        self.area_blocked = False
        self.project = None

    def set_project(self, project: ProjectInfo) -> None:
        """The open project's permissions changed (Page access, role). The
        area block is left alone: the syncs decide it, the list may be stale."""
        if self.project is None or project.id != self.project.id:
            return
        self.project = project
        for layer in self.project_layers(KIND_LAYER):
            self._apply_edit_policy(layer)

    def _group(self) -> QgsLayerTreeGroup:
        root = QgsProject.instance().layerTreeRoot()
        key = f"{self.server}/{self.user_id}/{self.project.id}"
        for node in root.findGroups(True):
            if node.customProperty(PROP + "group") == key:
                return node
        group = root.insertGroup(0, f"Geodit – {self.project.name}")
        group.setCustomProperty(PROP + "group", key)
        return group

    def _mark(self, layer: QgsVectorLayer, kind: str, shp_id=None) -> None:
        layer.setCustomProperty(PROP + "server", self.server)
        layer.setCustomProperty(PROP + "user", str(self.user_id))
        layer.setCustomProperty(PROP + "project", str(self.project.id))
        layer.setCustomProperty(PROP + "kind", kind)
        if shp_id is not None:
            layer.setCustomProperty(PROP + "shp_id", str(shp_id))

    def _add(
        self, path: str, table: str, name: str, kind: str, shp_id=None, *, bottom=False
    ) -> Optional[QgsVectorLayer]:
        layer = QgsVectorLayer(f"{path}|layername={table}", name, "ogr")
        if not layer.isValid():
            self.message(f"Couldn't open {name} ({path}).", Qgis.MessageLevel.Warning)
            return None
        self._mark(layer, kind, shp_id)
        QgsProject.instance().addMapLayer(layer, False)
        group = self._group()
        if bottom:
            group.addLayer(layer)
        else:
            group.insertLayer(0 if kind != KIND_SA else len(group.children()), layer)
        return layer

    def _ensure_layers(self) -> None:
        """Make sure every local store of this project is in the QGIS project,
        and watch the synced layers."""
        if not os.path.isdir(self.folder):
            return
        self._ensure_survey_area()
        for name in sorted(os.listdir(self.folder)):
            if not (name.startswith("layer_") and name.endswith(".gpkg")):
                continue
            try:
                shp_id = int(name[len("layer_") : -len(".gpkg")])
            except ValueError:
                continue
            self._ensure_layer(shp_id)

    def _ensure_survey_area(self) -> bool:
        """Add the survey-area layer if its file exists and it isn't in the
        project yet; True when it was added now. The first time it shows, the
        map moves to it — with a basemap loaded QGIS wouldn't do it itself, and
        the data would look missing."""
        project_path = paths.project_gpkg(self.folder)
        added = False
        if os.path.exists(project_path) and self.survey_area_layer() is None:
            sa = self._add(project_path, "survey_area", "Survey area (assigned to you)", KIND_SA, bottom=True)
            if sa is not None:
                self._style_survey_area(sa)
                added = True
        sa = self.survey_area_layer()
        if sa is not None and not sa.readOnly():
            sa.setReadOnly(True)
        if added and sa is not None:
            self._zoom_to(sa)
        return added

    def _ensure_layer(self, shp_id: int, name: Optional[str] = None) -> Tuple[Optional[QgsVectorLayer], bool]:
        """The synced layer ``shp_id`` in the project, set up and watched;
        ``(layer, added_now)``."""
        layer = self.layer_for(shp_id)
        added = False
        if layer is None:
            path = paths.layer_gpkg(self.folder, shp_id)
            if not os.path.exists(path):
                return None, False
            layer = self._add(path, layer_table(shp_id), name or self._layer_name(shp_id), KIND_LAYER, shp_id)
            added = layer is not None
        if layer is not None:
            if layer.id() not in self._configured:
                # Once per attach: every call fires updatedFields per field,
                # which resets open attribute tables and forms.
                self._configure_fields(layer)
                self._configured.add(layer.id())
            self._watch(layer, shp_id)
            self._apply_edit_policy(layer)
        return layer, added

    def _layer_name(self, shp_id: int) -> str:
        # Plain sqlite3, not GDAL: this runs on the main thread, where GDAL
        # would bring its per-thread options along.
        path = paths.layer_gpkg(self.folder, shp_id)
        try:
            con = sqlite3.connect(path, timeout=2)
            try:
                row = con.execute("SELECT value FROM gpkgext_geodit_meta WHERE key = 'name'").fetchone()
            finally:
                con.close()
            return str(row[0]) if row and row[0] else f"Layer {shp_id}"
        except sqlite3.Error:
            return f"Layer {shp_id}"

    def _zoom_to(self, layer: QgsVectorLayer) -> None:
        with contextlib.suppress(Exception):  # a convenience; never break a sync over it
            extent = layer.extent()
            if extent.isNull() or extent.isEmpty():
                return
            canvas = self.iface.mapCanvas()
            transform = QgsCoordinateTransform(
                layer.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance()
            )
            rect = transform.transformBoundingBox(extent)
            rect.scale(1.1)
            canvas.setExtent(rect)
            canvas.refresh()

    # ---------------------------------------------------------- permissions
    def _apply_edit_policy(self, layer: QgsVectorLayer) -> None:
        """Read-only unless the user may edit features (Map write) — set both
        ways every time, because QGIS stores the flag in the project file. QGIS
        refuses to change it while the layer is being edited, so that case is
        re-checked when editing stops."""
        orphaned = layer.customProperty(PROP + "orphaned") == "1"
        readonly = orphaned or self.project is None or not self.project.can_edit or self.area_blocked
        if layer.readOnly() == readonly:
            self._readonly_pending.discard(layer.id())
            return
        if layer.isEditable():
            # A lost area has its own message (``set_area_blocked``): edits
            # saved from this session still upload there.
            if layer.id() not in self._readonly_pending and readonly and not orphaned and not self.area_blocked:
                self._warn(
                    f"You can no longer edit features in {self.project.name if self.project else 'this project'}. "
                    "Your current edits won't be uploaded."
                )
            self._readonly_pending.add(layer.id())
            return
        layer.setReadOnly(readonly)
        self._readonly_pending.discard(layer.id())

    def _warn_delete_not_allowed(self, layer: QgsVectorLayer, fids) -> None:
        """Only saved, server-known features (they carry a Geodit ID) come back:
        undoing an add or deleting a feature that was never uploaded is fine."""
        if self.project is None or self.project.can_delete or layer.id() in self._delete_warned:
            return
        saved = [int(f) for f in fids if int(f) >= 0]
        idx = layer.fields().indexOf("gd_id")
        if not saved or idx < 0:
            return
        request = QgsFeatureRequest().setFilterFids(saved).setSubsetOfAttributes([idx])
        flags = getattr(Qgis, "FeatureRequestFlag", None) or QgsFeatureRequest.Flag  # 3.36+ / older
        request.setFlags(flags.NoGeometry)
        if all(_is_null(f.attribute(idx)) for f in layer.dataProvider().getFeatures(request)):
            return
        self._delete_warned.add(layer.id())
        self.message(
            f"Deleting features isn't allowed in {self.project.name}. Deleted features are restored "
            "on the next sync — use Undo (Ctrl+Z) to bring them back now.",
            Qgis.MessageLevel.Warning,
        )

    # ------------------------------------------------------------- fields
    def _configure_fields(self, layer: QgsVectorLayer) -> None:
        fields = layer.fields()
        form = layer.editFormConfig()
        for name, alias in (("gd_id", "Geodit ID"), ("gd_ans_id", "Response ID")):
            idx = fields.indexOf(name)
            if idx < 0:
                continue
            layer.setFieldAlias(idx, alias)
            form.setReadOnly(idx, True)
            _set_policy(layer, idx, "setFieldSplitPolicy", "FieldDomainSplitPolicy")
            _set_policy(layer, idx, "setFieldDuplicatePolicy", "FieldDuplicatePolicy")
            _set_policy(layer, idx, "setFieldMergePolicy", "FieldDomainMergePolicy")
        layer.setEditFormConfig(form)
        for idx, field in enumerate(fields):
            if field.name() in ("fid", "gd_id", "gd_ans_id") or field.typeName().lower() not in ("string", "text"):
                continue
            quoted = '"' + field.name().replace('"', '""') + '"'
            layer.setConstraintExpression(idx, f"{quoted} IS NULL OR length({quoted}) <= 300", "At most 300 characters")
            layer.setFieldConstraint(
                idx,
                QgsFieldConstraints.Constraint.ConstraintExpression,
                QgsFieldConstraints.ConstraintStrength.ConstraintStrengthHard,
            )

    @staticmethod
    def _style_survey_area(layer: QgsVectorLayer) -> None:
        pending = QgsFillSymbol.createSimple(
            {"color": "255,146,43,35", "outline_color": "#e8590c", "outline_width": "0.8"}
        )
        done = QgsFillSymbol.createSimple({"color": "64,192,87,60", "outline_color": "#2f9e44", "outline_width": "0.8"})
        renderer = QgsCategorizedSymbolRenderer(
            "if(\"completed\", 'Completed', 'Pending')",
            [QgsRendererCategory("Pending", pending, "Pending"), QgsRendererCategory("Completed", done, "Completed")],
        )
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    # ---------------------------------------------------------- watchers
    def _connect(self, signal, slot) -> None:
        signal.connect(slot)
        self._connections.append((signal, slot))

    def _watch(self, layer: QgsVectorLayer, shp_id: int) -> None:
        # Once per layer: _ensure_layers runs after every sync.
        if layer.id() in self._watched:
            return
        self._watched.add(layer.id())

        def update(*_args):
            self.modified.set(shp_id, layer.isEditable() and layer.isModified())

        def started(*_args):
            self._delete_warned.discard(layer.id())
            update()

        def stopped(*_args):
            update()
            schema = self._reload_after_edit.pop(layer.id(), None)
            if schema is not None:
                self._reload(layer, schema=schema)
            if layer.id() in self._readonly_pending:
                self._apply_edit_policy(layer)

        def deleted(fids):
            self._warn_delete_not_allowed(layer, fids)

        def committed(*_args):
            update()
            self.on_saved()

        def added(fid):
            feat = layer.getFeature(fid)
            self._check_fence(feat.geometry())

        def moved(_fid, geom):
            self._check_fence(geom)

        def schema(*_args):
            self._warn(
                "Fields of Geodit layers are managed on the server. Local field changes are not "
                "uploaded, and a deleted synced field is restored on the next sync."
            )

        for signal, slot in (
            (layer.editingStarted, started),
            (layer.layerModified, update),
            (layer.afterRollBack, update),
            (layer.editingStopped, stopped),
            (layer.afterCommitChanges, committed),
            (layer.featureAdded, added),
            (layer.geometryChanged, moved),
            (layer.attributeAdded, schema),
            (layer.attributeDeleted, schema),
            (layer.featuresDeleted, deleted),
        ):
            self._connect(signal, slot)
        update()

    def _warn(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last_warning >= _WARN_EVERY_S:
            self._last_warning = now
            self.message(text, Qgis.MessageLevel.Warning)

    # ------------------------------------------------------------- fence
    def rebuild_fence(self) -> None:
        self._fence_engine = None
        self._fence_empty = True
        sa = self.survey_area_layer()
        if sa is None:
            return
        geoms = [f.geometry() for f in sa.getFeatures() if f.hasGeometry()]
        if not geoms:
            return
        union = QgsGeometry.unaryUnion(geoms).buffer(FENCE_TOLERANCE_DEG, 5)
        engine = QgsGeometry.createGeometryEngine(union.constGet())
        engine.prepareGeometry()
        self._fence_engine = engine
        self._fence_empty = False

    def _fence_error(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last_fence_error >= _FENCE_ERROR_EVERY_S:
            self._last_fence_error = now
            self.message(text, Qgis.MessageLevel.Critical)

    def _check_fence(self, geom: QgsGeometry) -> None:
        if geom is None or geom.isEmpty():
            return
        if not self.has_survey_area:
            self._fence_error("This project has no survey area yet, so features added or moved in QGIS won't sync.")
            return
        if self._fence_empty:
            self._fence_error("No survey area is assigned to you, so features added or moved here won't sync.")
            return
        if not self._fence_engine.intersects(geom.constGet()):
            self._fence_error(
                "This feature is outside your survey area and won't sync. Move it so that at least part of it "
                "is inside the survey area (the outlined polygons)."
            )

    # ------------------------------------------------------------ reports
    def refresh_modified(self) -> None:
        for layer in self.project_layers(KIND_LAYER):
            shp_id = int(layer.customProperty(PROP + "shp_id"))
            self.modified.set(shp_id, layer.isEditable() and layer.isModified())

    def editing_in_transaction_group(self) -> bool:
        """ "Automatic transaction groups" keep a DB transaction open for the whole
        edit session, which would block every engine write to that file."""
        project = QgsProject.instance()
        mode_fn = getattr(project, "transactionMode", None)
        enum = getattr(Qgis, "TransactionMode", None)
        if mode_fn is not None and enum is not None:
            grouped = mode_fn() == enum.AutomaticGroups
        else:  # pragma: no cover - pre-3.26 API
            grouped = bool(getattr(project, "autoTransaction", lambda: False)())
        return grouped and any(lyr.isEditable() for lyr in self.project_layers(KIND_LAYER))

    def _reload(self, layer: QgsVectorLayer, *, schema: bool) -> None:
        if layer.isEditable():
            self._reload_after_edit[layer.id()] = self._reload_after_edit.get(layer.id(), False) or schema
            layer.triggerRepaint()
            return
        layer.reload()
        if schema:
            layer.updateFields()
            self._configure_fields(layer)
        layer.updateExtents()
        layer.triggerRepaint()

    # ------------------------------------------------------------ area block
    def set_area_blocked(self, blocked: bool) -> bool:
        """No survey area is assigned to the user (any more): layers read-only.
        True when the state changed."""
        blocked = bool(blocked)
        if blocked == self.area_blocked:
            return False
        self.area_blocked = blocked
        editing = False
        for layer in self.project_layers(KIND_LAYER):
            editing = editing or layer.isEditable()
            self._apply_edit_policy(layer)
        if blocked and editing and self.project is not None:
            self.message(
                f"No survey area is assigned to you in {self.project.name} any more. Save or roll back your "
                "current edits; the layers then become read-only. Changes you saved inside your former area "
                "still upload.",
                Qgis.MessageLevel.Warning,
            )
        return True

    # ------------------------------------------------------- during a tick
    def begin_tick(self) -> None:
        self._applied.clear()

    def apply_layer_event(self, event: LayerEvent) -> None:
        """Show a finished part of a running sync: the survey area (added,
        refreshed, map moved to it the first time), or a layer that is new in
        this tick. Reloads of existing layers wait for ``apply_report``."""
        if self.project is None or event.project_id != self.project.id or not os.path.isdir(self.folder):
            return
        if event.kind == "survey_area":
            added = self._ensure_survey_area()
            sa = self.survey_area_layer()
            if event.survey_area_changed and not added and sa is not None:
                sa.reload()
                sa.triggerRepaint()
            if not event.area_blocked:
                self.has_survey_area = not event.no_survey_area
            if added or event.survey_area_changed:
                self.rebuild_fence()
            self.set_area_blocked(event.area_blocked)
            self._applied.add(_SA_KEY)
        elif event.kind == "layer" and self.layer_for(event.shp_id) is None:
            _layer, added = self._ensure_layer(event.shp_id, event.name)
            if added:
                self._applied.add(event.shp_id)

    def apply_report(self, report: SyncReport) -> None:
        if self.project is None:
            return
        if report.area_checked:
            if not report.area_blocked:
                self.has_survey_area = not report.no_survey_area
            self.set_area_blocked(report.area_blocked)
        self._ensure_layers()
        for shp_id, lr in report.layers.items():
            layer = self.layer_for(shp_id)
            if layer is None:
                continue
            if lr.orphaned:
                if not layer.name().endswith("(removed on server)"):
                    layer.setName(f"{lr.name} (removed on server)")
                layer.setCustomProperty(PROP + "orphaned", "1")
                self._apply_edit_policy(layer)
                continue
            if layer.customProperty(PROP + "orphaned") == "1":  # listed on the server again
                layer.removeCustomProperty(PROP + "orphaned")
                self._apply_edit_policy(layer)
            if lr.renamed:
                layer.setName(lr.name)
            if (lr.rows_changed or lr.schema_changed or lr.created) and shp_id not in self._applied:
                self._reload(layer, schema=lr.schema_changed)
            if lr.discarded_total and self.layer_for(shp_id, KIND_DISCARDED) is None:
                discarded = self._add(
                    paths.layer_gpkg(self.folder, shp_id),
                    discarded_table(shp_id),
                    f"{lr.name} — discarded edits",
                    KIND_DISCARDED,
                    shp_id,
                    bottom=True,
                )
                if discarded is not None:
                    node = QgsProject.instance().layerTreeRoot().findLayer(discarded.id())
                    if node is not None:
                        node.setExpanded(False)
                    self.message(
                        f"Some edits in {lr.name} were discarded because the features were deleted "
                        "on the server. They are kept in the “discarded edits” layer.",
                        Qgis.MessageLevel.Warning,
                    )
            elif lr.discarded_now:
                discarded = self.layer_for(shp_id, KIND_DISCARDED)
                if discarded is not None:
                    self._reload(discarded, schema=False)
        if report.survey_area_changed and _SA_KEY not in self._applied:
            sa = self.survey_area_layer()
            if sa is not None:
                sa.reload()
                sa.triggerRepaint()
            self.rebuild_fence()
        self._applied.clear()

    def zoom_to(self, shp_id: int, fid: int, kind: str = KIND_LAYER) -> None:
        layer = self.layer_for(shp_id, kind)
        if layer is None:
            return
        layer.selectByIds([int(fid)])
        self.iface.setActiveLayer(layer)
        self.iface.mapCanvas().zoomToSelected(layer)
