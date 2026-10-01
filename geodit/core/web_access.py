"""Settings › Web access, resolved as the web resolves it — for a server whose
``projects/desktop/list`` rows don't carry the answer-modal caps (``data``)
yet, so the plugin reads the project's ``survey_settings.web_access`` itself.

A port of geodit-ui ``settings/web-access.ts`` (``parseWebAccess``,
``allows``, ``capability``, ``viewCapability``) and its ``roles.ts`` fallbacks,
for the two screens the plugin is: Map (the layers) and Data (the feature form,
whose permissions are the Data screen's, as on the web map). Pure Python, no
QGIS, so it is unit-tested against the web's rules:

- the owner is never governed: everything;
- an admin and an editor follow their own role's block, every key missing from
  it falling back to that role's seed;
- a project without a block falls back to the static role gates: Map edit and
  delete for admins only (``canEditMapLayers``), Data edit for both
  (``canEditData``), the three edit switches on, the show-hidden switches as
  seeded (admin on, editor off).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, FrozenSet, Mapping, Optional, Tuple

from .projects import ROLE_ADMIN, ROLE_EDITOR, ROLE_OWNER, DataCaps

PAGE_KEYS = ("overview", "logs", "settings", "team", "map", "form", "data", "analytics", "report")
ALWAYS_ALLOWED = frozenset({"overview"})  # the screen every governed role keeps
FLAGS = ("edit_read_only", "duplicate_for_page", "remove_for_page", "show_hidden_pages", "show_hidden_questions")


@dataclass(frozen=True)
class MapCaps:
    can_view: bool = True
    can_edit: bool = True
    can_delete: bool = True


FULL_MAP = MapCaps()
FULL_DATA = DataCaps(
    can_view=True,
    can_edit=True,
    edit_read_only=True,
    duplicate_entry=True,
    remove_entry=True,
    show_hidden_pages=True,
    show_hidden_questions=True,
)


@dataclass(frozen=True)
class RoleConfig:
    pages: FrozenSet[str]
    writes: FrozenSet[str]
    deletes: FrozenSet[str]
    flags: Tuple[Tuple[str, bool], ...]

    def flag(self, name: str) -> bool:
        return dict(self.flags)[name]


def _seed(role: str) -> RoleConfig:
    """``seededConfig``: what a role starts with on every screen the plugin is.
    Admins read every screen and write Map, Data and Report; editors open
    Overview, Map and Data and write Map and Data; neither deletes. Every
    answer-modal switch starts on for an admin, off for an editor."""
    pages = frozenset(PAGE_KEYS) if role == "admin" else frozenset({"overview", "map", "data"})
    writes = frozenset({"map", "data", "report"}) if role == "admin" else frozenset({"map", "data"})
    on = role == "admin"
    return RoleConfig(pages, writes, frozenset(), tuple((name, on) for name in FLAGS))


SEED = {"admin": _seed("admin"), "editor": _seed("editor")}


def _obj(raw: Any) -> Optional[Mapping]:
    """``toObj``: an object, or a JSON string holding one; anything else is no blob."""
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _keys(raw: Any, fallback: FrozenSet[str]) -> FrozenSet[str]:
    """``parseKeys``: the known page keys of a list; an absent list is the seed."""
    if not isinstance(raw, list):
        return fallback
    return frozenset(key for key in raw if isinstance(key, str) and key in PAGE_KEYS)


def role_config(blob: Mapping, role: str) -> RoleConfig:
    """``parseRoleConfig`` for an admin or an editor (who are offered read,
    write and delete on Map and Data, and every switch): ``deletes ⊆ writes ⊆
    pages``, each key missing from the stored block taken from the seed."""
    seed = SEED[role]
    raw = _obj(blob.get(role))
    if raw is None:
        return seed
    pages = _keys(raw.get("pages"), seed.pages) | ALWAYS_ALLOWED
    writes = _keys(raw.get("writes"), seed.writes) & pages
    deletes = _keys(raw.get("deletes"), seed.deletes) & writes
    flags = tuple((name, raw[name] if isinstance(raw.get(name), bool) else seed.flag(name)) for name in FLAGS)
    return RoleConfig(pages, writes, deletes, flags)


def resolve(role: int, web_access: Any) -> Tuple[MapCaps, DataCaps]:
    """What ``role`` may do on the Map (the layers) and in the answer sheet,
    under the project's ``survey_settings.web_access`` (``None``: none set)."""
    if role == ROLE_OWNER:
        return FULL_MAP, FULL_DATA
    key = {ROLE_ADMIN: "admin", ROLE_EDITOR: "editor"}.get(role)
    if key is None:  # not a desktop role: nothing to edit
        return MapCaps(can_view=True, can_edit=False, can_delete=False), DataCaps()
    blob = _obj(web_access)
    if blob is None:
        # No policy: the static gates (``roles.ts``) — and ``capability()``'s
        # blanket yes for the edit switches, ``viewCapability()``'s seed for
        # the show-hidden ones.
        map_edit = role == ROLE_ADMIN
        seed = SEED[key]
        return (
            MapCaps(can_view=True, can_edit=map_edit, can_delete=map_edit),
            DataCaps(
                can_view=True,
                can_edit=True,
                edit_read_only=True,
                duplicate_entry=True,
                remove_entry=True,
                show_hidden_pages=seed.flag("show_hidden_pages"),
                show_hidden_questions=seed.flag("show_hidden_questions"),
            ),
        )
    config = role_config(blob, key)
    return (
        MapCaps(
            can_view="map" in config.pages,
            can_edit="map" in config.writes,
            can_delete="map" in config.deletes,
        ),
        DataCaps(
            can_view="data" in config.pages,
            can_edit="data" in config.writes,
            edit_read_only=config.flag("edit_read_only"),
            duplicate_entry=config.flag("duplicate_for_page"),
            remove_entry=config.flag("remove_for_page"),
            show_hidden_pages=config.flag("show_hidden_pages"),
            show_hidden_questions=config.flag("show_hidden_questions"),
        ),
    )
