"""Geodit plugin controller (main thread).

Owns the session (server, tokens, user, worker id), the active project, the
sync scheduler and the background tasks; the dock is its view and
``LayerManager`` its hands in the QGIS project.

Who can use it: project Owners, Admins and Editors. The server refuses a
desktop sign-in for anyone else, and ``projects/desktop/list`` answers 403 for
a remembered session whose user lost those roles — the plugin then revokes the
session and signs out. The list also carries each project's Page access (Map
row): no Map write → the layers are read-only and nothing is uploaded; no Map
delete → locally deleted features are restored by the sync engine.

Only a project with a survey-area polygon assigned to the user can be opened
(QGIS syncs nothing else). One that loses its assignment while open stays
open: its layers turn read-only and only changes already made are uploaded.

Sync scheduling: a periodic timer (default 1 min), plus a sync 3 s after the
user saves a synced layer, plus "Sync now". One sync at a time; a request that
arrives mid-sync is queued for its project and runs right after (a timer tick
isn't — the running sync already downloads). Failures back off (30 s … 30 min)
for the timer, never for the user's own actions; a 429 honours
``Retry-After``; an expired plan (402) pauses uploads for an hour while
downloads continue; a dead session stops syncing until the user signs in again
(a stored password is never replayed). Permissions are re-read hourly, after
the server refuses a change, and on every manual refresh — never per tick.
"""

from __future__ import annotations

import functools
import os
import time
import traceback
from dataclasses import dataclass, replace
from typing import List, Optional, Set

from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsProject
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QPushButton

from .config import Config, SecretStore
from .core.clock import ServerClock
from .core.policy import PLAN_EXPIRED_RETRY_S, backoff_delay
from .core.projects import AREA_NOT_ASSIGNED, ProjectInfo, hidden_counts, parse_desktop_projects, visible_projects
from .net.errors import ApiError, NotFound, PermissionDenied, ServerTooOld, SessionExpired
from .net.tokens import TokenStore
from .qgis_ui.dock import PAGE_PROJECTS, GeoditDock, describe_time
from .qgis_ui.layers import LayerManager
from .store import paths
from .sync.context import LayerEvent, ModifiedLayers, SyncContext, SyncReport
from .sync.task import CallTask, SyncTask

SAVE_SYNC_DELAY_MS = 3_000
CAPS_REFRESH_S = 3600
QUEUED_SYNC_DELAY_MS = 1_500
SLOW_TICK_S = 2.0  # a tick at least this slow is logged even when it changed nothing
_ICON = os.path.join(os.path.dirname(__file__), "icons", "geodit.svg")

# A request that arrives while a sync runs waits for it; the strongest wins.
_PRIORITY = {"saved": 1, "permissions": 2, "open": 3, "manual": 4}
# The user's own actions don't wait out a failure backoff (it is for the timer).
_SKIP_BACKOFF = frozenset({"saved", "permissions", "open", "manual"})
# A 429 is the server asking to slow down: only an explicit sync or opening a
# project goes ahead anyway.
_SKIP_RATE_LIMIT = frozenset({"manual", "open"})
# Background ticks stay out of the way (no progress bar, not in the task bar).
_QUIET = frozenset({"timer", "saved"})


@dataclass
class QueuedSync:
    project_id: int
    reason: str
    full: bool = False
    discard: bool = False


