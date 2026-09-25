"""Project list: what the plugin can open, and what the user may do there.

``GET projects/desktop/list`` returns the caller's map-based projects where
they are Owner, Admin or Editor — the only roles that can use the plugin.
Each row carries the owner-first ``role`` (int), a fresh ``is_expired`` (the
owner's plan, same predicate as the server's 402 gate) and the Map row of the
project's Page access (``web_access``):

* ``can_view``   — Map read. Off: listed only so the picker can say why it's hidden.
* ``can_edit``   — Map write: add and edit features.
* ``can_delete`` — Map delete: delete features.

and whether a survey-area polygon is assigned to the caller
(``has_survey_area`` / ``has_assigned_area``). QGIS syncs only the assigned
polygons — owners included — so a project without one is listed but can't be
opened. A server that predates the two flags sends neither: nothing is blocked.

The server answers 403 when the user holds none of the three roles anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional

ROLE_OWNER = 1
ROLE_ADMIN = 2
ROLE_EDITOR = 3
ROLE_CLIENT = 4
ROLE_SUPERVISOR = 6
ROLE_SURVEYOR = 7

ROLE_LABELS = {
    ROLE_OWNER: "Owner",
    ROLE_ADMIN: "Admin",
    ROLE_EDITOR: "Editor",
    ROLE_CLIENT: "Client",
    ROLE_SUPERVISOR: "Supervisor",
    ROLE_SURVEYOR: "Surveyor",
}
DESKTOP_ROLES = frozenset({ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR})
_LABEL_TO_ROLE = {label.casefold(): role for role, label in ROLE_LABELS.items()}

AREA_NO_SURVEY_AREA = "no_survey_area"
AREA_NOT_ASSIGNED = "not_assigned"
AREA_LABELS = {
    AREA_NO_SURVEY_AREA: "No survey area in this project yet",
    AREA_NOT_ASSIGNED: "No survey area assigned to you",
}


@dataclass(frozen=True)
class ProjectInfo:
    id: int
    name: str
    role: int
    is_expired: bool = False
    can_view: bool = True
    can_edit: bool = True
    can_delete: bool = True
    owner_name: str = ""
    # ``None``: the server didn't say (it predates the flags) — not a blocker.
    has_survey_area: Optional[bool] = None
    has_assigned_area: Optional[bool] = None

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, str(self.role))

    @property
    def is_owner(self) -> bool:
        return self.role == ROLE_OWNER

    @property
    def visible(self) -> bool:
        """Shown in the picker: Map access and a live plan."""
        return self.can_view and not self.is_expired

    @property
    def area_blocker(self) -> Optional[str]:
        """Why the project can't be opened for lack of a survey area, else ``None``."""
        if self.has_survey_area is False:
            return AREA_NO_SURVEY_AREA
        if self.has_assigned_area is False:
            return AREA_NOT_ASSIGNED
        return None

    @property
    def area_label(self) -> str:
        return AREA_LABELS.get(self.area_blocker or "", "")

    @property
    def can_open(self) -> bool:
        """Shown and openable: visible, and a survey-area polygon is assigned to the user."""
        return self.visible and self.area_blocker is None

    @property
    def access_label(self) -> str:
        if not self.can_view:
            return "No Map access"
        if not self.can_edit:
            return "View only"
        if not self.can_delete:
            return "Can edit · no delete"
        return "Full access"

    def same_access(self, other: ProjectInfo) -> bool:
        return (self.can_view, self.can_edit, self.can_delete) == (other.can_view, other.can_edit, other.can_delete)


@dataclass(frozen=True)
class HiddenCounts:
    expired: int = 0
    no_access: int = 0

    @property
    def total(self) -> int:
        return self.expired + self.no_access


def parse_role(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return _LABEL_TO_ROLE.get(text.casefold())


def _flag(item: Mapping, key: str, default: bool) -> bool:
    value = item.get(key, default)
    return default if value is None else bool(value)


def _opt_flag(item: Mapping, key: str) -> Optional[bool]:
    value = item.get(key)
    return None if value is None else bool(value)


def parse_desktop_projects(items: Iterable[Mapping]) -> List[ProjectInfo]:
    """Rows of ``projects/desktop/list``; malformed rows and roles the plugin
    doesn't serve are skipped (the server already filters — belt and braces)."""
    out: List[ProjectInfo] = []
    seen = set()
    for item in items or ():
        if not isinstance(item, Mapping):
            continue
        try:
            pid = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        role = parse_role(item.get("role"))
        if pid in seen or role not in DESKTOP_ROLES:
            continue
        seen.add(pid)
        out.append(
            ProjectInfo(
                id=pid,
                name=str(item.get("name") or f"Project {pid}"),
                role=role,
                is_expired=_flag(item, "is_expired", False),
                can_view=_flag(item, "can_view", True),
                can_edit=_flag(item, "can_edit", True),
                can_delete=_flag(item, "can_delete", True),
                owner_name=str(item.get("owner_name") or ""),
                has_survey_area=_opt_flag(item, "has_survey_area"),
                has_assigned_area=_opt_flag(item, "has_assigned_area"),
            )
        )
    # Projects that can't be opened for lack of an assigned area go last.
    out.sort(key=lambda p: (p.area_blocker is not None, p.name.casefold(), p.id))
    return out


def visible_projects(projects: Iterable[ProjectInfo]) -> List[ProjectInfo]:
    return [p for p in projects if p.visible]


def hidden_counts(projects: Iterable[ProjectInfo]) -> HiddenCounts:
    expired = no_access = 0
    for p in projects:
        if not p.can_view:
            no_access += 1
        elif p.is_expired:
            expired += 1
    return HiddenCounts(expired=expired, no_access=no_access)
