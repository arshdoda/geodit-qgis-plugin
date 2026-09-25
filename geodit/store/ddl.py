"""GeoPackage layout for one synced layer / one project.

Per layer — ``layer_<shp_id>.gpkg`` (the unit of atomicity and locking):

* ``shp_<id>`` — the editable feature table QGIS shows (Multi*, 2D, EPSG:4326)
  with ``gd_id`` / ``gd_ans_id`` display copies and one TEXT(300) column per
  server attribute key.
* ``shp_<id>__discarded`` — local copies of edits the server made impossible
  (feature deleted on the server). Visible, never synced.
* ``gpkgext_geodit_*`` — sync bookkeeping. The ``gpkgext_`` prefix keeps GDAL
  (hence the QGIS Browser) from listing them; they are registered in
  ``gpkg_extensions`` as the GeoPackage spec expects.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Set

from osgeo import ogr

from ..core.colmap import quote_ident
from ..core.wkb import GTYPE_TO_MULTI
from .gpkg import META_TABLE, create_gpkg, exec_sql, query, sql_str, srs_4326

SCHEMA_VERSION = 2  # 2: base.gd_edited

BASE = "gpkgext_geodit_base"
STAGED = "gpkgext_geodit_staged"
MINTED = "gpkgext_geodit_minted"
PARKED = "gpkgext_geodit_parked"
META = META_TABLE

ATTR_WIDTH = 300
EXTENSION_NAME = "geodit_sync"
# ``base.gd_edited``: the server's ``edited_on`` (as sent) of the copy ``base``
# holds, when it came from a pull — lets a re-delivered, unchanged row be
# skipped. NULL when unknown (our own upload), so the next delivery applies.
EDITED = "gd_edited"

_OGR_MULTI = {1: ogr.wkbMultiPoint, 2: ogr.wkbMultiLineString, 3: ogr.wkbMultiPolygon}
assert set(_OGR_MULTI) == set(GTYPE_TO_MULTI)


def layer_table(shp_id: int) -> str:
    return f"shp_{int(shp_id)}"


def discarded_table(shp_id: int) -> str:
    return f"shp_{int(shp_id)}__discarded"


def text_field(name: str) -> ogr.FieldDefn:
    fd = ogr.FieldDefn(name, ogr.OFTString)
    fd.SetWidth(ATTR_WIDTH)
    return fd


def _create_feature_table(ds, name: str, g_type: int, cols: Iterable[str], extra: Iterable[str] = ()):
    lyr = ds.CreateLayer(
        name,
        srs_4326(),
        _OGR_MULTI[int(g_type)],
        options=["FID=fid", "GEOMETRY_NAME=geom", "SPATIAL_INDEX=YES"],
    )
    if lyr is None:
        raise RuntimeError(f"cannot create table {name}")
    lyr.CreateField(ogr.FieldDefn("gd_id", ogr.OFTInteger64))
    lyr.CreateField(ogr.FieldDefn("gd_ans_id", ogr.OFTInteger64))
    for col in cols:
        lyr.CreateField(text_field(col))
    for col in extra:
        lyr.CreateField(ogr.FieldDefn(col, ogr.OFTString))
    return lyr


def _col_defs(cols: Iterable[str]) -> str:
    return "".join(f", {quote_ident(c)} TEXT" for c in cols)


def _register_extensions(ds, tables: List[str]) -> None:
    exec_sql(
        ds,
        "CREATE TABLE IF NOT EXISTS gpkg_extensions (table_name TEXT, column_name TEXT, "
        "extension_name TEXT NOT NULL, definition TEXT NOT NULL, scope TEXT NOT NULL, "
        "CONSTRAINT ge_tce UNIQUE (table_name, column_name, extension_name))",
    )
    for table in tables:
        exec_sql(
            ds,
            "INSERT OR IGNORE INTO gpkg_extensions "
            "(table_name, column_name, extension_name, definition, scope) VALUES "
            f"({sql_str(table)}, NULL, '{EXTENSION_NAME}', 'Geodit QGIS sync state', 'read-write')",
        )


def create_meta(ds, values: Dict[str, str]) -> None:
    exec_sql(ds, f"CREATE TABLE {META} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    rows = {"_lock": "0", "schema_version": str(SCHEMA_VERSION), **values}
    for key, value in rows.items():
        exec_sql(ds, f"INSERT INTO {META} (key, value) VALUES ({sql_str(key)}, {sql_str(value)})")


def create_layer_file(path: str, shp_id: int, g_type: int, cols: List[str], meta: Dict[str, str]):
    """Create ``layer_<shp_id>.gpkg`` with every table up front (QGIS may add
    the file as a layer while the engine holds its own connection)."""
    ds = create_gpkg(path)
    _create_feature_table(ds, layer_table(shp_id), g_type, cols)
    _create_feature_table(ds, discarded_table(shp_id), g_type, cols, extra=("discard_reason", "discarded_at"))
    create_meta(ds, meta)
    exec_sql(
        ds,
        f"CREATE TABLE {BASE} (fid INTEGER PRIMARY KEY, gid INTEGER NOT NULL UNIQUE, g BLOB, "
        f"{EDITED} TEXT{_col_defs(cols)})",
    )
    exec_sql(
        ds,
        f"CREATE TABLE {STAGED} (fid INTEGER PRIMARY KEY, gid INTEGER NOT NULL, op TEXT NOT NULL, g BLOB{_col_defs(cols)})",
    )
    exec_sql(
        ds,
        f"CREATE TABLE {MINTED} (fid INTEGER PRIMARY KEY, gid INTEGER NOT NULL UNIQUE, "
        "sent INTEGER NOT NULL DEFAULT 0, minted_at TEXT NOT NULL)",
    )
    exec_sql(
        ds,
        f"CREATE TABLE {PARKED} (fid INTEGER PRIMARY KEY, gid INTEGER, kind TEXT NOT NULL, "
        "error_code INTEGER, payload_sha1 TEXT, detail TEXT, at TEXT NOT NULL)",
    )
    _register_extensions(ds, [META, BASE, STAGED, MINTED, PARKED])
    return ds


def table_columns(ds, table: str) -> Set[str]:
    rows = query(ds, f"SELECT name FROM pragma_table_info({sql_str(table)})")
    return {r["name"] for r in rows}


def upgrade_layer_file(ds) -> None:
    """Bring a layer file written by an older plugin up to ``SCHEMA_VERSION``.
    Idempotent: each step checks before it changes anything."""
    rows = query(ds, f"SELECT value FROM {META} WHERE key = 'schema_version'")
    if rows and str(rows[0]["value"]) == str(SCHEMA_VERSION):
        return
    if EDITED not in table_columns(ds, BASE):
        exec_sql(ds, f"ALTER TABLE {BASE} ADD COLUMN {EDITED} TEXT")
    exec_sql(
        ds,
        f"INSERT INTO {META} (key, value) VALUES ('schema_version', {sql_str(str(SCHEMA_VERSION))}) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
    )


def add_text_column(ds, shp_id: int, col: str) -> None:
    """Add one synced column to every table that mirrors the layer schema.
    Idempotent per table, so a run interrupted half-way is finished by the
    next one."""
    for table in (layer_table(shp_id), discarded_table(shp_id)):
        lyr = ds.GetLayerByName(table)
        if lyr is not None and lyr.GetLayerDefn().GetFieldIndex(col) < 0:
            lyr.CreateField(text_field(col))
    for table in (BASE, STAGED):
        if col not in table_columns(ds, table):
            exec_sql(ds, f"ALTER TABLE {table} ADD COLUMN {quote_ident(col)} TEXT")


def create_project_file(path: str, sa_cols: List[str], meta: Dict[str, str]):
    ds = create_gpkg(path)
    lyr = ds.CreateLayer(
        "survey_area",
        srs_4326(),
        ogr.wkbMultiPolygon,
        options=["FID=fid", "GEOMETRY_NAME=geom", "SPATIAL_INDEX=YES"],
    )
    lyr.CreateField(ogr.FieldDefn("gd_id", ogr.OFTInteger64))
    completed = ogr.FieldDefn("completed", ogr.OFTInteger)
    completed.SetSubType(ogr.OFSTBoolean)
    lyr.CreateField(completed)
    for col in sa_cols:
        lyr.CreateField(text_field(col))
    create_meta(ds, meta)
    _register_extensions(ds, [META])
    return ds
