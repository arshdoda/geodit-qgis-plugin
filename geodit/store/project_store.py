"""Per-project store (``project.gpkg``): the caller's assigned survey-area
polygons (read-only in QGIS) plus project-level sync metadata."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from osgeo import ogr

from ..core.colmap import extend_colmap
from . import ddl
from .gpkg import check_ogr, exec_sql, open_gpkg, query, sql_int_list, sql_str, transaction

SURVEY_AREA_TABLE = "survey_area"
# The Android guard buffers the fence by 1e-9° (~0.1 mm) to absorb float noise
# on boundary-touching geometry (geodit-mobile-v3 SurveyAreaGuard.kt).
FENCE_TOLERANCE_DEG = 1e-9


@dataclass
class SurveyPolygon:
    gid: int
    wkb: bytes  # 2D Multi* WKB
    completed: bool
    attrs: Mapping[str, Optional[str]]


class ProjectStore:
    def __init__(self, ds, path: str) -> None:
        self.ds = ds
        self.path = path

    @classmethod
    def open_or_create(cls, path: str, meta: Mapping[str, str]) -> ProjectStore:
        if os.path.exists(path):
            return cls(open_gpkg(path), path)
        tmp = path + ".creating"
        if os.path.exists(tmp):
            os.remove(tmp)
        ds = ddl.create_project_file(tmp, [], {"sa_colmap": "{}", "sa_keys": "[]", **meta})
        del ds  # close before the rename (Windows keeps the file locked)
        os.replace(tmp, path)
        return cls(open_gpkg(path), path)

    def close(self) -> None:
        self.ds = None

    # ----------------------------------------------------------------- meta
    def meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = query(self.ds, f"SELECT value FROM {ddl.META} WHERE key = {sql_str(key)}")  # nosec B608
        return rows[0]["value"] if rows else default

    def meta_json(self, key: str, default):
        raw = self.meta(key)
        if raw in (None, ""):
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_meta(self, values: Mapping[str, object]) -> None:
        with transaction(self.ds):
            for key, value in values.items():
                text = value if isinstance(value, str) else json.dumps(value)
                exec_sql(
                    self.ds,
                    f"INSERT INTO {ddl.META} (key, value) VALUES ({sql_str(key)}, {sql_str(text)}) "  # nosec B608
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                )

    # ---------------------------------------------------------- survey area
    def assigned_ids(self) -> List[int]:
        return [int(g) for g in self.meta_json("assigned_ids", [])]

    def _ensure_columns(self, keys: Sequence[str]) -> Dict[str, str]:
        colmap = extend_colmap(self.meta_json("sa_colmap", {}), keys)
        lyr = self.ds.GetLayerByName(SURVEY_AREA_TABLE)
        defn = lyr.GetLayerDefn()
        for key in keys:
            if defn.GetFieldIndex(colmap[key]) < 0:
                lyr.CreateField(ddl.text_field(colmap[key]))
        return colmap

    def replace_survey_area(
        self, polygons: Sequence[SurveyPolygon], keys: Sequence[str]
    ) -> Tuple[bool, bool, Set[int]]:
        """Mirror exactly the assigned polygons. Returns ``(geometry_changed,
        anything_changed, reshaped)``: the first drives the prune of features
        outside the area, the second a repaint (e.g. ``completed`` flipped).
        ``reshaped`` are polygons we had before whose geometry changed: editing
        a polygon moves only its own ``edited_on``, not the features it now
        covers, so their pull must start over (no watermark)."""
        colmap = self._ensure_columns(keys)
        lyr = self.ds.GetLayerByName(SURVEY_AREA_TABLE)
        defn = lyr.GetLayerDefn()
        existing = {
            int(r["gid_s"]): int(r["sfid"])
            for r in query(
                self.ds,
                f"SELECT CAST(fid AS INTEGER) AS sfid, CAST(gd_id AS TEXT) AS gid_s FROM {SURVEY_AREA_TABLE}",  # nosec B608
            )
            if r["gid_s"] is not None
        }
        geo_digest = hashlib.sha1(usedforsecurity=False)
        full_digest = hashlib.sha1(usedforsecurity=False)
        poly_digests: Dict[str, str] = {}
        for poly in sorted(polygons, key=lambda p: p.gid):
            geo_digest.update(f"{poly.gid}:".encode() + poly.wkb)
            full_digest.update(f"{poly.gid}:{int(poly.completed)}:".encode() + poly.wkb)
            full_digest.update(json.dumps(dict(poly.attrs), sort_keys=True).encode())
            poly_digests[str(poly.gid)] = hashlib.sha1(poly.wkb, usedforsecurity=False).hexdigest()
        geo_hex, full_hex = geo_digest.hexdigest(), full_digest.hexdigest()
        geometry_changed = geo_hex != self.meta("sa_geo_digest")
        anything_changed = full_hex != self.meta("sa_full_digest")
        known = self.meta_json("sa_poly_digests", None)
        # A file from before per-polygon digests: record them, report nothing.
        reshaped = (
            {int(g) for g, d in poly_digests.items() if g in known and known[g] != d}
            if isinstance(known, dict)
            else set()
        )
        if not anything_changed and set(existing) == {p.gid for p in polygons}:
            if not isinstance(known, dict):
                self.set_meta({"sa_poly_digests": poly_digests})
            return False, False, reshaped
        keep = set()
        with transaction(self.ds):
            for poly in polygons:
                fid = existing.get(poly.gid)
                feat = lyr.GetFeature(fid) if fid is not None else ogr.Feature(defn)
                feat.SetGeometry(ogr.CreateGeometryFromWkb(poly.wkb))
                feat.SetFieldInteger64(defn.GetFieldIndex("gd_id"), int(poly.gid))
                feat.SetField(defn.GetFieldIndex("completed"), 1 if poly.completed else 0)
                for key in keys:
                    idx = defn.GetFieldIndex(colmap[key])
                    value = poly.attrs.get(key)
                    if value in (None, ""):
                        feat.SetFieldNull(idx)
                    else:
                        feat.SetField(idx, str(value))
                if fid is None:
                    check_ogr(lyr.CreateFeature(feat), "insert survey area polygon")
                else:
                    check_ogr(lyr.SetFeature(feat), "update survey area polygon")
                keep.add(poly.gid)
            gone = [fid for gid, fid in existing.items() if gid not in keep]
            if gone:
                exec_sql(self.ds, f"DELETE FROM {SURVEY_AREA_TABLE} WHERE fid IN {sql_int_list(gone)}")  # nosec B608
            for key, value in {
                "sa_colmap": json.dumps(colmap),
                "sa_keys": json.dumps(list(keys)),
                "assigned_ids": json.dumps(sorted(p.gid for p in polygons)),
                "sa_geo_digest": geo_hex,
                "sa_full_digest": full_hex,
                "sa_poly_digests": json.dumps(poly_digests, sort_keys=True),
            }.items():
                exec_sql(
                    self.ds,
                    f"INSERT INTO {ddl.META} (key, value) VALUES ({sql_str(key)}, {sql_str(value)}) "  # nosec B608
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                )
        return geometry_changed, True, reshaped

    def fence(self):
        """Union of the assigned polygons, buffered by the Android tolerance —
        an OGR geometry, or ``None`` when nothing is assigned."""
        lyr = self.ds.GetLayerByName(SURVEY_AREA_TABLE)
        collection = ogr.Geometry(ogr.wkbMultiPolygon)
        lyr.ResetReading()
        feat = lyr.GetNextFeature()
        while feat is not None:
            geom = feat.GetGeometryRef()
            if geom is not None and not geom.IsEmpty():
                for i in range(geom.GetGeometryCount()):
                    collection.AddGeometry(geom.GetGeometryRef(i))
            feat = lyr.GetNextFeature()
        if collection.IsEmpty():
            return None
        union = collection.UnionCascaded()
        if union is None:
            return None
        if not union.IsValid():
            union = union.Buffer(0)
        return union.Buffer(FENCE_TOLERANCE_DEG)
