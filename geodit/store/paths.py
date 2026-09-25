"""Where local data lives, and the per-project sync lock.

``<data root>/geodit/<server key>/<user id>/<project id>/`` holds
``project.gpkg`` and one ``layer_<shp_id>.gpkg`` per synced layer. The data root
defaults to Qt's AppLocalDataLocation (never the roaming profile, never a
network share — SQLite file locking needs a local filesystem).
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Optional
from urllib.parse import urlsplit


def server_key(base_url: str) -> str:
    parts = urlsplit(base_url.strip())
    host = (parts.hostname or "server").lower()
    port = f"_{parts.port}" if parts.port else ""
    key = base_url.strip().rstrip("/").lower().encode("utf-8")
    digest = hashlib.sha1(key, usedforsecurity=False).hexdigest()[:8]
    return re.sub(r"[^a-z0-9._-]+", "_", f"{host}{port}") + f"_{digest}"


def project_dir(root: str, base_url: str, user_id: int, project_id: int) -> str:
    return os.path.join(root, "geodit", server_key(base_url), str(int(user_id)), str(int(project_id)))


def project_gpkg(folder: str) -> str:
    return os.path.join(folder, "project.gpkg")


def layer_gpkg(folder: str, shp_id: int) -> str:
    return os.path.join(folder, f"layer_{int(shp_id)}.gpkg")


class ProjectLock:
    """Keeps two QGIS instances from syncing one project store at once.

    Uses ``QLockFile`` (stale-lock detection across platforms) when Qt is
    importable; a plain ``O_EXCL`` file otherwise (unit tests)."""

    def __init__(self, folder: str) -> None:
        self.path = os.path.join(folder, "sync.lock")
        self._qt_lock = None
        self._fd: Optional[int] = None

    def try_lock(self) -> bool:
        try:
            from qgis.PyQt.QtCore import QLockFile
        except ImportError:  # pragma: no cover - exercised only without Qt
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return True
            except FileExistsError:
                return False
        lock = QLockFile(self.path)
        lock.setStaleLockTime(10 * 60 * 1000)
        if lock.tryLock(0):
            self._qt_lock = lock
            return True
        return False

    def unlock(self) -> None:
        if self._qt_lock is not None:
            self._qt_lock.unlock()
            self._qt_lock = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            try:
                os.remove(self.path)
            except OSError:
                pass
