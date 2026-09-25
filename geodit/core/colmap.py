"""Server attribute key → local column name.

Keys come from the layer's ``attr_head`` (≤ 15 chars, free text). A column
name must not collide with the plugin's own columns and must be unique
case-insensitively (SQLite identifiers are). The mapping is persisted per
layer and only ever extended, so a column never silently changes meaning.
"""

from __future__ import annotations

from typing import Dict, Iterable

# The feature table's own columns, plus those of the bookkeeping tables that
# mirror the synced columns (``base``: fid, gid, g, gd_edited; ``staged``: fid,
# gid, op, g) — a key such as "G" or "gid" would otherwise be a duplicate
# column there and fail the layer's creation (and with it every sync).
RESERVED = frozenset(
    {"fid", "geom", "gd_id", "gd_ans_id", "ogc_fid", "geometry", "rowid", "oid", "gid", "g", "op", "gd_edited"}
)


def _sanitize(key: str) -> str:
    cleaned = "".join(ch if ch.isprintable() and ch != '"' else "_" for ch in str(key)).strip()
    return cleaned or "field"


def extend_colmap(existing: Dict[str, str], keys: Iterable[str]) -> Dict[str, str]:
    result = dict(existing)
    used = {col.casefold() for col in result.values()}
    for key in keys:
        if key in result:
            continue
        base = _sanitize(key)
        candidate = base
        if candidate.casefold() in RESERVED or candidate.casefold() in used:
            candidate = f"a_{base}"
            n = 2
            while candidate.casefold() in RESERVED or candidate.casefold() in used:
                candidate = f"a_{base}_{n}"
                n += 1
        used.add(candidate.casefold())
        result[key] = candidate
    return result


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'
