"""Parsing of the server payloads the sync engine consumes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class LayerInfo:
    id: int
    name: str
    g_type: Optional[int]  # 1 point, 2 line, 3 polygon; None while ingesting
    attr_keys: Tuple[str, ...]
    is_active: bool = True
    locked: bool = False
    form_id: Optional[int] = None
    position: int = 0


@dataclass(frozen=True)
class SurveyAreaInfo:
    id: int
    attr_keys: Tuple[str, ...]


def _attr_keys(attr_head) -> Tuple[str, ...]:
    if isinstance(attr_head, Mapping):
        return tuple(str(k) for k in attr_head.keys())
    return ()


def parse_shp_list(payload: Mapping) -> Tuple[List[LayerInfo], Optional[SurveyAreaInfo]]:
    layers: List[LayerInfo] = []
    for item in payload.get("shp") or []:
        try:
            layer_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        g_type = item.get("g_type")
        layers.append(
            LayerInfo(
                id=layer_id,
                name=str(item.get("name") or f"Layer {layer_id}"),
                g_type=int(g_type) if g_type not in (None, "") else None,
                attr_keys=_attr_keys(item.get("attr_head")),
                is_active=bool(item.get("is_active", True)),
                locked=bool(item.get("locked", False)),
                form_id=item.get("form_id"),
                position=int(item.get("position") or 0),
            )
        )
    layers.sort(key=lambda layer: (layer.position, layer.id))
    sa_raw = payload.get("survey_area") or {}
    survey_area = None
    if isinstance(sa_raw, Mapping) and sa_raw.get("id") is not None:
        survey_area = SurveyAreaInfo(id=int(sa_raw["id"]), attr_keys=_attr_keys(sa_raw.get("attr_head")))
    return layers, survey_area


def cursor_from_next(next_url: Optional[str]) -> Optional[str]:
    """The ``cursor`` query arg of a page's ``next`` link.

    ``next`` is an absolute URL built from the server's own view of the host,
    which can differ from the base URL the plugin talks to (proxies, ALB), so
    only the cursor token is reused.
    """
    if not next_url:
        return None
    values = parse_qs(urlsplit(next_url).query).get("cursor")
    return values[0] if values else None


def fold_attrs(rows) -> Dict[int, Dict[str, str]]:
    """``[{id, feat_id, key, value}]`` → ``{feat_id: {key: value}}``.

    There is no UNIQUE on (feat_id, key) server-side, so duplicate cells can
    exist; the newest row (highest attr id) wins, deterministically.
    """
    best: Dict[Tuple[int, str], Tuple[int, str]] = {}
    for row in rows or []:
        try:
            feat_id = int(row["feat_id"])
            key = str(row["key"])
            attr_id = int(row.get("id") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        value = row.get("value")
        prev = best.get((feat_id, key))
        if prev is None or attr_id >= prev[0]:
            best[(feat_id, key)] = (attr_id, value if value is not None else "")
    out: Dict[int, Dict[str, str]] = {}
    for (feat_id, key), (_, value) in best.items():
        out.setdefault(feat_id, {})[key] = value
    return out