class GeoditPlugin:
    def __init__(self, iface) -> None:
        self.iface = iface
        self.config = Config()
        self.secrets = SecretStore()
        self.modified = ModifiedLayers()
        self.layers = LayerManager(iface, self.modified, on_saved=self._on_layer_saved, message=self._message)
        self.dock: Optional[GeoditDock] = None
        self.action: Optional[QAction] = None

        self.base_url = self.config.base_url
        self.tokens = TokenStore()
        self.clock = ServerClock()
        self.user_id: Optional[int] = None
        self.display_name = ""
        self.worker_id: Optional[int] = None
        self.remember = False
        self._mfa_token: Optional[str] = None
        self.projects: List[ProjectInfo] = []  # every project the user may use here, incl. hidden ones
        self.project: Optional[ProjectInfo] = None

        self.timer = QTimer()
        self.timer.timeout.connect(lambda: self.request_sync("timer"))
        self.soon = QTimer()
        self.soon.setSingleShot(True)
        self.soon.timeout.connect(lambda: self.request_sync("saved"))

        self._sync_task: Optional[SyncTask] = None
        self._calls: Set[CallTask] = set()
        self._queued: Optional[QueuedSync] = None
        self._backoff_until = 0.0  # after failures: the timer waits
        self._rate_limit_until = 0.0  # after a 429
        self._push_paused_until = 0.0
        self._failures = 0
        self._area_blocked_seen = False  # the open project has no survey area assigned
        self._logged_warnings: Set[str] = set()
        self._last_caps_refresh = 0.0
        self._caps_call_running = False
        self._last_sync_at: Optional[float] = None
        self._last_report: Optional[SyncReport] = None
        self._session_dead = False
        self._shutting_down = False
        self._project_signals: List[tuple] = []

    # ================================================================ QGIS
    def initGui(self) -> None:
        icon = QIcon(_ICON)
        self.action = QAction(icon, "Geodit", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip("Geodit: sign in, open a map project and sync your survey area")
        self.dock = GeoditDock(self, self.iface.mainWindow())
        self.dock.setWindowIcon(icon)
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        self.dock.hide()
        self.action.toggled.connect(self.dock.setVisible)
        self.dock.visibilityChanged.connect(self.action.setChecked)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&Geodit", self.action)
        self._show_signed_out()
        project = QgsProject.instance()
        for signal, slot in (
            (project.aboutToBeCleared, self._on_project_clearing),
            (project.readProject, self._on_project_read),
            (QgsApplication.instance().aboutToQuit, self._shutdown),
        ):
            signal.connect(slot)
            self._project_signals.append((signal, slot))

    def unload(self) -> None:
        self._shutdown()
        for signal, slot in self._project_signals:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._project_signals.clear()
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("&Geodit", self.action)
            self.action.deleteLater()
            self.action = None
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

    def _shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.timer.stop()
        self.soon.stop()
        task = self._sync_task
        if task is not None:
            task.cancel()
            task.waitForFinished(3000)  # never an unbounded wait on exit
        self.layers.detach()

    # ============================================================ helpers
    def _message(self, text: str, level=Qgis.MessageLevel.Info, duration: int = 6) -> None:
        self.iface.messageBar().pushMessage("Geodit", text, level, duration)

    @staticmethod
    def _log(text: str, level=Qgis.MessageLevel.Info) -> None:
        QgsMessageLog.logMessage(text, "Geodit", level)

    def _run(self, description: str, fn, on_done) -> None:
        task = CallTask(description, self.base_url, self.tokens, self.clock, fn, None)

        def done(value, exc):
            self._calls.discard(task)
            if not self._shutting_down:
                on_done(value, exc)

        task.on_done = done
        self._calls.add(task)
        QgsApplication.taskManager().addTask(task)

    @staticmethod
    def _error_text(exc: BaseException) -> str:
        if isinstance(exc, ApiError):
            return exc.message
        return f"Unexpected error: {exc}"

    def _show_signed_out(self, error: str = "") -> None:
        if self.dock is None:
            return
        self.dock.show_sign_in(self.config.remembered(self.config.base_url), error)

    def _show_projects(self, error: str = "") -> None:
        if self.dock is not None:
            self.dock.show_projects(
                visible_projects(self.projects), self.display_name, error, hidden=hidden_counts(self.projects)
            )

    def _visible(self, project_id: Optional[int]) -> Optional[ProjectInfo]:
        return next((p for p in self.projects if p.id == project_id and p.visible), None)

    def sync_step_text(self) -> str:
        task = self._sync_task
        return task.step_text if task is not None else ""

    # ============================================================ sign in
    def sign_in(self, *, username, phone, country_code, password, remember) -> None:
        if not password or not (username or phone):
            self.dock.set_sign_in_error("Enter your username (or phone number) and password.")
            return
        self._new_session()
        self.remember = remember
        self.dock.set_busy(True)
        self._run(
            "Geodit sign in",
            lambda c: c.login(
                password=password,
                username=username,
                phone_number=phone,
                country_code=country_code,
                remember_me=remember,
            ),
            self._on_login,
        )

    def _new_session(self) -> None:
        self.base_url = self.config.base_url
        self.tokens = TokenStore()
        self.clock = ServerClock()
        self.user_id = None
        self.worker_id = None
        self._session_dead = False
        self._backoff_until = self._rate_limit_until = self._push_paused_until = 0.0
        self._failures = 0
        self._queued = None
        self._logged_warnings = set()
        self._last_caps_refresh = 0.0

    def _on_login(self, result, exc) -> None:
        if exc is not None:
            self.dock.set_busy(False)
            self.dock.set_sign_in_error(self._error_text(exc))
            return
        if result.requires_2fa:
            self._mfa_token = result.mfa_token
            self.dock.show_two_factor()
            return
        self._after_login()

    def verify_code(self, code: str) -> None:
        if not code:
            self.dock.set_code_error("Enter the code.")
            return
        if not self._mfa_token:
            self.cancel_two_factor()
            return
        self.dock.set_busy(True)
        token = self._mfa_token
        self._run("Geodit two-factor", lambda c: c.login_verify(mfa_token=token, code=code), self._on_verify)

    def _on_verify(self, result, exc) -> None:
        if exc is not None:
            self.dock.set_busy(False)
            if isinstance(exc, ApiError) and exc.field_message("mfa_token"):
                # The 5-minute challenge expired or was used: start over.
                self._mfa_token = None
                self._show_signed_out("The sign-in challenge expired. Please sign in again.")
            elif isinstance(exc, PermissionDenied):
                # Not an Owner/Admin/Editor anywhere: QGIS isn't for this account.
                self._mfa_token = None
                self._show_signed_out(self._error_text(exc))
            else:
                self.dock.set_code_error(self._error_text(exc))
            return
        self._mfa_token = None
        self._after_login()

    def cancel_two_factor(self) -> None:
        self._mfa_token = None
        self._show_signed_out()

    def continue_session(self) -> None:
        """Resume a "Stay signed in" session. Reading the stored token can show
        QGIS's master-password prompt, so it only happens on this click."""
        self._new_session()
        refresh = self.secrets.load(self.base_url)
        if not refresh:
            self.config.forget(self.base_url)
            self._show_signed_out("Please sign in again.")
            return
        self.tokens.set_tokens(None, refresh)
        self.remember = True
        self.dock.set_busy(True)
        self._after_login()

    def _after_login(self) -> None:
        """Project list first (it is also the role check), then the profile and
        the device worker id — off the main thread."""
        uid = self.tokens.user_id
        cached_worker = self.config.worker_id(self.base_url, uid) if uid is not None else None
        device_uuid = "qgis-" + self.config.install_uuid

        def work(c):
            try:
                projects = parse_desktop_projects(c.projects_desktop())
            except PermissionDenied:
                # Not an Owner/Admin/Editor anywhere: revoke the session we just made.
                c.logout()
                raise
            except NotFound:
                c.logout()
                raise ServerTooOld() from None
            profile = c.profile()
            worker = cached_worker or c.register_device(device_uuid)
            return profile, worker, projects

        self._run("Geodit: loading projects", work, self._on_session_ready)

    def _on_session_ready(self, value, exc) -> None:
        if exc is not None:
            if isinstance(exc, (SessionExpired, PermissionDenied, ServerTooOld)):
                # Dead, refused, or revoked just now by the job (403 / 404 from
                # the project list): keeping "Continue as …" would only lead back here.
                self.secrets.delete(self.base_url)
                self.config.forget(self.base_url)
            if isinstance(exc, PermissionDenied) and self.project is not None:
                self.close_project(show_list=False)
            self.tokens.clear()
            self._show_signed_out(self._error_text(exc))
            return
        profile, worker, projects = value
        self.user_id = self.tokens.user_id
        self.worker_id = int(worker)
        self.config.set_worker_id(self.base_url, self.user_id, self.worker_id)
        name = " ".join(filter(None, [profile.get("first_name"), profile.get("last_name")])).strip()
        self.display_name = name or profile.get("username") or f"user {self.user_id}"
        if self.remember and self.tokens.refresh_token:
            if self.secrets.save(self.base_url, self.tokens.refresh_token):
                self.config.remember(self.base_url, self.user_id, self.display_name)
            else:
                self._message(
                    "Couldn't store the sign-in securely; you'll need to sign in again next time.",
                    Qgis.MessageLevel.Warning,
                )
        elif not self.remember:
            self.secrets.delete(self.base_url)
            self.config.forget(self.base_url)
        self.projects = projects
        self._last_caps_refresh = time.time()
        if self.project is not None and self._session_dead:
            # Signed in again after the session died: carry on where we were.
            self._session_dead = False
            match = self._visible(self.project.id)
            if match is not None:
                self.open_project(match)
                return
            name = self.project.name
            self.close_project(show_list=False)
            self._message(f"{name} can no longer be synced from QGIS.", Qgis.MessageLevel.Warning, 0)
        self._session_dead = False
        self._show_projects()
        self._reopen_last_project()

    def _openable(self, project_id: Optional[int]) -> Optional[ProjectInfo]:
        return next((p for p in self.projects if p.id == project_id and p.can_open), None)

    @staticmethod
    def _why_not_openable(project: ProjectInfo) -> str:
        if not project.can_view:
            return "the Map page is turned off for your role"
        if project.is_expired:
            return "the owner's plan has expired"
        if project.area_blocker == AREA_NOT_ASSIGNED:
            return "no survey area is assigned to you"
        return "it has no survey area yet"

    def _reopen_last_project(self) -> None:
        remembered = self.config.remembered(self.base_url)
        last_id = remembered.project_id if remembered else None
        if last_id is None or not self.layers.geodit_layers_in_project():
            return
        match = self._openable(last_id)
        if match is not None:
            self.open_project(match)
            return
        listed = next((p for p in self.projects if p.id == last_id and p.visible), None)
        if listed is not None:
            self._message(
                f"{listed.name} can't be opened in QGIS now: {self._why_not_openable(listed)}.",
                Qgis.MessageLevel.Warning,
                0,
            )

    def refresh_projects(self) -> None:
        """Manual refresh from the project list."""
        self.dock.set_busy(True)

        def work(c):
            return parse_desktop_projects(c.projects_desktop())

        def done(projects, exc):
            if exc is not None:
                if isinstance(exc, PermissionDenied):
                    self._lost_desktop_access(exc)
                    return
                self._handle_call_error(exc)
                if not self._session_dead:
                    self._show_projects(self._error_text(exc))
                return
            self._last_caps_refresh = time.time()
            self.projects = projects
            self._show_projects()

        self._run("Geodit: refreshing projects", work, done)

    def _refresh_caps(self) -> None:
        """Re-read the project list quietly — role and Page-access changes —
        without leaving the page the user is on."""
        if self._caps_call_running or self.user_id is None or self._session_dead:
            return
        self._caps_call_running = True
        self._last_caps_refresh = time.time()

        def work(c):
            return parse_desktop_projects(c.projects_desktop())

        def done(projects, exc):
            self._caps_call_running = False
            if exc is not None:
                if isinstance(exc, PermissionDenied):
                    self._lost_desktop_access(exc)
                elif isinstance(exc, SessionExpired):
                    self._on_session_expired()
                return  # 429 / offline / server trouble: keep what we have
            self._apply_projects(projects)

        self._run("Geodit: checking permissions", work, done)

    def _apply_projects(self, projects: List[ProjectInfo]) -> None:
        self.projects = projects
        if self.dock is not None and self.project is None and self.dock.stack.currentIndex() == PAGE_PROJECTS:
            self._show_projects()
        current = self.project
        if current is None:
            return
        match = next((p for p in projects if p.id == current.id), None)
        if match is None or not match.can_view:
            self._message(f"You no longer have access to {current.name} in QGIS.", Qgis.MessageLevel.Warning, 0)
            self.close_project()
            return
        self.project = match
        self.layers.set_project(match)
        if self.dock is not None:
            self.dock.update_project(match)
        if not match.same_access(current):
            self._message(f"Your permissions in {match.name} changed: {match.access_label}.", Qgis.MessageLevel.Info)
            self.request_sync("permissions")

    def _lost_desktop_access(self, exc: BaseException) -> None:
        """The user is no longer an Owner/Admin/Editor anywhere: sign out."""
        self.close_project(show_list=False)
        self._run("Geodit sign out", lambda c: c.logout(), lambda _v, _e: None)
        self.secrets.delete(self.base_url)
        self.config.forget(self.base_url)
        self.tokens = TokenStore()
        self.user_id = None
        self._show_signed_out(self._error_text(exc))

    def _handle_call_error(self, exc) -> None:
        if isinstance(exc, SessionExpired):
            self._on_session_expired()

    def sign_out(self) -> None:
        pending = self._last_report.pending_total if self._last_report else 0
        if pending:
            answer = QMessageBox.question(
                self.dock,
                "Sign out of Geodit",
                f"{pending} change(s) haven't been uploaded yet. They stay on this computer and "
                f"upload the next time you sign in as {self.display_name}. Sign out anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.close_project(show_list=False)
        # The logout task captures the current TokenStore; the next session gets a fresh one.
        self._run("Geodit sign out", lambda c: c.logout(), lambda _v, _e: None)
        self.secrets.delete(self.base_url)
        self.config.forget(self.base_url)
        self.tokens = TokenStore()
        self.user_id = None
        self._show_signed_out()

    def _on_session_expired(self) -> None:
        self._session_dead = True
        self._queued = None
        self.timer.stop()
        self.secrets.delete(self.base_url)
        self._message(
            "Your Geodit session has expired. Sign in again to keep syncing — local edits are kept.",
            Qgis.MessageLevel.Warning,
            0,
        )
        self._show_signed_out("Your session has expired. Please sign in again.")

    # ============================================================ projects
    def open_project(self, project: ProjectInfo) -> None:
        if self.user_id is None or self.worker_id is None:
            return
        # Without an assigned area there is nothing to sync — except for the
        # project already open (it lost its area mid-session and stays open).
        reopening = self.project is not None and self.project.id == project.id
        if not project.can_open and not reopening:
            return
        if self.project is not None and self.project.id != project.id:
            self.close_project(show_list=False)
        self.project = project
        self._area_blocked_seen = project.area_blocker is not None
        folder = paths.project_dir(self.config.data_root, self.base_url, self.user_id, project.id)
        self.config.remember_project(self.base_url, project.id, project.name)
        self.layers.attach(self.base_url, self.user_id, project, folder)
        self.dock.show_project(project, auto_sync=self.config.auto_sync, interval=self.config.interval_min)
        self.dock.set_area_blocked(self._area_blocked_seen)
        self._last_report = None
        self._restart_timer()
        self.request_sync("open")

    def leave_project(self) -> None:
        """The dock's back button. A project whose area was unassigned can't be
        opened again until it is reassigned — say so if changes still wait."""
        project = self.project
        pending = self._last_report.pending_total if self._last_report is not None else 0
        if project is not None and self.layers.area_blocked and pending:
            answer = QMessageBox.question(
                self.dock,
                "Leave project",
                f"{pending} change(s) in {project.name} haven't been uploaded yet. No survey area is "
                "assigned to you there any more, so you can't open the project again until one is; "
                "the changes stay on this computer until then. Leave anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.close_project()

    def close_project(self, show_list: bool = True) -> None:
        self.timer.stop()
        self.soon.stop()
        self._queued = None
        if self._sync_task is not None:
            self._sync_task.cancel()
        self.layers.detach()
        self.project = None
        self._area_blocked_seen = False
        self.config.remember_project(self.base_url, None)
        if show_list and self.dock is not None and self.user_id is not None:
            self._show_projects()

    def _on_project_clearing(self) -> None:
        # The QGIS project is being closed: our layers are about to disappear.
        if self.project is not None:
            self.close_project(show_list=True)

    def _on_project_read(self, *_args) -> None:
        if not self.layers.geodit_layers_in_project() or self.project is not None:
            return
        bar = self.iface.messageBar()
        if self.user_id is not None:
            layer = self.layers.geodit_layers_in_project()[0]
            pid = int(layer.customProperty("geodit/project") or 0)
            match = next((p for p in self.projects if p.id == pid), None)
            if match is not None and match.can_open:
                self.open_project(match)
                return
            if match is not None:
                why = self._why_not_openable(match)
                text = f"This project's Geodit layers belong to {match.name}, which can't be synced now: {why}."
            else:
                text = "This project's Geodit layers belong to a project you can't sync from QGIS."
            bar.pushMessage("Geodit", text, Qgis.MessageLevel.Warning, 0)
            return
        widget = bar.createMessage("Geodit", "This project contains Geodit layers.")
        button = QPushButton("Sign in to sync")
        button.clicked.connect(lambda: (self.dock.show(), self.dock.raise_()))
        widget.layout().addWidget(button)
        bar.pushWidget(widget, Qgis.MessageLevel.Info, 0)

    # ================================================================ sync
    def set_auto_sync(self, on: bool) -> None:
        self.config.auto_sync = on
        self._restart_timer()

    def set_interval(self, minutes: int) -> None:
        self.config.interval_min = minutes
        self._restart_timer()

    def _restart_timer(self) -> None:
        self.timer.stop()
        if self.project is not None and self.config.auto_sync and not self._session_dead:
            self.timer.start(self.config.interval_min * 60_000)

    def _on_layer_saved(self) -> None:
        if self.project is not None and self.config.auto_sync:
            self.soon.start(SAVE_SYNC_DELAY_MS)

    def sync_now(self) -> None:
        self.request_sync("manual")

    def full_resync(self) -> None:
        answer = QMessageBox.question(
            self.dock,
            "Full re-sync",
            "Download everything in your survey area again? Unsynced local edits are kept; local "
            "copies of features that no longer exist on the server are removed. This can take a "
            "while on large layers.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.request_sync("manual", full=True)

    def discard_local_changes(self) -> None:
        if self.project is None:
            return
        answer = QMessageBox.question(
            self.dock,
            "Discard unsynced changes",
            "Put every Geodit layer of this project back to the last synced copy? Deleted features come "
            "back, and changed or new features are moved to the “discarded edits” layer (nothing is "
            "uploaded). Save or roll back any layer you are editing first.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.request_sync("manual", discard=True)

    def request_sync(self, reason: str, *, full: bool = False, discard: bool = False) -> None:
        if self._shutting_down or self.project is None or self.user_id is None or self.worker_id is None:
            return
        if self._session_dead or not self.tokens.has_session:
            return
        if self._sync_task is not None:
            if reason != "timer":  # the running sync already downloads
                self._queue(reason, full=full, discard=discard)
            return
        now = time.time()
        manual = reason in ("manual", "open", "permissions")
        if now < self._rate_limit_until and reason not in _SKIP_RATE_LIMIT:
            if reason == "saved":  # upload as soon as the server lets us
                self.soon.start(int((self._rate_limit_until - now) * 1000) + 500)
            return
        if now < self._backoff_until and reason not in _SKIP_BACKOFF:
            return
        if self.layers.editing_in_transaction_group():
            self.dock.set_status(
                self.dock.status_label.text(),
                [
                    "Sync waits while layers are edited with “automatic transaction groups” on. Save "
                    "or stop editing to sync."
                ],
            )
            return
        if now - self._last_caps_refresh > CAPS_REFRESH_S:
            self._refresh_caps()
        self.layers.refresh_modified()
        ctx = SyncContext(
            base_url=self.base_url,
            data_root=self.config.data_root,
            user_id=self.user_id,
            project_id=self.project.id,
            worker_id=self.worker_id,
            role=self.project.role,
            modified=self.modified,
            full_resync=full,
            push_enabled=(manual or now >= self._push_paused_until) and not discard,
            can_edit=self.project.can_edit,
            can_delete=self.project.can_delete,
            discard_local=discard,
        )
        quiet = reason in _QUIET
        task = SyncTask(
            f"Geodit sync: {self.project.name}", ctx, self.tokens, self.clock, self._on_sync_done, quiet=quiet
        )
        task.progressChanged.connect(self.dock.set_progress)
        task.layerReady.connect(functools.partial(self._on_layer_ready, task), Qt.ConnectionType.QueuedConnection)
        self._sync_task = task
        self.layers.begin_tick()
        self.dock.set_syncing(True, quiet=quiet)
        QgsApplication.taskManager().addTask(task)

    def _queue(self, reason: str, *, full: bool, discard: bool) -> None:
        """Remember a request made while a sync runs — for this project only."""
        assert self.project is not None
        queued = self._queued
        if queued is None or queued.project_id != self.project.id:
            queued = self._queued = QueuedSync(self.project.id, reason)
        elif _PRIORITY.get(reason, 0) > _PRIORITY.get(queued.reason, 0):
            queued.reason = reason
        queued.full = queued.full or full
        queued.discard = queued.discard or discard

    def _run_queued(self, queued: QueuedSync) -> None:
        if self.project is None or self.project.id != queued.project_id:
            return  # the project was closed or switched meanwhile
        self.request_sync(queued.reason, full=queued.full, discard=queued.discard)

    def _on_layer_ready(self, task: SyncTask, event: LayerEvent) -> None:
        """A part of the running sync is done (queued from its worker thread):
        show it now rather than when the whole tick ends."""
        try:
            if task is not self._sync_task or self.project is None or event.project_id != self.project.id:
                return  # a sync of a project that is no longer open
            self.layers.apply_layer_event(event)
            if event.kind == "survey_area":
                self._on_area_state(event.area_blocked)
        except Exception:  # noqa: BLE001 - never raise into Qt's event loop
            self._log(traceback.format_exc(), Qgis.MessageLevel.Warning)

    def _on_area_state(self, blocked: bool) -> None:
        """A sync's answer on whether a survey area is assigned to the user."""
        project = self.project
        if project is None:
            return
        if self.dock is not None:
            self.dock.set_area_blocked(blocked)
        if blocked == self._area_blocked_seen:
            return
        self._area_blocked_seen = blocked
        # Patch the list entry, so the picker marks (or frees) the project
        # without waiting for the hourly list refresh.
        patched = replace(project, has_assigned_area=not blocked, has_survey_area=True if not blocked else None)
        self.projects = [patched if p.id == project.id else p for p in self.projects]
        self.project = patched
        if blocked:
            self._message(
                f"No survey area is assigned to you in {project.name} any more. Its layers are read-only now; "
                "changes you already saved still upload.",
                Qgis.MessageLevel.Warning,
                0,
            )

    def _log_once(self, text: str, level) -> None:
        if text not in self._logged_warnings:
            self._logged_warnings.add(text)
            self._log(text, level)

    def _log_timing(self, report: SyncReport) -> None:
        total = sum(report.timings.values())
        pushed = sum(lr.pushed for lr in report.layers.values())
        pulled = sum(lr.pulled for lr in report.layers.values())
        changed = pushed or pulled or any(lr.rows_changed for lr in report.layers.values())
        if not changed and total < SLOW_TICK_S:
            return
        phases = " · ".join(f"{name} {secs:.1f}" for name, secs in report.timings.items())
        self._log(
            f"Sync {report.project_id}: {total:.1f} s — {phases} · {report.requests} requests "
            f"({report.request_s:.1f} s) · ↑{pushed} ↓{pulled}"
        )

    def _on_sync_done(self, task: SyncTask) -> None:
        self._sync_task = None
        if self._shutting_down:
            return
        try:
            self._apply_sync_result(task)
        finally:
            # Whatever happened to this tick, a request queued meanwhile runs.
            queued, self._queued = self._queued, None
            if queued is not None and not self._shutting_down:
                QTimer.singleShot(QUEUED_SYNC_DELAY_MS, lambda: self._run_queued(queued))

    def _apply_sync_result(self, task: SyncTask) -> None:
        current = self.project is not None and task.ctx.project_id == self.project.id
        self.dock.set_syncing(False)
        report = task.report
        now = time.time()
        if report is not None:
            for warning in report.warnings:
                self._log_once(warning, Qgis.MessageLevel.Warning)
        if report is None:
            self._log(task.error or "Sync failed without a report", Qgis.MessageLevel.Critical)
            self._failures += 1
            self._backoff_until = now + backoff_delay(self._failures)
            if current:
                self.dock.set_status(
                    "Sync failed unexpectedly. It will retry automatically.",
                    [("error", (task.error or "").strip().splitlines()[-1] if task.error else "")],
                    state="error",
                )
            return
        if not current or self.project is None or report.project_id != self.project.id:
            return  # finished after the project was closed/switched
        self._log_timing(report)
        notes: List[tuple] = []
        state = "ok"
        headline: Optional[str] = None
        kind = report.error_kind
        if kind == "session_expired":
            self._on_session_expired()
            return
        if kind == "forbidden":
            self._message(f"You no longer have access to {self.project.name}.", Qgis.MessageLevel.Warning, 0)
            self.close_project()
            self.refresh_projects()
            return
        if report.plan_expired:
            self._push_paused_until = now + PLAN_EXPIRED_RETRY_S
            state = "paused"
            notes.append(
                (
                    "warning",
                    "The project owner's plan has expired: uploads are paused (downloads continue). "
                    "Use “Sync now” to retry after renewal.",
                )
            )
        elif task.ctx.push_enabled:
            self._push_paused_until = 0.0
        if kind == "rate_limited":
            self._rate_limit_until = now + (report.retry_after_s or 60)
            state, headline = "paused", "Waiting for the server"
            notes.append(("warning", f"The server asked us to slow down; next sync in {report.retry_after_s or 60} s."))
        elif kind in ("network", "server", "store", "unknown"):
            self._failures += 1
            delay = backoff_delay(self._failures)
            self._backoff_until = now + delay
            state = "error"
            notes.append(("error", f"{report.error or 'Sync failed'} — retrying in {delay // 60 or 1} min."))
        elif report.ok or kind == "plan_expired":
            self._failures = 0
            self._backoff_until = self._rate_limit_until = 0.0
        if report.busy:
            notes.append(("info", "Another QGIS window is syncing this project right now."))
        if report.canceled:
            notes.append(("info", "Sync was canceled."))
        if report.area_checked:
            self._on_area_state(report.area_blocked)
        self.layers.apply_report(report)
        if report.ok or kind == "plan_expired":
            self._last_sync_at = now
            self._last_report = report
        notes += self._report_notes(report, self.project)
        pending = report.pending_total
        status = f"Last synced {describe_time(self._last_sync_at)}"
        if self.layers.area_blocked:
            status += f" · {pending} change(s) waiting to upload" if pending else " · all changes uploaded"
            if state == "ok":
                state, headline = "paused", "No survey area assigned"
        elif pending and not self.project.can_edit:
            status += f" · {pending} local change(s) can't be uploaded"
            if state == "ok":
                state = "readonly"
        elif pending:
            status += f" · {pending} change(s) waiting to upload"
            if state == "ok":
                state = "pending"
        else:
            status += " · all changes uploaded"
        self.dock.set_status(status, notes, state=state, headline=headline)
        self.dock.set_issues(report)
        if report.permission_denied:
            self._refresh_caps()

    @staticmethod
    def _report_notes(report: SyncReport, project: ProjectInfo) -> List[tuple]:
        notes: List[tuple] = []
        if report.area_blocked:
            notes.append(
                (
                    "warning",
                    "No survey area is assigned to you in this project, so nothing downloads and the layers are "
                    "read-only. Changes you already saved still upload. Ask the project owner to assign you an "
                    "area (web Map page → Assign area), then click Sync now.",
                )
            )
        restored = report.restored_total
        if restored:
            notes.append(
                (
                    "warning",
                    f"Deleting features isn't allowed for your role in this project: {restored} deleted "
                    "feature(s) were restored.",
                )
            )
        denied = report.denied_total
        if denied:
            notes.append(
                (
                    "warning",
                    f"The server refused {denied} change(s): your role's Page access for the Map doesn't allow "
                    "them. They stay on this computer; checking your permissions again.",
                )
            )
        if report.pending_total and not project.can_edit:
            notes.append(
                (
                    "info",
                    "You have view-only access here, so local changes can't be uploaded. Use ⋯ → "
                    "“Discard unsynced changes” to drop them, or ask the owner for Map edit access.",
                )
            )
        reverted = report.reverted_total
        if reverted:
            notes.append(("info", f"Discarded {reverted} unsynced change(s)."))
        skipped = [lr.name for lr in report.layers.values() if lr.revert_skipped_unsaved]
        if skipped:
            notes.append(
                ("warning", "Not discarded — save or roll back your edits first in: " + ", ".join(skipped) + ".")
            )
        waiting = [lr.name for lr in report.layers.values() if lr.pull_skipped_unsaved]
        if waiting:
            notes.append(("info", "Downloads wait for unsaved edits in: " + ", ".join(waiting) + ". Save to sync."))
        orphaned = [lr.name for lr in report.layers.values() if lr.orphaned]
        if orphaned:
            notes.append(
                ("warning", "Removed on the server (kept locally, no longer synced): " + ", ".join(orphaned) + ".")
            )
        return notes

    def zoom_to(self, shp_id: int, fid: int, kind: str) -> None:
        self.layers.zoom_to(shp_id, fid, kind)
