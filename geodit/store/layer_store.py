"""One synced layer's local store (``layer_<shp_id>.gpkg``).

Identity: the feature table's ``fid`` is the local identity. The server id
(``gid``) is known ONLY through ``gpkgext_geodit_base`` (features the server
knows, with the last-synced content) and ``gpkgext_geodit_minted`` (our own
creates not yet confirmed). ``gd_id`` in the feature table is a read-only
display copy — copy/paste, split and duplicate copy it verbatim, so it can
never decide identity (a pasted id sent as a create would overwrite someone
else's feature: the server treats an existing id as an idempotent retry).

Change detection is a pure-SQL diff of the feature table against ``base``.
Only the engine writes ``base``/``staged``/``minted``/``parked``; the user
(through QGIS) writes the feature table. The engine writes feature rows only
for layers that have no uncommitted QGIS edits, and only in place (never
delete+insert), so fids stay stable under QGIS.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from osgeo import ogr

from ..core.colmap import extend_colmap, quote_ident
from ..core.ops import payload_sha1
from ..core.results import ResultBuckets
from ..core.snowflake import SnowflakeMinter
from ..core.watermark import rewind
from ..core.wkb import WkbError, gpkg_blob_to_wkb
from . import ddl
from .gpkg import (
    StoreError,
    check_ogr,
    exec_sql,
    open_gpkg,
    query,
    sql_int_list,
    sql_str,
    transaction,
    utc_now_iso,
)

_IN_CHUNK = 500

STATE_ACTIVE = "active"
STATE_ORPHANED = "orphaned"


def _chunks(values: Sequence, size: int = _IN_CHUNK):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _q(name: str) -> str:
    return quote_ident(name)


def _norm(value) -> str:
    return "" if value is None else str(value)


@dataclass
class Changes:
    new_fids: List[int] = field(default_factory=list)
    deleted: List[Tuple[int, int]] = field(default_factory=list)  # (fid, gid)
    changed_fids: List[int] = field(default_factory=list)

    @property
    def pending(self) -> int:
        return len(self.new_fids) + len(self.deleted) + len(self.changed_fids)


@dataclass
class StagedRow:
    fid: int
    gid: int
    op: str
    wkb: Optional[bytes]
    attrs: Dict[str, Optional[str]]


@dataclass
class SchemaChange:
    added_keys: List[str] = field(default_factory=list)
    retired_keys: List[str] = field(default_factory=list)
    repaired_cols: List[str] = field(default_factory=list)
    deferred: bool = False

    @property
    def altered(self) -> bool:
        return bool(self.added_keys or self.repaired_cols)


@dataclass
class PageOutcome:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    discarded: int = 0
    kept_local: int = 0
    ids_written: int = 0  # only gd_id / gd_ans_id changed (e.g. our create came back)

    @property
    def rows_changed(self) -> bool:
        return bool(self.inserted or self.updated or self.deleted or self.discarded or self.ids_written)


class LayerStore:
    def __init__(self, ds, path: str, shp_id: int) -> None:
        self.ds = ds
        self.path = path
        self.shp_id = int(shp_id)
        self.table = ddl.layer_table(shp_id)
        self.discarded_table = ddl.discarded_table(shp_id)

    # ------------------------------------------------------------ lifecycle
    @classmethod
    def open_or_create(
        cls, path: str, shp_id: int, *, g_type: int, name: str, attr_keys: Sequence[str]
    ) -> Tuple[LayerStore, bool]:
        if os.path.exists(path):
            ds = open_gpkg(path)
            ddl.upgrade_layer_file(ds)
            return cls(ds, path, shp_id), False
        colmap = extend_colmap({}, attr_keys)
        tmp = path + ".creating"
        if os.path.exists(tmp):
            os.remove(tmp)
        ds = ddl.create_layer_file(
            tmp,
            shp_id,
            g_type,
            [colmap[k] for k in attr_keys],
            {
                "shp_id": str(shp_id),
                "g_type": str(int(g_type)),
                "name": name,
                "attr_keys": json.dumps(list(attr_keys)),
                "colmap": json.dumps(colmap),
                "retired": "[]",
                "watermark_ms": "",
                "drained_geoms": "[]",
                "last_mint_ms": "0",
                "last_mint_seq": "-1",
                "state": STATE_ACTIVE,
            },
        )
        del ds  # close before the rename (Windows keeps the file locked)
        os.replace(tmp, path)
        return cls(open_gpkg(path), path, shp_id), True

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
        for key, value in values.items():
            text = value if isinstance(value, str) else json.dumps(value)
            exec_sql(
                self.ds,
                f"INSERT INTO {ddl.META} (key, value) VALUES ({sql_str(key)}, {sql_str(text)}) "  # nosec B608
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            )

    @property
    def state(self) -> str:
        return self.meta("state", STATE_ACTIVE) or STATE_ACTIVE

    @property
    def g_type(self) -> int:
        return int(self.meta("g_type") or 0)

    def watermark(self) -> Optional[int]:
        raw = self.meta("watermark_ms")
        return int(raw) if raw not in (None, "") else None

    def drained_geoms(self) -> Set[int]:
        return {int(g) for g in self.meta_json("drained_geoms", [])}

    # --------------------------------------------------------------- schema
    def colmap(self) -> Dict[str, str]:
        return dict(self.meta_json("colmap", {}))

    def synced(self) -> List[Tuple[str, str]]:
        """``(server key, column)`` pairs currently synced, in server order."""
        colmap = self.colmap()
        retired = set(self.meta_json("retired", []))
        return [(key, colmap[key]) for key in self.meta_json("attr_keys", []) if key in colmap and key not in retired]

    def _layer_field_names(self) -> Set[str]:
        lyr = self.ds.GetLayerByName(self.table)
        defn = lyr.GetLayerDefn()
        return {defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())}

    def reconcile_schema(self, server_keys: Sequence[str], *, can_alter: bool) -> SchemaChange:
        """Follow the server's ``attr_head``. New keys get a column (and rewind
        the watermark); removed keys retire (kept locally, never sent); a synced
        column the user deleted is re-added and refilled from ``base``."""
        change = SchemaChange()
        colmap = self.colmap()
        server_keys = list(server_keys)
        new_keys = [k for k in server_keys if k not in colmap]
        change.retired_keys = [k for k in colmap if k not in server_keys]
        fields = self._layer_field_names()
        missing = [colmap[k] for k in server_keys if k in colmap and colmap[k] not in fields]
        if (new_keys or missing) and not can_alter:
            change.deferred = True
            new_keys, missing = [], []
        new_colmap = extend_colmap(colmap, new_keys)
        for key in new_keys:
            ddl.add_text_column(self.ds, self.shp_id, new_colmap[key])
            change.added_keys.append(key)
        for col in missing:
            self.ds.GetLayerByName(self.table).CreateField(ddl.text_field(col))
            with transaction(self.ds):
                exec_sql(
                    self.ds,
                    f"UPDATE {_q(self.table)} SET {_q(col)} = "  # nosec B608
                    f"(SELECT b.{_q(col)} FROM {ddl.BASE} b WHERE b.fid = {_q(self.table)}.fid)",
                )
            change.repaired_cols.append(col)
        known_keys = [k for k in server_keys if k in new_colmap]
        values: Dict[str, object] = {
            "colmap": new_colmap,
            "attr_keys": known_keys,
            "retired": change.retired_keys,
        }
        with transaction(self.ds):
            if change.added_keys:
                values["watermark_ms"] = _opt_int_text(rewind(self.watermark()))
                # The rewind re-delivers rows whose values for the new keys may
                # have been dropped: they must be applied again, not skipped.
                exec_sql(self.ds, f"UPDATE {ddl.BASE} SET {ddl.EDITED} = NULL")  # nosec B608
            self.set_meta(values)
        return change

    # ----------------------------------------------------------- change diff
    def _changed_predicate(self, left: str, right: str, cols: Sequence[str]) -> str:
        preds = [f"{left}.{'geom' if left == 'l' else 'g'} IS NOT {right}.g"]
        preds += [f"IFNULL({left}.{_q(c)}, '') <> IFNULL({right}.{_q(c)}, '')" for c in cols]
        return " OR ".join(preds)

    def detect_changes(self) -> Changes:
        t = _q(self.table)
        cols = [c for _, c in self.synced()]
        out = Changes()
        out.new_fids = [
            int(r["lfid"])
            for r in query(
                self.ds,
                f"SELECT CAST(l.fid AS INTEGER) AS lfid FROM {t} l "  # nosec B608
                f"LEFT JOIN {ddl.BASE} b ON b.fid = l.fid WHERE b.fid IS NULL ORDER BY l.fid",
            )
        ]
        out.deleted = [
            (int(r["bfid"]), int(r["gid_s"]))
            for r in query(
                self.ds,
                f"SELECT CAST(b.fid AS INTEGER) AS bfid, CAST(b.gid AS TEXT) AS gid_s "  # nosec B608
                f"FROM {ddl.BASE} b LEFT JOIN {t} l ON l.fid = b.fid WHERE l.fid IS NULL",
            )
        ]
        out.changed_fids = [
            int(r["lfid"])
            for r in query(
                self.ds,
                f"SELECT CAST(l.fid AS INTEGER) AS lfid FROM {t} l JOIN {ddl.BASE} b ON b.fid = l.fid "  # nosec B608
                f"WHERE {self._changed_predicate('l', 'b', cols)} ORDER BY l.fid",
            )
        ]
        return out

    def locally_deleted(self) -> List[int]:
        """Fids the server knows that are gone from the feature table."""
        t = _q(self.table)
        return [
            int(r["bfid"])
            for r in query(
                self.ds,
                f"SELECT CAST(b.fid AS INTEGER) AS bfid FROM {ddl.BASE} b "  # nosec B608
                f"LEFT JOIN {t} l ON l.fid = b.fid WHERE l.fid IS NULL ORDER BY b.fid",
            )
        ]

    def _modified_among(self, fids: Iterable[int]) -> Set[int]:
        t = _q(self.table)
        cols = [c for _, c in self.synced()]
        out: Set[int] = set()
        for part in _chunks(sorted(set(fids))):
            out.update(
                int(r["lfid"])
                for r in query(
                    self.ds,
                    f"SELECT CAST(l.fid AS INTEGER) AS lfid FROM {t} l JOIN {ddl.BASE} b ON b.fid = l.fid "  # nosec B608
                    f"WHERE l.fid IN {sql_int_list(part)} AND ({self._changed_predicate('l', 'b', cols)})",
                )
            )
        return out

    def base_gids(self, fids: Iterable[int]) -> Dict[int, int]:
        out: Dict[int, int] = {}
        for part in _chunks(sorted(set(fids))):
            for r in query(
                self.ds,
                f"SELECT CAST(fid AS INTEGER) AS bfid, CAST(gid AS TEXT) AS gid_s "  # nosec B608
                f"FROM {ddl.BASE} WHERE fid IN {sql_int_list(part)}",
            ):
                out[int(r["bfid"])] = int(r["gid_s"])
        return out

    # ----------------------------------------------------------------- mint
    def minted(self) -> Dict[int, Tuple[int, bool]]:
        return {
            int(r["mfid"]): (int(r["gid_s"]), bool(r["sent"]))
            for r in query(
                self.ds,
                f"SELECT CAST(fid AS INTEGER) AS mfid, CAST(gid AS TEXT) AS gid_s, sent FROM {ddl.MINTED}",  # nosec B608
            )
        }

    def mint(self, fids: Sequence[int], worker_id: int, now_ms: Callable[[], int]) -> Dict[int, int]:
        """Give each new row a snowflake and COMMIT it before anything is sent,
        so a retried create reuses the same id (the server's idempotent-retry
        path) instead of duplicating the feature."""
        if not fids:
            return {}
        minter = SnowflakeMinter(
            worker_id,
            int(self.meta("last_mint_ms") or 0),
            int(self.meta("last_mint_seq") or -1),
        )
        out: Dict[int, int] = {}
        stamp = utc_now_iso()
        with transaction(self.ds):
            for fid in fids:
                gid = minter.mint(now_ms())
                exec_sql(
                    self.ds,
                    f"INSERT INTO {ddl.MINTED} (fid, gid, sent, minted_at) "  # nosec B608
                    f"VALUES ({int(fid)}, {gid}, 0, {sql_str(stamp)})",
                )
                out[int(fid)] = gid
            last_ms, last_seq = minter.state
            self.set_meta({"last_mint_ms": str(last_ms), "last_mint_seq": str(last_seq)})
        return out

    def drop_minted(self, fids: Iterable[int]) -> None:
        fids = list(fids)
        if not fids:
            return
        with transaction(self.ds):
            for part in _chunks(fids):
                exec_sql(self.ds, f"DELETE FROM {ddl.MINTED} WHERE fid IN {sql_int_list(part)}")  # nosec B608
                exec_sql(self.ds, f"DELETE FROM {ddl.STAGED} WHERE fid IN {sql_int_list(part)}")  # nosec B608
                exec_sql(self.ds, f"DELETE FROM {ddl.PARKED} WHERE fid IN {sql_int_list(part)}")  # nosec B608

    # --------------------------------------------------------------- staging
    def stage(self, entries: Sequence[Tuple[int, int, str]]) -> None:
        """Snapshot rows ``(fid, gid, op)`` into ``staged``. Ops are built FROM
        this snapshot and an ok result copies it into ``base``, so an edit the
        user commits while the request is in flight is never mistaken for
        synced — it still differs from ``base`` and goes out next tick."""
        if not entries:
            return
        t = _q(self.table)
        cols = [c for _, c in self.synced()]
        col_list = "".join(f", {_q(c)}" for c in cols)
        sel_list = "".join(f", l.{_q(c)}" for c in cols)
        with transaction(self.ds):
            for part in _chunks(list(entries)):
                case = " ".join(f"WHEN {int(f)} THEN {int(g)}" for f, g, _ in part)
                ops = " ".join(f"WHEN {int(f)} THEN {sql_str(o)}" for f, _, o in part)
                exec_sql(
                    self.ds,
                    f"INSERT OR REPLACE INTO {ddl.STAGED} (fid, gid, op, g{col_list}) "  # nosec B608
                    f"SELECT l.fid, CASE l.fid {case} END, CASE l.fid {ops} END, l.geom{sel_list} "
                    f"FROM {t} l WHERE l.fid IN {sql_int_list(f for f, _, _ in part)}",
                )
                creates = [f for f, _, o in part if o == "create"]
                if creates:
                    exec_sql(
                        self.ds,
                        f"UPDATE {ddl.MINTED} SET sent = 1 WHERE fid IN {sql_int_list(creates)}",  # nosec B608
                    )

    def staged_rows(self, fids: Iterable[int]) -> Dict[int, StagedRow]:
        synced = self.synced()
        sel = "".join(f", s.{_q(c)} AS {_q('c_' + str(i))}" for i, (_, c) in enumerate(synced))
        out: Dict[int, StagedRow] = {}
        for part in _chunks(sorted(set(fids))):
            for r in query(
                self.ds,
                f"SELECT CAST(s.fid AS INTEGER) AS sfid, CAST(s.gid AS TEXT) AS gid_s, s.op AS op, "  # nosec B608
                f"hex(s.g) AS g_hex{sel} FROM {ddl.STAGED} s WHERE s.fid IN {sql_int_list(part)}",
            ):
                wkb = None
                if r["g_hex"]:
                    try:
                        wkb = gpkg_blob_to_wkb(bytes.fromhex(r["g_hex"]))
                    except (WkbError, ValueError):
                        wkb = None
                attrs = {key: r.get(f"c_{i}") for i, (key, _) in enumerate(synced)}
                fid = int(r["sfid"])
                out[fid] = StagedRow(fid, int(r["gid_s"]), r["op"], wkb, attrs)
        return out

    def staged_changes(self, fids: Iterable[int]) -> Dict[int, Tuple[bool, List[str]]]:
        """For staged updates: did the geometry change, and which keys, vs base."""
        synced = self.synced()
        flags = "".join(
            f", (IFNULL(s.{_q(c)}, '') <> IFNULL(b.{_q(c)}, '')) AS {_q('c_' + str(i))}"
            for i, (_, c) in enumerate(synced)
        )
        out: Dict[int, Tuple[bool, List[str]]] = {}
        for part in _chunks(sorted(set(fids))):
            for r in query(
                self.ds,
                f"SELECT CAST(s.fid AS INTEGER) AS sfid, (s.g IS NOT b.g) AS gch{flags} "  # nosec B608
                f"FROM {ddl.STAGED} s JOIN {ddl.BASE} b ON b.fid = s.fid WHERE s.fid IN {sql_int_list(part)}",
            ):
                keys = [key for i, (key, _) in enumerate(synced) if r.get(f"c_{i}")]
                out[int(r["sfid"])] = (bool(r["gch"]), keys)
        return out

    def unstage(self, fids: Iterable[int]) -> None:
        fids = list(fids)
        if fids:
            with transaction(self.ds):
                for part in _chunks(fids):
                    exec_sql(self.ds, f"DELETE FROM {ddl.STAGED} WHERE fid IN {sql_int_list(part)}")  # nosec B608

    # ---------------------------------------------------------------- parked
    def parked(self) -> Dict[int, dict]:
        return {
            int(r["pfid"]): r
            for r in query(
                self.ds,
                f"SELECT CAST(fid AS INTEGER) AS pfid, CAST(gid AS TEXT) AS gid_s, kind, error_code, "  # nosec B608
                f"payload_sha1, detail, at FROM {ddl.PARKED}",
            )
        }

    def clear_parked(self, fids: Iterable[int]) -> None:
        fids = list(fids)
        if fids:
            with transaction(self.ds):
                for part in _chunks(fids):
                    exec_sql(self.ds, f"DELETE FROM {ddl.PARKED} WHERE fid IN {sql_int_list(part)}")  # nosec B608

    def _park(
        self, fid: int, gid: Optional[int], kind: str, code: Optional[int], sha1: Optional[str], detail: str
    ) -> None:
        exec_sql(
            self.ds,
            f"INSERT OR REPLACE INTO {ddl.PARKED} (fid, gid, kind, error_code, payload_sha1, detail, at) VALUES "
            f"({int(fid)}, {int(gid) if gid is not None else 'NULL'}, {sql_str(kind)}, "
            f"{int(code) if code is not None else 'NULL'}, {sql_str(sha1)}, {sql_str(detail)}, {sql_str(utc_now_iso())})",
        )

    # ------------------------------------------------------------- push acks
    def _discard_row(self, fid: int, gid: Optional[int], reason: str) -> None:
        cols = [c for _, c in self.synced()]
        dcols = self._layer_field_names_of(self.discarded_table)
        cols = [c for c in cols if c in dcols]
        col_list = "".join(f", {_q(c)}" for c in cols)
        exec_sql(
            self.ds,
            f"INSERT INTO {_q(self.discarded_table)} (geom, gd_id, gd_ans_id{col_list}, discard_reason, discarded_at) "  # nosec B608
            f"SELECT geom, {int(gid) if gid is not None else 'gd_id'}, gd_ans_id{col_list}, {sql_str(reason)}, "
            f"{sql_str(utc_now_iso())} FROM {_q(self.table)} WHERE fid = {int(fid)}",
        )
        exec_sql(self.ds, f"DELETE FROM {_q(self.table)} WHERE fid = {int(fid)}")  # nosec B608
        for table in (ddl.BASE, ddl.STAGED, ddl.MINTED, ddl.PARKED):
            exec_sql(self.ds, f"DELETE FROM {table} WHERE fid = {int(fid)}")  # nosec B608

    def _layer_field_names_of(self, table: str) -> Set[str]:
        lyr = self.ds.GetLayerByName(table)
        defn = lyr.GetLayerDefn()
        return {defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())}

    def _base_from_staged(self, fids: Sequence[int]) -> None:
        """Upsert ``base`` from ``staged``: what was sent is now the server's copy.
        Its server ``edited_on`` isn't known, so the next delivery is applied."""
        cols = [c for _, c in self.synced()]
        col_list = "".join(f", {_q(c)}" for c in cols)
        sel_list = "".join(f", s.{_q(c)}" for c in cols)
        updates = ", ".join(
            ["gid = excluded.gid", "g = excluded.g", f"{ddl.EDITED} = NULL"]
            + [f"{_q(c)} = excluded.{_q(c)}" for c in cols]
        )
        for part in _chunks(list(fids)):
            exec_sql(
                self.ds,
                f"INSERT INTO {ddl.BASE} (fid, gid, g{col_list}) "  # nosec B608
                f"SELECT s.fid, s.gid, s.g{sel_list} FROM {ddl.STAGED} s WHERE s.fid IN {sql_int_list(part)} "
                f"ON CONFLICT(fid) DO UPDATE SET {updates}",
            )

    def _base_from_layer(self, fid_to_gid: Mapping[int, int], edited: Optional[Mapping[int, str]] = None) -> None:
        """Upsert ``base`` from the feature table. ``edited`` (gid → the
        server's ``edited_on``) is recorded for rows that came from a pull."""
        if not fid_to_gid:
            return
        edited = edited or {}
        cols = [c for _, c in self.synced()]
        col_list = "".join(f", {_q(c)}" for c in cols)
        sel_list = "".join(f", l.{_q(c)}" for c in cols)
        updates = ", ".join(
            ["gid = excluded.gid", "g = excluded.g", f"{ddl.EDITED} = excluded.{ddl.EDITED}"]
            + [f"{_q(c)} = excluded.{_q(c)}" for c in cols]
        )
        items = list(fid_to_gid.items())
        for part in _chunks(items):
            case = " ".join(f"WHEN {int(f)} THEN {int(g)}" for f, g in part)
            ed_case = " ".join(f"WHEN {int(f)} THEN {sql_str(edited[g])}" for f, g in part if edited.get(g))
            ed_expr = f"CASE l.fid {ed_case} END" if ed_case else "NULL"
            exec_sql(
                self.ds,
                f"INSERT INTO {ddl.BASE} (fid, gid, g, {ddl.EDITED}{col_list}) "  # nosec B608
                f"SELECT l.fid, CASE l.fid {case} END, l.geom, {ed_expr}{sel_list} FROM {_q(self.table)} l "
                f"WHERE l.fid IN {sql_int_list(f for f, _ in part)} "
                f"ON CONFLICT(fid) DO UPDATE SET {updates}",
            )

    def _set_edited(self, edited_by_fid: Mapping[int, str]) -> None:
        """Record the server ``edited_on`` of rows whose server copy matched ``base``."""
        for fid, value in edited_by_fid.items():
            exec_sql(self.ds, f"UPDATE {ddl.BASE} SET {ddl.EDITED} = {sql_str(value)} WHERE fid = {int(fid)}")  # nosec B608

    def already_applied(self, rows: Sequence[Mapping]) -> Set[int]:
        """Gids of live ``feat-list`` rows this store already holds exactly as
        the server sent them (same ``edited_on``), with no local change — the
        watermark overlap re-delivers them on every tick; nothing to apply."""
        want = {
            int(r["id"]): str(r["edited_on"])
            for r in rows
            if not r.get("deleted") and r.get("edited_on") not in (None, "")
        }
        if not want:
            return set()
        t = _q(self.table)
        hits: Dict[int, int] = {}
        for part in _chunks(list(want)):
            for r in query(
                self.ds,
                f"SELECT CAST(b.fid AS INTEGER) AS bfid, CAST(b.gid AS TEXT) AS gid_s, b.{ddl.EDITED} AS ed "  # nosec B608
                f"FROM {ddl.BASE} b JOIN {t} l ON l.fid = b.fid WHERE b.gid IN {sql_int_list(part)}",
            ):
                gid = int(r["gid_s"])
                if r["ed"] is not None and r["ed"] == want.get(gid):
                    hits[int(r["bfid"])] = gid
        modified = self._modified_among(hits)
        return {gid for fid, gid in hits.items() if fid not in modified}

    def apply_push_results(
        self,
        buckets: ResultBuckets,
        fid_by_gid: Mapping[int, int],
        ops_by_gid: Mapping[int, dict],
        *,
        can_write_layer: Callable[[], bool],
        messages: Mapping[int, str],
    ) -> int:
        """Record one batch's outcome in a single transaction. Returns the
        number of rows moved to ``__discarded``."""
        discarded = 0
        with transaction(self.ds):
            acked = [fid_by_gid[g] for g in buckets.acked if g in fid_by_gid]
            if acked:
                self._base_from_staged(acked)
                for part in _chunks(acked):
                    for table in (ddl.MINTED, ddl.PARKED, ddl.STAGED):
                        exec_sql(self.ds, f"DELETE FROM {table} WHERE fid IN {sql_int_list(part)}")  # nosec B608
            gone = [fid_by_gid[g] for g in buckets.deleted if g in fid_by_gid]
            for part in _chunks(gone):
                for table in (ddl.BASE, ddl.MINTED, ddl.PARKED, ddl.STAGED):
                    exec_sql(self.ds, f"DELETE FROM {table} WHERE fid IN {sql_int_list(part)}")  # nosec B608
            for gid in buckets.discard:
                fid = fid_by_gid.get(gid)
                if fid is None:
                    continue
                if can_write_layer():
                    self._discard_row(fid, gid, "Deleted on the server")
                    discarded += 1
                else:
                    self._park(fid, gid, "discard_pending", 404, None, "Deleted on the server")
            for gid, code in buckets.rejected:
                fid = fid_by_gid.get(gid)
                op = ops_by_gid.get(gid)
                if fid is None or op is None:
                    continue
                self._park(fid, gid, "rejected", code, payload_sha1(op), messages.get(gid, "Rejected by the server"))
            if buckets.orphaned_layer:
                self.set_meta({"state": STATE_ORPHANED})
        return discarded

    # --------------------------------------------------------------- restore
    def restore_deleted(self, fids: Iterable[int]) -> int:
        """Put features the user deleted locally back into the feature table,
        exactly as last synced (same fid and gid, geometry and synced values)
        — deleting isn't allowed for this user. Rows waiting to be discarded
        (deleted on the server) are left to ``run_pending_discards``.
        ``gd_ans_id`` isn't kept in ``base``: it reappears with the next server
        copy of the feature (it is a display copy only)."""
        wanted = set(int(f) for f in fids)
        if not wanted:
            return 0
        waiting = {fid for fid, r in self.parked().items() if r["kind"] == "discard_pending"}
        victims = [fid for fid in self.locally_deleted() if fid in wanted and fid not in waiting]
        if not victims:
            return 0
        fields = self._layer_field_names()
        cols = [c for _, c in self.synced() if c in fields]
        col_list = "".join(f", {_q(c)}" for c in cols)
        sel_list = "".join(f", b.{_q(c)}" for c in cols)
        with transaction(self.ds):
            for part in _chunks(victims):
                exec_sql(
                    self.ds,
                    f"INSERT INTO {_q(self.table)} (fid, geom, gd_id{col_list}) "  # nosec B608
                    f"SELECT b.fid, b.g, b.gid{sel_list} FROM {ddl.BASE} b WHERE b.fid IN {sql_int_list(part)}",
                )
                for table in (ddl.STAGED, ddl.PARKED):
                    exec_sql(self.ds, f"DELETE FROM {table} WHERE fid IN {sql_int_list(part)}")  # nosec B608
        return len(victims)

    def revert_local(self, reason: str) -> Tuple[int, int, int]:
        """Back the "Discard unsynced changes" action: make the layer match the last-synced copy.
        Deleted rows come back, changed rows get the server's content (the
        local version is kept in ``__discarded``), new rows move to
        ``__discarded``. Returns ``(restored, reverted, discarded)``."""
        changes = self.detect_changes()
        minted = self.minted()
        waiting = {fid for fid, r in self.parked().items() if r["kind"] == "discard_pending"}
        restored = self.restore_deleted(fid for fid, _ in changes.deleted)
        changed = [fid for fid in changes.changed_fids if fid not in waiting]
        new = [fid for fid in changes.new_fids if fid not in waiting]
        fields = self._layer_field_names()
        cols = [c for _, c in self.synced() if c in fields]
        t = _q(self.table)
        if changed:
            dcols = self._layer_field_names_of(self.discarded_table)
            keep = [c for c in cols if c in dcols]
            keep_list = "".join(f", {_q(c)}" for c in keep)
            keep_sel = "".join(f", l.{_q(c)}" for c in keep)
            sets = ", ".join(
                [f"geom = (SELECT b.g FROM {ddl.BASE} b WHERE b.fid = {t}.fid)"]  # nosec B608
                + [f"{_q(c)} = (SELECT b.{_q(c)} FROM {ddl.BASE} b WHERE b.fid = {t}.fid)" for c in cols]  # nosec B608
            )
            with transaction(self.ds):
                for part in _chunks(changed):
                    exec_sql(
                        self.ds,
                        f"INSERT INTO {_q(self.discarded_table)} "  # nosec B608
                        f"(geom, gd_id, gd_ans_id{keep_list}, discard_reason, discarded_at) "
                        f"SELECT l.geom, b.gid, l.gd_ans_id{keep_sel}, {sql_str(reason)}, {sql_str(utc_now_iso())} "
                        f"FROM {t} l JOIN {ddl.BASE} b ON b.fid = l.fid WHERE l.fid IN {sql_int_list(part)}",
                    )
                    exec_sql(self.ds, f"UPDATE {t} SET {sets} WHERE fid IN {sql_int_list(part)}")  # nosec B608
                    for table in (ddl.STAGED, ddl.PARKED):
                        exec_sql(self.ds, f"DELETE FROM {table} WHERE fid IN {sql_int_list(part)}")  # nosec B608
        if new:
            with transaction(self.ds):
                for fid in new:
                    gid = minted[fid][0] if fid in minted else None
                    self._discard_row(fid, gid, reason)
        # Our own creates that were sent unconfirmed and then deleted: forget
        # them (if a create did land, the pull brings the server copy back).
        new_set = set(changes.new_fids)
        self.drop_minted(fid for fid in minted if fid not in new_set)
        return restored, len(changed), len(new)

    def run_pending_discards(self) -> int:
        """Apply discards that waited for the layer's QGIS edits to be saved."""
        pending = [(fid, r) for fid, r in self.parked().items() if r["kind"] == "discard_pending"]
        if not pending:
            return 0
        with transaction(self.ds):
            for fid, r in pending:
                gid = int(r["gid_s"]) if r.get("gid_s") else None
                self._discard_row(fid, gid, r.get("detail") or "Deleted on the server")
        return len(pending)

    # ------------------------------------------------------------------ pull
    def apply_pull_page(
        self,
        rows: Sequence[Mapping],
        attrs_by_gid: Mapping[int, Mapping[str, Optional[str]]],
        wkb_by_gid: Mapping[int, Optional[bytes]],
        *,
        seen: Optional[Set[int]] = None,
    ) -> PageOutcome:
        """Apply one ``feat-list`` page (the caller already fetched attributes
        and normalised geometry to Multi*, 2D WKB). Last write wins: a row the
        user changed locally keeps its local content (it is pushed next tick)."""
        out = PageOutcome()
        if not rows:
            return out
        synced = self.synced()
        t = _q(self.table)
        gids = [int(r["id"]) for r in rows]
        if seen is not None:
            seen.update(g for g, r in zip(gids, rows) if not r.get("deleted"))

        base: Dict[int, dict] = {}
        sel = "".join(f", b.{_q(c)} AS {_q('c_' + str(i))}" for i, (_, c) in enumerate(synced))
        for part in _chunks(gids):
            for r in query(
                self.ds,
                f"SELECT CAST(b.fid AS INTEGER) AS bfid, CAST(b.gid AS TEXT) AS gid_s, hex(b.g) AS g_hex{sel} "  # nosec B608
                f"FROM {ddl.BASE} b WHERE b.gid IN {sql_int_list(part)}",
            ):
                base[int(r["gid_s"])] = r
        minted = {gid: fid for fid, (gid, _) in self.minted().items() if gid in set(gids)}
        local_fids = [int(b["bfid"]) for b in base.values()] + list(minted.values())
        present: Dict[int, dict] = {}
        for part in _chunks(local_fids):
            for r in query(
                self.ds,
                f"SELECT CAST(fid AS INTEGER) AS lfid, CAST(gd_id AS TEXT) AS gd_s, "  # nosec B608
                f"CAST(gd_ans_id AS TEXT) AS ga_s FROM {t} WHERE fid IN {sql_int_list(part)}",
            ):
                present[int(r["lfid"])] = r
        modified = self._modified_among(int(b["bfid"]) for b in base.values())

        lyr = self.ds.GetLayerByName(self.table)
        rebase: Dict[int, int] = {}
        confirm: List[int] = []
        edited = {
            int(r["id"]): str(r["edited_on"]) for r in rows if r.get("edited_on") not in (None, "")
        }  # gid → the server's edited_on, as sent
        same: Dict[int, str] = {}  # fid → edited_on, for rows already matching base
        with transaction(self.ds):
            for row in rows:
                gid = int(row["id"])
                deleted = bool(row.get("deleted"))
                ans_id = row.get("ans_id")
                ans_id = int(ans_id) if ans_id not in (None, "") else None
                if gid in base:
                    fid = int(base[gid]["bfid"])
                    if deleted:
                        if fid in present:
                            if fid in modified:
                                self._discard_row(fid, gid, "Deleted on the server")
                                out.discarded += 1
                                continue
                            exec_sql(self.ds, f"DELETE FROM {t} WHERE fid = {fid}")  # nosec B608
                            out.deleted += 1
                        for table in (ddl.BASE, ddl.STAGED, ddl.PARKED):
                            exec_sql(self.ds, f"DELETE FROM {table} WHERE fid = {fid}")  # nosec B608
                        continue
                    if fid not in present:
                        out.kept_local += 1  # deleted locally: our delete goes out
                        continue
                    if fid in modified:
                        out.kept_local += 1
                        out.ids_written += self._set_ids(fid, present[fid], gid, ans_id)
                        continue
                    wkb = wkb_by_gid.get(gid)
                    attrs = attrs_by_gid.get(gid, {})
                    if self._same_as_base(base[gid], wkb, attrs, synced):
                        out.ids_written += self._set_ids(fid, present[fid], gid, ans_id)
                        if gid in edited:
                            same[fid] = edited[gid]
                        continue
                    self._write_feature(lyr, fid, gid, ans_id, wkb, attrs, synced)
                    rebase[fid] = gid
                    out.updated += 1
                elif gid in minted:
                    fid = minted[gid]
                    if deleted:
                        if fid in present:
                            self._discard_row(fid, gid, "Deleted on the server")
                            out.discarded += 1
                        exec_sql(self.ds, f"DELETE FROM {ddl.MINTED} WHERE fid = {fid}")  # nosec B608
                        continue
                    confirm.append(fid)  # our create, whose ok response was lost
                    if fid in present:
                        out.ids_written += self._set_ids(fid, present[fid], gid, ans_id)
                else:
                    if deleted:
                        continue
                    fid = self._write_feature(
                        lyr, None, gid, ans_id, wkb_by_gid.get(gid), attrs_by_gid.get(gid, {}), synced
                    )
                    rebase[fid] = gid
                    out.inserted += 1
            if confirm:
                staged = {
                    int(r["sfid"])
                    for r in query(
                        self.ds,
                        f"SELECT CAST(fid AS INTEGER) AS sfid FROM {ddl.STAGED} WHERE fid IN {sql_int_list(confirm)}",  # nosec B608
                    )
                }
                if staged:
                    self._base_from_staged(sorted(staged))
                # No snapshot (shouldn't happen): the row as it stands becomes the base.
                gid_of = {fid: gid for gid, fid in minted.items()}
                self._base_from_layer({fid: gid_of[fid] for fid in confirm if fid not in staged and fid in present})
                for table in (ddl.MINTED, ddl.STAGED):
                    exec_sql(self.ds, f"DELETE FROM {table} WHERE fid IN {sql_int_list(confirm)}")  # nosec B608
            self._base_from_layer(rebase, edited)
            self._set_edited(same)
        return out

    def _set_ids(self, fid: int, present_row: Mapping, gid: int, ans_id: Optional[int]) -> int:
        """Refresh the display ids; 1 if the row changed, else 0."""
        gd = present_row.get("gd_s")
        ga = present_row.get("ga_s")
        want_ga = str(ans_id) if ans_id is not None else None
        if gd == str(gid) and ga == want_ga:
            return 0
        exec_sql(
            self.ds,
            f"UPDATE {_q(self.table)} SET gd_id = {int(gid)}, "  # nosec B608
            f"gd_ans_id = {int(ans_id) if ans_id is not None else 'NULL'} WHERE fid = {int(fid)}",
        )
        return 1

    @staticmethod
    def _same_as_base(base_row: Mapping, wkb: Optional[bytes], attrs: Mapping, synced) -> bool:
        base_wkb = None
        if base_row.get("g_hex"):
            try:
                base_wkb = gpkg_blob_to_wkb(bytes.fromhex(base_row["g_hex"]))
            except (WkbError, ValueError):
                return False
        if base_wkb != wkb:
            return False
        for i, (key, _) in enumerate(synced):
            if _norm(base_row.get(f"c_{i}")) != _norm(attrs.get(key)):
                return False
        return True

    def _write_feature(self, lyr, fid, gid, ans_id, wkb, attrs, synced) -> int:
        if fid is None:
            feat = ogr.Feature(lyr.GetLayerDefn())
        else:
            feat = lyr.GetFeature(int(fid))
            if feat is None:
                raise StoreError(f"feature {fid} vanished from {self.table}")
        geom = ogr.CreateGeometryFromWkb(wkb) if wkb else None
        feat.SetGeometry(geom)
        defn = lyr.GetLayerDefn()
        feat.SetFieldInteger64(defn.GetFieldIndex("gd_id"), int(gid))
        ans_idx = defn.GetFieldIndex("gd_ans_id")
        if ans_id is None:
            feat.SetFieldNull(ans_idx)
        else:
            feat.SetFieldInteger64(ans_idx, int(ans_id))
        for key, col in synced:
            idx = defn.GetFieldIndex(col)
            if idx < 0:
                continue
            value = attrs.get(key)
            if value is None or value == "":
                feat.SetFieldNull(idx)
            else:
                feat.SetField(idx, str(value))
        if fid is None:
            check_ogr(lyr.CreateFeature(feat), f"insert into {self.table}")
            return int(feat.GetFID())
        check_ogr(lyr.SetFeature(feat), f"update {self.table}")
        return int(fid)

    def finish_pull(self, watermark_ms: int, drained: Sequence[int]) -> None:
        with transaction(self.ds):
            self.set_meta({"watermark_ms": str(int(watermark_ms)), "drained_geoms": sorted({int(g) for g in drained})})

    # ----------------------------------------------------------- housekeeping
    def prune_outside(self, fence) -> int:
        """Drop unmodified, server-known rows that no longer intersect any
        assigned polygon (area unassigned / reshaped). ``fence`` is an OGR
        geometry (the union of assigned polygons); ``None`` prunes everything
        unmodified — the user has no assigned area left."""
        lyr = self.ds.GetLayerByName(self.table)
        inside: Set[int] = set()
        if fence is not None:
            defn = lyr.GetLayerDefn()
            lyr.SetIgnoredFields([defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())])
            lyr.SetSpatialFilter(fence)
            lyr.ResetReading()
            feat = lyr.GetNextFeature()
            while feat is not None:
                inside.add(int(feat.GetFID()))
                feat = lyr.GetNextFeature()
            lyr.SetSpatialFilter(None)
            lyr.SetIgnoredFields([])
        return self._drop_known_unmodified(lambda fid, gid: fid not in inside)

    def ghost_cleanup(self, seen_gids: Set[int]) -> int:
        """After a full re-sync: drop unmodified rows the server no longer
        returns (``clear-features`` wipes, features moved out of the area)."""
        return self._drop_known_unmodified(lambda fid, gid: gid not in seen_gids)

    def _drop_known_unmodified(self, predicate: Callable[[int, int], bool]) -> int:
        t = _q(self.table)
        known = [
            (int(r["lfid"]), int(r["gid_s"]))
            for r in query(
                self.ds,
                f"SELECT CAST(l.fid AS INTEGER) AS lfid, CAST(b.gid AS TEXT) AS gid_s "  # nosec B608
                f"FROM {t} l JOIN {ddl.BASE} b ON b.fid = l.fid",
            )
        ]
        modified = self._modified_among(f for f, _ in known)
        victims = [fid for fid, gid in known if fid not in modified and predicate(fid, gid)]
        if victims:
            with transaction(self.ds):
                for part in _chunks(victims):
                    exec_sql(self.ds, f"DELETE FROM {t} WHERE fid IN {sql_int_list(part)}")  # nosec B608
                    for table in (ddl.BASE, ddl.STAGED, ddl.PARKED):
                        exec_sql(self.ds, f"DELETE FROM {table} WHERE fid IN {sql_int_list(part)}")  # nosec B608
        return len(victims)

    def discarded_count(self) -> int:
        rows = query(self.ds, f"SELECT COUNT(*) AS n FROM {_q(self.discarded_table)}")  # nosec B608
        return int(rows[0]["n"]) if rows else 0


def _opt_int_text(value: Optional[int]) -> str:
    return "" if value is None else str(int(value))
