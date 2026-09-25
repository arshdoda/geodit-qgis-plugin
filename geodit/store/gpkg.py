"""Thin GDAL helpers for the GeoPackage store.

Kept to the GDAL 3.4 Python API (Ubuntu 22.04's QGIS 3.40 builds): no
``ExceptionMgr``, no ``ExecuteSQL`` context manager, no ``UpdateFeature``. GDAL's
global exception mode is never changed (it would leak into QGIS and other
plugins); every call is checked explicitly and also guarded for the case where
someone else turned exceptions on.
"""

from __future__ import annotations

import datetime as _dt
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional

from osgeo import gdal, ogr, osr

META_TABLE = "gpkgext_geodit_meta"
BUSY_TIMEOUT_MS = 5000


class StoreError(RuntimeError):
    pass


def _last_error(default: str) -> str:
    msg = gdal.GetLastErrorMsg()
    return msg or default


@contextmanager
def quiet_gdal() -> Iterator[None]:
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        yield
    finally:
        gdal.PopErrorHandler()


def srs_4326():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    if hasattr(srs, "SetAxisMappingStrategy"):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


# Never a journal mode (no OGR_SQLITE_JOURNAL): the file's journal mode belongs
# to QGIS, which keeps its own connection to every layer file — WAL while it
# edits, rollback journaling otherwise. Switching a file to WAL under a QGIS
# connection opened in rollback mode leaves that connection unable to read
# ("unable to open database file") as soon as our connection closes and SQLite
# deletes the -wal/-shm files: the features vanished at the next zoom.
_THREAD_OPTIONS = (("OGR_SQLITE_PRAGMA", f"busy_timeout={BUSY_TIMEOUT_MS}"),)


@contextmanager
def _thread_options() -> Iterator[None]:
    """GDAL options for our own open only: the busy timeout makes a colliding
    QGIS read or commit (or our write) wait instead of failing at once. They
    are thread-local and restored afterwards: QGIS reuses task threads, and the
    main thread, for other GeoPackages."""
    getter = getattr(gdal, "GetThreadLocalConfigOption", None)
    previous = [(key, getter(key, None) if getter is not None else None) for key, _ in _THREAD_OPTIONS]
    for key, value in _THREAD_OPTIONS:
        gdal.SetThreadLocalConfigOption(key, value)
    try:
        yield
    finally:
        for key, value in previous:
            gdal.SetThreadLocalConfigOption(key, value)


def create_gpkg(path: str):
    drv = ogr.GetDriverByName("GPKG")
    with _thread_options(), quiet_gdal():
        try:
            ds = drv.CreateDataSource(path)
        except RuntimeError as exc:
            raise StoreError(f"cannot create {path}: {exc}") from exc
    if ds is None:
        raise StoreError(_last_error(f"cannot create {path}"))
    exec_sql(ds, f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return ds


def open_gpkg(path: str, update: bool = True):
    flags = gdal.OF_VECTOR | (gdal.OF_UPDATE if update else gdal.OF_READONLY)
    with _thread_options(), quiet_gdal():
        try:
            ds = gdal.OpenEx(path, flags, allowed_drivers=["GPKG"])
        except RuntimeError as exc:
            raise StoreError(f"cannot open {path}: {exc}") from exc
    if ds is None:
        raise StoreError(_last_error(f"cannot open {path}"))
    exec_sql(ds, f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return ds


def exec_sql(ds, sql: str) -> None:
    gdal.ErrorReset()
    with quiet_gdal():
        try:
            lyr = ds.ExecuteSQL(sql)
        except RuntimeError as exc:
            raise StoreError(f"{exc} — in: {sql[:300]}") from exc
    if lyr is not None:
        ds.ReleaseResultSet(lyr)
    if gdal.GetLastErrorType() >= gdal.CE_Failure:
        raise StoreError(f"{_last_error('SQL failed')} — in: {sql[:300]}")


def query(ds, sql: str) -> List[Dict[str, Any]]:
    """Rows of a SELECT as dicts. Note OGR turns a selected rowid/PK column into
    the result's FID — select fids as ``CAST(x.fid AS INTEGER) AS alias``."""
    gdal.ErrorReset()
    with quiet_gdal():
        try:
            lyr = ds.ExecuteSQL(sql)
        except RuntimeError as exc:
            raise StoreError(f"{exc} — in: {sql[:300]}") from exc
    if gdal.GetLastErrorType() >= gdal.CE_Failure:
        if lyr is not None:
            ds.ReleaseResultSet(lyr)
        raise StoreError(f"{_last_error('query failed')} — in: {sql[:300]}")
    if lyr is None:
        return []
    try:
        defn = lyr.GetLayerDefn()
        names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
        rows: List[Dict[str, Any]] = []
        feat = lyr.GetNextFeature()
        while feat is not None:
            row = {}
            for i, name in enumerate(names):
                row[name] = feat.GetField(i) if feat.IsFieldSetAndNotNull(i) else None
            rows.append(row)
            feat = lyr.GetNextFeature()
        return rows
    finally:
        ds.ReleaseResultSet(lyr)


def scalar(ds, sql: str) -> Any:
    rows = query(ds, sql)
    if not rows:
        return None
    return next(iter(rows[0].values()), None)


@contextmanager
def transaction(ds) -> Iterator[None]:
    """One write transaction that takes SQLite's write lock FIRST.

    ``StartTransaction`` begins lazily; in WAL mode a transaction that has
    read and then tries to write after another connection committed fails
    with ``SQLITE_BUSY_SNAPSHOT``. A no-op write as the first statement
    acquires the lock up front (waiting up to the busy timeout).
    """
    with quiet_gdal():
        try:
            err = ds.StartTransaction()
        except RuntimeError as exc:
            raise StoreError(f"cannot start transaction: {exc}") from exc
    if err not in (0, None):
        raise StoreError(_last_error("cannot start transaction"))
    try:
        exec_sql(ds, f"UPDATE {META_TABLE} SET value = value WHERE key = '_lock'")  # nosec B608
        yield
    except BaseException:
        with quiet_gdal():
            try:
                ds.RollbackTransaction()
            except RuntimeError:
                pass
        raise
    with quiet_gdal():
        try:
            err = ds.CommitTransaction()
        except RuntimeError as exc:
            raise StoreError(f"commit failed: {exc}") from exc
    if err not in (0, None):
        raise StoreError(_last_error("commit failed"))


# ---------------------------------------------------------------- SQL literals
# GDAL's ExecuteSQL has no parameter binding. Only integers, ISO timestamps,
# hex and our own (escaped) identifiers/strings are ever interpolated; user
# attribute values move between tables through INSERT … SELECT only. That is
# why the f-string statements in geodit/store are marked "# nosec B608".


def sql_int_list(values: Iterable[int]) -> str:
    items = ",".join(str(int(v)) for v in values)
    return f"({items})" if items else "(NULL)"


def sql_str(value: Optional[str]) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def check_ogr(err, what: str) -> None:
    if err not in (0, None):
        raise StoreError(_last_error(f"{what} failed"))
