"""Plugin settings (``QgsSettings``, prefix ``geodit/``) and secret storage.

Nothing secret lives in QgsSettings (a plain ini file). The refresh token —
only when the user ticks "Stay signed in" — goes into QGIS's encrypted auth
database via ``QgsAuthManager``, which may ask for the QGIS master password.
It is never touched while the plugin loads, only on explicit user actions.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from dataclasses import dataclass
from typing import Optional

from qgis.core import QgsApplication, QgsSettings
from qgis.PyQt.QtCore import QStandardPaths

from .store.paths import server_key

SERVER_URL = "https://prod.api.geodit.in/api/v2/"
# Points the plugin at another Geodit API (development, tests). There is no
# server choice in the UI; set this in the environment, e.g. QGIS Settings →
# Options → System → Environment.
SERVER_URL_ENV = "GEODIT_SERVER_URL"
# Downloads (web and Android edits) every minute; an idle tick is a handful of
# cheap, unthrottled map calls. Uploads also start a few seconds after a save.
DEFAULT_INTERVAL_MIN = 1
_PREFIX = "geodit/"


@dataclass
class RememberedUser:
    user_id: int
    display_name: str
    project_id: Optional[int]
    project_name: str


class Config:
    def __init__(self) -> None:
        self._s = QgsSettings()

    def _get(self, key: str, default=None, type_=None):
        if type_ is None:
            return self._s.value(_PREFIX + key, default)
        return self._s.value(_PREFIX + key, default, type=type_)

    def _set(self, key: str, value) -> None:
        self._s.setValue(_PREFIX + key, value)

    # ---------------------------------------------------------------- server
    @property
    def base_url(self) -> str:
        """The Geodit API. Server choices saved by versions before 0.3.2
        (``server/preset``, ``server/custom_url``) are ignored."""
        url = os.environ.get(SERVER_URL_ENV, "").strip()
        if not url:
            return SERVER_URL
        return url if url.endswith("/") else url + "/"

    # ------------------------------------------------------------------ sync
    @property
    def interval_min(self) -> int:
        try:
            return max(1, min(60, int(self._get("sync/interval_min", DEFAULT_INTERVAL_MIN))))
        except (TypeError, ValueError):
            return DEFAULT_INTERVAL_MIN

    @interval_min.setter
    def interval_min(self, value: int) -> None:
        self._set("sync/interval_min", max(1, min(60, int(value))))

    @property
    def auto_sync(self) -> bool:
        return bool(self._get("sync/auto", True, bool))

    @auto_sync.setter
    def auto_sync(self, value: bool) -> None:
        self._set("sync/auto", bool(value))

    @property
    def data_root(self) -> str:
        custom = str(self._get("data/root", "") or "")
        if custom:
            return custom
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        return base or os.path.join(QgsApplication.qgisSettingsDirPath(), "geodit_data")

    @data_root.setter
    def data_root(self, value: str) -> None:
        self._set("data/root", value or "")

    # --------------------------------------------------------- per install
    @property
    def install_uuid(self) -> str:
        value = str(self._get("install_uuid", "") or "")
        if not value:
            value = uuid.uuid4().hex
            self._set("install_uuid", value)
        return value

    def worker_id(self, base_url: str, user_id: int) -> Optional[int]:
        value = self._get(f"worker/{server_key(base_url)}/{int(user_id)}", None)
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def set_worker_id(self, base_url: str, user_id: int, worker_id: int) -> None:
        self._set(f"worker/{server_key(base_url)}/{int(user_id)}", int(worker_id))

    # ------------------------------------------------------------ remembered
    def remembered(self, base_url: str) -> Optional[RememberedUser]:
        key = f"session/{server_key(base_url)}/"
        uid = self._get(key + "user_id", None)
        if uid in (None, ""):
            return None
        project = self._get(key + "project_id", None)
        return RememberedUser(
            user_id=int(uid),
            display_name=str(self._get(key + "display_name", "")),
            project_id=int(project) if project not in (None, "") else None,
            project_name=str(self._get(key + "project_name", "")),
        )

    def remember(self, base_url: str, user_id: int, display_name: str) -> None:
        key = f"session/{server_key(base_url)}/"
        self._set(key + "user_id", int(user_id))
        self._set(key + "display_name", display_name)

    def remember_project(self, base_url: str, project_id: Optional[int], name: str = "") -> None:
        key = f"session/{server_key(base_url)}/"
        self._set(key + "project_id", "" if project_id is None else int(project_id))
        self._set(key + "project_name", name)

    def forget(self, base_url: str) -> None:
        self._s.remove(_PREFIX + f"session/{server_key(base_url)}")


class SecretStore:
    """Refresh-token persistence in QGIS's encrypted auth database.
    Main thread only — it can show the master-password dialog."""

    @staticmethod
    def _key(base_url: str) -> str:
        return f"geodit/{server_key(base_url)}/refresh"

    def save(self, base_url: str, refresh: str) -> bool:
        am = QgsApplication.authManager()
        try:
            return bool(am.storeAuthSetting(self._key(base_url), refresh, True))
        except Exception:  # noqa: BLE001 - auth DB unavailable / password refused
            return False

    def load(self, base_url: str) -> Optional[str]:
        am = QgsApplication.authManager()
        try:
            if not am.existsAuthSetting(self._key(base_url)):
                return None
            value = am.authSetting(self._key(base_url), None, True)
        except Exception:  # noqa: BLE001
            return None
        return str(value) if value else None

    def delete(self, base_url: str) -> None:
        am = QgsApplication.authManager()
        with contextlib.suppress(Exception):  # auth DB unavailable / password refused
            if am.existsAuthSetting(self._key(base_url)):
                am.removeAuthSetting(self._key(base_url))
