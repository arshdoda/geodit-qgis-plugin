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

import contextlib
import functools
import os
import time
import traceback
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Set

from qgis.core import Qgis, QgsApplication, QgsMessageLog, QgsProject
from qgis.gui import QgsApplicationExitBlockerInterface
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QPushButton

from .config import Config, SecretStore
from .core.clock import ServerClock
from .core.policy import PLAN_EXPIRED_RETRY_S, backoff_delay
from .core.projects import AREA_NOT_ASSIGNED, ProjectInfo, hidden_counts, parse_desktop_projects, visible_projects
from .core.status import FAILED, NOT_SYNCED, OK, PLAN_EXPIRED, RATE_LIMITED, Status, plural, sync_status
from .core.web_access import resolve as resolve_web_access
from .net.errors import ApiError, NotFound, PermissionDenied, ServerTooOld, SessionExpired
from .net.tokens import TokenStore
from .qgis_ui.dock import PAGE_PROJECTS, GeoditDock
from .qgis_ui.feature_picker import FeaturePicker
from .qgis_ui.layers import KIND_LAYER, PROP, LayerManager
from .qgis_ui.pick import FeaturePickTool, PointPickTool
from .qgis_ui.theme import Theme, line_icon
from .responses import ORPHANED_FORM_TEXT, FeatureFormController
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


class _FormExitBlocker(QgsApplicationExitBlockerInterface):
    """Quitting QGIS with unsaved answers in the feature form asks first."""

    def __init__(self, forms: FeatureFormController) -> None:
        super().__init__()
        self._forms = forms

    def allowExit(self) -> bool:  # noqa: N802 - QGIS API
        return self._forms.allow_exit()


class GeoditPlugin:
    def __init__(self, iface) -> None:
        self.iface = iface
        self.config = Config()
        self.secrets = SecretStore()
        self.modified = ModifiedLayers()
        self.layers = LayerManager(iface, self.modified, on_saved=self._on_layer_saved, message=self._message)
        self.dock: Optional[GeoditDock] = None
        self.action: Optional[QAction] = None
        # The feature form: a map tool that opens a feature's survey response.
        self.forms = FeatureFormController(self)
        self.form_action: Optional[QAction] = None
        self._pick_tool: Optional[FeaturePickTool] = None
        # The map tool picking replaced, to hand the map back to (and the watch on its deletion).
        self._previous_tool = None
        self._tool_watch = None
        self._picker: Optional[FeaturePicker] = None  # the overlapping features' list, while it is up
        self._exit_blocker: Optional[_FormExitBlocker] = None

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
        self._shown_status: Optional[Status] = None  # what the Sync card says: (state, headline, detail)
        # The open project's form names (form id → name), for the Data tool's
        # list of which layer opens which form. Read once per project open.
        self._form_names: Dict[int, str] = {}
        self._form_names_project: Optional[int] = None
        # Settings › Web access as last read, per project — for an older server
        # whose desktop list doesn't resolve them (``data``): project → blob.
        self._web_access: Dict[int, object] = {}
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
        # Not toggled ↔ visibilityChanged: a dock whose tab another panel covers
        # (tabbed with it by the user) reports "not visible", and that must not close it.
        self.action.triggered.connect(self._dock_action)
        self.dock.visibilityChanged.connect(self._sync_dock_action)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&Geodit", self.action)
        self.form_action = QAction(
            line_icon("crosshair", Theme.current().text, 20),
            "Geodit Data: click a feature to open its form",
            self.iface.mainWindow(),
        )
        self.form_action.setCheckable(True)
        self.form_action.setToolTip(
            "Geodit: while this is on, click a feature on a Geodit layer to open its survey form in its own window"
        )
        self.form_action.triggered.connect(self._feature_form_triggered)
        self.form_action.toggled.connect(self._form_tool_toggled)
        self.iface.addPluginToMenu("&Geodit", self.form_action)
        self._exit_blocker = _FormExitBlocker(self.forms)
        self.iface.registerApplicationExitBlocker(self._exit_blocker)
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
        if self._exit_blocker is not None:
            self.iface.unregisterApplicationExitBlocker(self._exit_blocker)
            self._exit_blocker = None
        # The form first: a location pick it has on the map hands the canvas back.
        self.forms.unload()
        self._stop_picking()
        self._pick_tool = None
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("&Geodit", self.action)
            self.action.deleteLater()
            self.action = None
        if self.form_action is not None:
            self.iface.removePluginMenu("&Geodit", self.form_action)
            self.form_action.deleteLater()
            self.form_action = None
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

    # ============================================================ dock
    def _sync_dock_action(self, _visible: bool = False) -> None:
        """Ticked while the Geodit dock is open — also while the tab of another
        panel tabbed with it covers it: Qt moves a covered tab off-screen and
        says "not visible", but it isn't hidden."""
        if self.action is not None and self.dock is not None:
            self.action.setChecked(not self.dock.isHidden())

    def _dock_action(self, _checked: bool = False) -> None:
        """The toolbar button: open the dock, bring it forward when another tab
        covers it, or close it when it's in front."""
        dock = self.dock
        if dock is None:
            return
        if dock.isHidden() or dock.visibleRegion().isEmpty():
            dock.show()
            dock.raise_()
        else:
            dock.hide()
        self._sync_dock_action()

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
        self._web_access.clear()  # a new session reads the settings afresh
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
        if self.dock is not None:
            self.dock.set_user(self.display_name)
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

    def refresh_caps_if_older(self, max_age_s: float) -> None:
        """Opening a feature form re-reads permissions older than ``max_age_s``,
        so a Web access change reaches QGIS about as fast as the web."""
        if time.time() - self._last_caps_refresh > max_age_s:
            self._refresh_caps()

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
        self._apply_open_project(self._with_web_access(match))
        self._read_web_access(match)

    def _apply_open_project(self, updated: ProjectInfo, *, announce: bool = True) -> None:
        """New permissions for the open project, applied in place: the layers'
        read-only state, the open form, the panel."""
        current = self.project
        self.project = updated
        self.layers.set_project(updated)
        self.forms.project_changed(updated)
        if self.dock is not None:
            self.dock.update_project(updated)
        if current is not None and not updated.same_access(current):
            if announce:
                self._message(
                    f"Your permissions in {updated.name} changed: {updated.access_label}.", Qgis.MessageLevel.Info
                )
            self.request_sync("permissions")

    def _with_web_access(self, project: ProjectInfo) -> ProjectInfo:
        """An older server's row narrowed by the project's Web access settings
        as last read, resolved as the web does (``core.web_access``) — never
        wider than the server's Map flags. A row the server resolved itself
        (``data``) is taken as it is."""
        if project.data_known or project.id not in self._web_access:
            return project
        map_caps, data = resolve_web_access(project.role, self._web_access[project.id])
        return replace(
            project,
            can_view=project.can_view and map_caps.can_view,
            can_edit=project.can_edit and map_caps.can_edit,
            can_delete=project.can_delete and map_caps.can_delete,
            data=data,
        )

    def _read_web_access(self, project: ProjectInfo) -> None:
        """An older server's desktop list has no answer-modal caps (``data``):
        read the project's Settings › Web access (``projects/detail``), so an
        admin or an editor gets in QGIS what the web gives them. Until it is
        read, the form is view only; a failure keeps it so."""
        if project.data_known:
            return
        tokens, project_id = self.tokens, project.id

        def work(client):
            settings = client.project_detail(project_id).get("survey_settings")
            return settings.get("web_access") if isinstance(settings, dict) else None

        def done(web_access, exc) -> None:
            if self.tokens is not tokens or self.project is None or self.project.id != project_id:
                return  # signed out, or another project open now
            if exc is not None:
                if isinstance(exc, SessionExpired):
                    self._handle_call_error(exc)
                return
            first = project_id not in self._web_access
            self._web_access[project_id] = web_access
            raw = next((p for p in self.projects if p.id == project_id), self.project)
            updated = self._with_web_access(raw)
            if updated != self.project:
                # The first read on opening isn't news; a later change is.
                self._apply_open_project(updated, announce=not first)

        self._run("Geodit: reading the project's Web access", work, done)

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
        """The account menu. Unsaved answers in the feature form are asked about first."""
        self.forms.guard(self._sign_out, parent=self.dock)

    def _sign_out(self) -> None:
        pending = self._last_report.pending_total if self._last_report else 0
        if pending:
            verb = "hasn't" if pending == 1 else "haven't"
            answer = QMessageBox.question(
                self.dock,
                "Sign out of Geodit",
                f"{plural(pending, 'change')} {verb} been uploaded yet. Your changes stay on this computer and "
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
        project = self._with_web_access(project)
        self.project = project
        self._area_blocked_seen = project.area_blocker is not None
        folder = paths.project_dir(self.config.data_root, self.base_url, self.user_id, project.id)
        self.config.remember_project(self.base_url, project.id, project.name)
        self.layers.attach(self.base_url, self.user_id, project, folder)
        self.dock.show_project(project, auto_sync=self.config.auto_sync, interval=self.config.interval_min)
        self.dock.set_area_blocked(self._area_blocked_seen)
        if self.layers.project_layers(KIND_LAYER):
            # Known from the local copy already; otherwise after the first sync.
            self._push_form_layers()
        self._read_form_names(project.id)
        self._read_web_access(project)
        self._last_report = None
        self._last_sync_at = None
        self._shown_status = None
        self._restart_timer()
        self.request_sync("open")

    def leave_project(self) -> None:
        """The dock's back button: stop syncing and go back to the list.
        Unsaved answers in the feature form are asked about first."""
        self.forms.guard(self._leave_project, parent=self.dock)

    def _leave_project(self) -> None:
        """A project whose area was unassigned can't be opened again until it
        is reassigned — say so if changes still wait."""
        project = self.project
        pending = self._last_report.pending_total if self._last_report is not None else 0
        if project is not None and self.layers.area_blocked and pending:
            verb = "hasn't" if pending == 1 else "haven't"
            answer = QMessageBox.question(
                self.dock,
                "Leave project",
                f"{plural(pending, 'change')} in {project.name} {verb} been uploaded yet. No survey area is "
                "assigned to you there any more, so you can't open the project again until one is; "
                "your changes stay on this computer until then. Leave anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.close_project()
        if project is not None and self.dock is not None and self.user_id is not None:
            self.dock.set_projects_notice(
                f"Stopped syncing {project.name}. Its layers stay in this QGIS project — open it again to resume."
            )

    def close_project(self, show_list: bool = True) -> None:
        self.forms.project_closed()
        self._stop_picking()  # the pick tool goes with the project
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
        if self.project is None:
            return
        # The save waits for the next sync (auto-sync off, or a canceled one):
        # "Up to date" no longer holds. The next completed sync counts it.
        if self._shown_status is not None and self._shown_status[0] == "ok":
            self._shown_status = ("pending", "Changes waiting to upload", self._shown_status[2])
            self.dock.set_sync_state(*self._shown_status)
        if self.config.auto_sync:
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
        if self.project is None:
            raise RuntimeError("no project is open")
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
                last = (task.error or "").strip().splitlines()[-1] if task.error else ""
                text = "The sync stopped unexpectedly. It will retry automatically."
                self._show_status(FAILED, [("error", f"{text} ({last})" if last else text)])
            return
        if not current or self.project is None or report.project_id != self.project.id:
            return  # finished after the project was closed/switched
        self._log_timing(report)
        notes: List[tuple] = []
        outcome = OK
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
            outcome = PLAN_EXPIRED
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
            wait = report.retry_after_s or 60
            self._rate_limit_until = now + wait
            outcome = RATE_LIMITED
            notes.append(
                ("info", f"The Geodit server asked to slow down: the next sync is in {plural(wait, 'second')}.")
            )
        elif kind in ("network", "server", "store", "unknown"):
            self._failures += 1
            delay = backoff_delay(self._failures)
            self._backoff_until = now + delay
            outcome = FAILED
            reason = (report.error or "Sync failed").rstrip(". ")
            notes.append(("error", f"{reason}. Retrying in {plural(max(1, delay // 60), 'minute')}."))
        elif report.ok or kind == "plan_expired":
            self._failures = 0
            self._backoff_until = self._rate_limit_until = 0.0
        if report.busy:
            notes.append(("info", "Another QGIS window is syncing this project right now."))
        if report.canceled:
            notes.append(("info", "Sync was canceled."))
        if report.area_checked:
            self._on_area_state(report.area_blocked)
        for shp_id in [k for k, lr in report.layers.items() if lr.orphaned and self.layers.is_removed(k)]:
            del report.layers[shp_id]  # the user removed it while this sync ran
        self.layers.apply_report(report)
        # Only a sync that ran to the end counted the pending changes and the problems.
        completed = report.ok or kind == "plan_expired"
        if completed:
            self._last_sync_at = now
            self._last_report = report
        notes += self._report_notes(report, self.project)
        # A sync that was skipped or canceled changed nothing: the last status stands.
        self._show_status(None if report.busy or report.canceled else outcome, notes)
        if completed:
            self.dock.set_issues(report)  # a layer removed on the server is listed there
            self._push_form_layers()
        if report.permission_denied:
            self._refresh_caps()

    def _show_status(self, outcome: Optional[str], notes: List[tuple]) -> None:
        """The Sync card for how the latest attempt ended (``None``: keep the last status)."""
        if outcome is None:
            status = self._shown_status or NOT_SYNCED
        else:
            report = self._last_report
            status = sync_status(
                outcome,
                last_synced=self._last_sync_at,
                pending=report.pending_total if report is not None else None,
                can_edit=self.project.can_edit if self.project is not None else True,
                area_blocked=self.layers.area_blocked,
            )
            self._shown_status = status
        state, headline, detail = status
        self.dock.set_status(detail, notes, state=state, headline=headline)

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
            were = "was" if restored == 1 else "were"
            notes.append(
                (
                    "warning",
                    f"Deleting features isn't allowed for your role in this project: "
                    f"{plural(restored, 'deleted feature')} {were} restored.",
                )
            )
        denied = report.denied_total
        if denied:
            notes.append(
                (
                    "warning",
                    f"The server refused {plural(denied, 'change')}: your role's Page access for the Map doesn't "
                    "allow them. Your changes stay on this computer; checking your permissions again.",
                )
            )
        if report.pending_total and not project.can_edit:
            notes.append(
                (
                    "warning",
                    "You have view-only access here, so local changes can't be uploaded. Use ⋯ → "
                    "“Discard unsynced changes” to drop them, or ask the owner for Map edit access.",
                )
            )
        reverted = report.reverted_total
        if reverted:
            notes.append(("info", f"Discarded {plural(reverted, 'unsynced change')}."))
        skipped = [lr.name for lr in report.layers.values() if lr.revert_skipped_unsaved]
        if skipped:
            notes.append(
                ("warning", "Not discarded — save or roll back your edits first in: " + ", ".join(skipped) + ".")
            )
        waiting = [lr.name for lr in report.layers.values() if lr.pull_skipped_unsaved]
        if waiting:
            notes.append(("warning", "Downloads wait for unsaved edits in: " + ", ".join(waiting) + ". Save to sync."))
        return notes

    def zoom_to(self, shp_id: int, fid: int, kind: str) -> None:
        self.layers.zoom_to(shp_id, fid, kind)

    def remove_layer(self, shp_id: int) -> None:
        """Needs attention › a layer removed on the server: after a yes, it
        leaves QGIS and its copy on this computer is deleted."""
        report = self._last_report
        entry = report.layers.get(shp_id) if report is not None else None
        if self.project is None or entry is None or not entry.orphaned or self._editing_blocks_removal(shp_id):
            return
        project_id = self.project.id
        answer = QMessageBox.question(
            self.dock,
            "Remove layer",
            f"Remove “{entry.name}” from QGIS and delete its copy on this computer?\n\n"
            "It was removed from the project on the server, so this copy is the only one left. To keep "
            "its features, export the layer first: right-click it in the Layers panel › Export › Save "
            "Features As….",
        )
        if answer != QMessageBox.StandardButton.Yes or self.project is None or self.project.id != project_id:
            return
        # A sync may have finished under the dialog: only a layer still gone from the server goes.
        report = self._last_report
        entry = report.layers.get(shp_id) if report is not None else None
        if entry is None or not entry.orphaned or self._editing_blocks_removal(shp_id):
            return
        if not self.layers.remove_layer(shp_id):
            return
        if self._sync_task is None:
            # Not while a sync of another QGIS window holds the project; in use
            # (Windows) or syncing, the next sync deletes the files.
            lock = paths.ProjectLock(self.layers.folder)
            if lock.try_lock():
                try:
                    self.layers.delete_removed_files(shp_id)
                finally:
                    lock.unlock()
        report.layers.pop(shp_id, None)
        self.dock.set_issues(report)

    def _editing_blocks_removal(self, shp_id: int) -> bool:
        """Removing a layer drops its edit buffer: ask for a save or a discard first."""
        layer = self.layers.layer_for(shp_id)
        if layer is None or not layer.isEditable():
            return False
        self._message(
            f"Stop editing {layer.name()} first — save or discard your edits — then remove it.",
            Qgis.MessageLevel.Warning,
        )
        return True

    # ======================================================== feature form
    def _read_form_names(self, project_id: int) -> None:
        """The project's form names, for the Data tool's list (``forms/list-basic``,
        as the web map reads them). A failure only leaves the names out."""
        if self._form_names_project != project_id:
            self._form_names = {}
            self._form_names_project = project_id
        tokens = self.tokens

        def work(client):
            names: Dict[int, str] = {}
            for row in client.forms_list_basic(project_id):
                try:
                    names[int(row.get("id"))] = str(row.get("name") or "")
                except (TypeError, ValueError):
                    continue
            return names

        def done(names, exc) -> None:
            # Signed out (a logout empties that session's tokens: a late 401 is
            # no news) or another project open now — this reply is stale.
            if self.tokens is not tokens or self.project is None or self.project.id != project_id:
                return
            if exc is not None:
                self._handle_call_error(exc)
                return
            self._form_names = names
            self._push_form_layers()

        self._run("Geodit: reading form names", work, done)

    def _push_form_layers(self) -> None:
        """Which layer opens which form, for the Data tool — layers without a form left out."""
        if self.dock is None or self.project is None:
            return
        rows = [(name, self._form_names.get(form_id, "")) for name, form_id in self.layers.form_layers()]
        self.dock.set_form_layers(rows)

    def _synced_layers(self):
        """The layers a feature can be picked on: valid, still on the server and
        shown on the map (checked in the layer tree, with every parent group) —
        in the map's drawing order, the top one first, so a click finds what is
        drawn on top first."""
        root = QgsProject.instance().layerTreeRoot()
        out = []
        for layer in self.layers.project_layers(KIND_LAYER):
            node = root.findLayer(layer.id())
            if layer.isValid() and not self.layers.is_orphaned(layer) and node is not None and node.isVisible():
                out.append(layer)
        order = {layer.id(): i for i, layer in enumerate(self.iface.mapCanvas().layers())}
        out.sort(key=lambda layer: order.get(layer.id(), len(order)))
        return out

    def _feature_hits(self, hits, pixel) -> None:
        """A click of the Data tool: the features under it on layers with a
        survey form. One opens; several are listed to pick from, as on the web
        map; none with a form says so."""
        with_form = [(layer, feature) for layer, feature in hits if self._has_form(layer)]
        if not with_form:
            self._message("No form is attached to this layer.", Qgis.MessageLevel.Info)
            return
        if len(with_form) == 1:
            layer, feature = with_form[0]
            self._open_hit(layer, feature.id())
            return
        self._close_picker()
        canvas = self.iface.mapCanvas()
        self._picker = FeaturePicker(canvas, with_form, self._open_hit)
        self._picker.closed.connect(self._picker_gone)
        self._picker.show_at(canvas.mapToGlobal(pixel))

    def _has_form(self, layer) -> bool:
        try:
            shp_id = int(layer.customProperty("geodit/shp_id"))
        except (TypeError, ValueError):
            return False
        return self.layers.layer_form_id(shp_id) is not None

    def _open_hit(self, layer, fid: int) -> None:
        """Select the feature (alone) and open its form."""
        for other in self._synced_layers():
            if other is not layer and other.selectedFeatureCount():
                other.removeSelection()
        layer.selectByIds([fid])
        self.forms.open_feature(layer, fid)

    def _close_picker(self) -> None:
        picker, self._picker = self._picker, None
        if picker is not None:
            with contextlib.suppress(RuntimeError):  # already gone with its window
                picker.close()

    def _picker_gone(self, *_args) -> None:
        self._picker = None

    def _feature_form_triggered(self, checked: bool) -> None:
        """The menu action (Qt has already flipped its tick)."""
        self.toggle_feature_pick(checked)

    def toggle_feature_pick(self, on: bool) -> None:
        """The Geodit panel's Data button and the menu
        action — the web's Data tool: while it's on, each feature clicked on the
        map opens in the form window; off hands the map back to the tool before."""
        if on:
            self._start_picking()
        else:
            self._stop_picking()

    def _start_picking(self) -> None:
        action = self.form_action
        if self.project is None or self.user_id is None:
            if action is not None:
                action.setChecked(False)
            self._message("Open a Geodit project in the Geodit panel first.", Qgis.MessageLevel.Info)
            return
        canvas = self.iface.mapCanvas()
        if self._pick_tool is None:
            self._pick_tool = FeaturePickTool(
                canvas,
                self._synced_layers,
                self._feature_hits,
                lambda: self._message(
                    "No Geodit feature there — click a feature on a Geodit layer.", Qgis.MessageLevel.Info, 3
                ),
            )
            if action is not None:
                self._pick_tool.setAction(action)  # ticked while the tool is on the map
        current = canvas.mapTool()
        if current is not self._pick_tool:
            self._remember_tool(current)
            canvas.setMapTool(self._pick_tool)
        if action is not None and not action.isChecked():
            action.setChecked(True)
        # A feature already selected opens at once.
        if not self._open_selected_feature():
            self._message("Click a feature on a Geodit layer to open its form.", Qgis.MessageLevel.Info, 3)

    def _stop_picking(self) -> None:
        self._close_picker()
        canvas = self.iface.mapCanvas()
        tool, previous = self._pick_tool, self._previous_tool
        self._release_tool()
        if tool is not None and canvas is not None and canvas.mapTool() is tool:
            if previous is not None and previous is not tool and self._tool_usable(previous):
                canvas.setMapTool(previous)
            else:
                canvas.unsetMapTool(tool)
                pan = self.iface.actionPan()
                if pan is not None:
                    pan.trigger()
        if self.form_action is not None and self.form_action.isChecked():
            self.form_action.setChecked(False)

    def _remember_tool(self, tool) -> None:
        """The map tool picking replaces — forgotten when QGIS deletes it. A
        location pick for the form isn't one: it is spent once replaced, and
        giving it the map back would leave clicks doing nothing."""
        if isinstance(tool, PointPickTool):
            return  # keep the tool from before it
        self._release_tool()
        if tool is None:
            return
        self._previous_tool = tool
        self._tool_watch = functools.partial(self._forget_tool, tool)
        tool.destroyed.connect(self._tool_watch)

    def _release_tool(self) -> None:
        tool, watch = self._previous_tool, self._tool_watch
        self._previous_tool = self._tool_watch = None
        if tool is not None and watch is not None:
            with contextlib.suppress(TypeError, RuntimeError):
                tool.destroyed.disconnect(watch)

    def _forget_tool(self, tool, *_args) -> None:
        if self._previous_tool is tool:
            self._previous_tool = self._tool_watch = None

    @staticmethod
    def _tool_usable(tool) -> bool:
        """A digitizing tool's action is disabled once editing stops: don't go back to it."""
        action = tool.action()
        return action is None or action.isEnabled()

    def form_tool_active(self) -> bool:
        return bool(self.form_action is not None and self.form_action.isChecked())

    def _form_tool_toggled(self, active: bool) -> None:
        if self.dock is not None:
            self.dock.set_form_tool_active(active)

    def _open_selected_feature(self) -> bool:
        layer = self.iface.activeLayer()
        if (
            layer is not None
            and layer.customProperty(PROP + "kind") == KIND_LAYER
            and self.layers.is_orphaned(layer)
            and layer.selectedFeatureCount() == 1
        ):
            self._message(ORPHANED_FORM_TEXT, Qgis.MessageLevel.Info)
            return True
        synced = self._synced_layers()
        if layer not in synced:
            selected = [lyr for lyr in synced if lyr.selectedFeatureCount() == 1]
            layer = selected[0] if len(selected) == 1 else None
        if layer is None or layer.selectedFeatureCount() != 1:
            return False
        if str(layer.customProperty(PROP + "kind")) != KIND_LAYER:
            return False
        self.forms.open_feature(layer, int(layer.selectedFeatureIds()[0]))
        return True
