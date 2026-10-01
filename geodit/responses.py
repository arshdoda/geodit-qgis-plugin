"""The feature form: open a feature's survey response, edit it, save it.

The controller behind the "Feature form" window (``qgis_ui/response/window.py``)
and the Geodit panel's Data tool (``qgis_ui/pick.py``) — the QGIS twin of
geodit-ui's map Data button (``FeatureFormDialog`` → ``ResponseFormSheet``).
It resolves the picked feature (its layer's form, its server id, its response
id), loads the form and the stored answers off the main thread, builds a
``FormSession`` with the viewer's Web access (``ProjectInfo.data``), and saves
through the web answer routes: ``ans-update`` sends only the edited slots
(``clear`` for erased ones); ``ans-create`` links the feature, with
``if_unlinked`` so a stale local ``ans_id`` can't mint a duplicate. After a save
it asks for a sync, so the feature's ``gd_ans_id`` arrives with the next pull.

One window serves every feature. Leaving a form with unsaved answers — another
feature, closing the window, leaving the project, signing out, quitting QGIS —
goes through ``guard``: Save / Discard / Cancel.
"""

from __future__ import annotations

import copy
import datetime as _dt
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QPixmap
from qgis.PyQt.QtWidgets import QDialog, QFileDialog, QMessageBox

from .forms.answers import file_answer_from_value, is_absolute_media_url
from .forms.decode import ans_items_to_answers
from .forms.defaults import is_schema_unique_id
from .forms.jsnum import as_int_if_integral
from .forms.media import media_spec
from .forms.model import Form, QType, parse_form
from .forms.session import FormSession, SessionOptions
from .forms.unique import UNIQUE_CHECK_UNAVAILABLE, batch_body, batch_unique_groups, flagged_items
from .net.errors import (
    ApiError,
    Conflict,
    NetworkError,
    NotFound,
    PermissionDenied,
    PlanExpired,
    ServerError,
    SessionExpired,
    ValidationFailed,
)

FORM_CACHE_S = 300  # the web keeps a form's detail 5 minutes
PREVIEW_MAX_PX = 480  # an image is kept only as a preview: the window draws 96×72 / 160×72, sharp on 3× screens
PREVIEW_CACHE = 100  # previews kept, most recently used
CAPS_MAX_AGE_S = 300  # opening a form re-reads permissions older than this
STATUS_TEXT = {1: "Pending", 2: "Rejected", 3: "Approved"}
ORPHANED_FORM_TEXT = "This layer was removed on the server, so its features have no form here."
_WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


@dataclass
class FeatureTarget:
    project_id: int
    shp_id: int
    fid: int
    layer_id: str
    layer_name: str
    gd_id: int
    ans_id: Optional[int]
    form_id: int
    attributes: Dict[str, Any]
    unsaved_edits: bool
    geometry: Optional[QgsGeometry] = None
    crs: Optional[QgsCoordinateReferenceSystem] = None


@dataclass
class ResponseMeta:
    status: Optional[int] = None
    surveyor_id: Optional[int] = None
    surveyed_on: str = ""
    edited_on: str = ""
    verifier_id: Optional[int] = None
    verified_on: str = ""
    found: bool = True


@dataclass
class LoadResult:
    form_payload: dict
    items: List[dict]
    rows: Optional[List[dict]]
    team: Optional[Dict[int, str]]
    latest: Dict[int, int] = field(default_factory=dict)


class ProbeFailed(Exception):
    """The uniqueness probe couldn't run: the save is withheld (fail closed)."""


def _to_wire(value: Any) -> Any:
    """JSON the way the web sends it: whole-number floats as integers."""
    if isinstance(value, float):
        return as_int_if_integral(value)
    if isinstance(value, list):
        return [_to_wire(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_wire(v) for k, v in value.items()}
    return value


def _format_instant(raw: Any) -> str:
    """A server timestamp (an instant) in the viewer's local time."""
    if not isinstance(raw, str) or not raw:
        return ""
    text = raw.strip().replace("Z", "+00:00")
    try:
        moment = _dt.datetime.fromisoformat(text)
    except ValueError:
        return raw
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone().strftime("%d %b %Y · %H:%M")  # the web's "dd MMM yyyy · HH:mm"


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class FormServices:
    """What the editors call: media, thumbnails, map pick."""

    def __init__(self, controller: FeatureFormController) -> None:
        self.c = controller
        self._thumbs: OrderedDict[str, QPixmap] = OrderedDict()
        self._pick_tool = None  # the map tool of a location pick that waits for its click

    # ------------------------------------------------------------ location
    def feature_location(self) -> Optional[Tuple[float, float]]:
        target = self.c.target
        if target is None or target.geometry is None or target.geometry.isEmpty():
            return None
        point = target.geometry.pointOnSurface()
        if point.isEmpty():
            return None
        xy = point.asPoint()
        if target.crs is not None and target.crs.isValid() and target.crs != _WGS84:
            transform = QgsCoordinateTransform(target.crs, _WGS84, QgsProject.instance())
            xy = transform.transform(xy)
        return (xy.y(), xy.x())

    def picking(self) -> bool:
        """A location answer's map pick is waiting for its click."""
        return self._pick_tool is not None

    def cancel_pick(self) -> None:
        if self._pick_tool is not None:
            self._pick_tool.cancel()

    def pick_point(self, callback: Callable[[float, float], None]) -> None:
        from .qgis_ui.pick import PointPickTool

        self.cancel_pick()  # a second "Pick on map" replaces the first
        canvas = self.c.plugin.iface.mapCanvas()
        previous = canvas.mapTool()
        session = self.c.session
        window = self.c.window

        def done(point: Optional[QgsPointXY]) -> None:
            tool, self._pick_tool = self._pick_tool, None
            # Hand the canvas back — unless another tool is taking it right now.
            if previous is not None and tool is not None and canvas.mapTool() is tool and not tool.leaving:
                canvas.setMapTool(previous)
            if self.c.session is not session:
                return  # another feature's form is open now, or none
            if window is not None:
                window.clear_notice()
            if point is None:
                return  # cancelled
            crs = canvas.mapSettings().destinationCrs()
            if crs.isValid() and crs != _WGS84:
                point = QgsCoordinateTransform(crs, _WGS84, QgsProject.instance()).transform(point)
            callback(point.y(), point.x())
            if window is not None:
                window.present()  # the click went to the map: back to the form

        self._pick_tool = PointPickTool(canvas, done)
        canvas.setMapTool(self._pick_tool)
        self.c.notify(
            "Click the map to set the location. Move this window aside if it covers the spot. Esc cancels.", "info"
        )

    # ------------------------------------------------------------ media
    def thumbnail(self, value: Any, callback: Callable[[Any, Optional[QPixmap]], None]) -> None:
        file = file_answer_from_value(value)
        key = file["key"] if file else ""
        if not key:
            return
        if key in self._thumbs:
            self._thumbs.move_to_end(key)
            callback(value, self._thumbs[key])
            return
        project_id = self.c.project_id()
        if project_id is None:
            return

        def work(client):
            url = key if is_absolute_media_url(key) else client.media_url_by_key(project_id, key)
            return client.fetch_bytes(url) if url else b""

        def done(data, exc):
            if exc is not None or not data:
                callback(value, None)
                return
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                callback(value, self.keep_preview(key, pixmap))
            else:
                callback(value, None)

        self.c.plugin._run("Geodit: loading a preview", work, done)

    def keep_preview(self, key: str, pixmap: QPixmap) -> QPixmap:
        """A fetched or uploaded image, kept as a preview only: scaled down and in
        a short most-recently-used list — reviewing many photographed features
        must not hold every photo at full size."""
        if pixmap.width() > PREVIEW_MAX_PX or pixmap.height() > PREVIEW_MAX_PX:
            pixmap = pixmap.scaled(
                PREVIEW_MAX_PX,
                PREVIEW_MAX_PX,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._thumbs[key] = pixmap
        self._thumbs.move_to_end(key)
        while len(self._thumbs) > PREVIEW_CACHE:
            self._thumbs.popitem(last=False)
        return pixmap

    def clear_previews(self) -> None:
        self._thumbs.clear()

    def open_media(self, value: Any) -> None:
        file = file_answer_from_value(value)
        key = file["key"] if file else ""
        if not key:
            return
        if is_absolute_media_url(key):
            QDesktopServices.openUrl(QUrl(key.strip()))
            return
        project_id = self.c.project_id()
        if project_id is None:
            return

        def done(url, exc):
            if exc is not None or not url:
                self.c.notify(
                    f"Couldn't open the file: {self.c.plugin._error_text(exc) if exc else 'no link'}", "warning"
                )
                return
            QDesktopServices.openUrl(QUrl(url))

        self.c.plugin._run("Geodit: opening a file", lambda client: client.media_url_by_key(project_id, key), done)

    def choose_media(self, kind: str, question, started: Callable[[str], None], finished) -> None:
        window = self.c.window
        data: Optional[bytes] = None
        path = ""
        if kind == "signature":
            from .qgis_ui.response.media import SignatureDialog

            dialog = SignatureDialog(window)
            if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.png:
                return
            data = dialog.png
        else:
            filters = {
                "image": "Photos (*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff *.heic *.heif);;All files (*)",
                "document": "PDF documents (*.pdf)",
                "audio": "M4A audio (*.m4a *.mp4);;All files (*)",
                "video": "MP4 video (*.mp4);;All files (*)",
            }
            path, _ = QFileDialog.getOpenFileName(window, "Choose a file", "", filters.get(kind, "All files (*)"))
            if not path:
                return
        project_id = self.c.project_id()
        if project_id is None:
            return
        session = self.c.session
        spec = media_spec(kind)
        started("Uploading…" if data is not None else "Preparing and uploading…")
        if window is not None:
            window.media_busy(+1)

        def work(client):
            from .qgis_ui.response.media import MediaError, prepare

            if data is not None:
                payload, name = data, "signature.png"
            else:
                try:
                    payload, name = prepare(kind, path)
                except MediaError as exc:
                    raise ApiError(str(exc)) from None
            if len(payload) > spec["max_bytes"]:
                raise ApiError("This file is larger than the upload limit.")
            slot = client.media_presign(
                project_id, kind=kind, file_name=name, content_type=spec["content_type"], size=len(payload)
            )
            client.s3_post(
                slot["url"], slot.get("fields") or {}, file_name=name, content_type=spec["content_type"], data=payload
            )
            return {"url": f"{slot['url']}{slot['key']}", "key": slot["key"], "name": name}, payload

        def done(value, exc):
            if self.c.session is not session:
                # The form was closed or another feature's opened (the window
                # reset its upload count): never file the upload there.
                return
            if window is not None:
                window.media_busy(-1)
            if exc is not None:
                self.c.plugin._handle_call_error(exc)
                finished(None, self.c.plugin._error_text(exc))
                return
            answer, payload = value
            if kind in ("image", "signature"):
                pixmap = QPixmap()
                if pixmap.loadFromData(payload):
                    self.keep_preview(answer["key"], pixmap)
            finished(answer, None)

        self.c.plugin._run("Geodit: uploading a file", work, done)


class FeatureFormController:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.window = None
        self.services = FormServices(self)
        self.target: Optional[FeatureTarget] = None
        self.session: Optional[FormSession] = None
        self.meta: Optional[ResponseMeta] = None
        self.form: Optional[Form] = None
        self._form_cache: Dict[Tuple[str, int, int], Tuple[float, dict]] = {}
        self._team_cache: Dict[Tuple[str, int], Dict[int, str]] = {}
        # (project, layer, feature) → a response created here, until the pull
        # brings the feature's new `gd_ans_id`.
        self._created: Dict[Tuple[int, int, int], int] = {}
        self._load_seq = 0
        self._saving = False
        self._saving_target: Optional[FeatureTarget] = None  # the feature whose save is running
        self._guarding = False  # the Save / Discard / Cancel prompt is up
        self._view_only_notice = ""
        self._view_only_caps = None  # the permissions a save was refused under (403)
        self._not_found_notice = ""

    # ================================================================ window
    def ensure_window(self):
        if self.window is None:
            from .qgis_ui.response.window import FeatureFormWindow

            # Its own window, owned by the QGIS main window: on top of it, gone with it.
            self.window = FeatureFormWindow(self, self.plugin.iface.mainWindow())
        return self.window

    def window_geometry(self) -> str:
        return self.plugin.config.form_geometry

    def save_window_geometry(self, text: str) -> None:
        self.plugin.config.form_geometry = text

    def unload(self) -> None:
        self.session = None
        self.target = None
        self.services.cancel_pick()
        if self.window is not None:
            self.window.dismiss()  # hidden now: the deferred delete may come late
            self.window.deleteLater()
            self.window = None

    def project_id(self) -> Optional[int]:
        return self.target.project_id if self.target is not None else None

    def notify(self, text: str, kind: str = "info") -> None:
        """A note about the open form: in its window while the form is on
        screen (it may cover QGIS's message bar), else in the message bar."""
        window = self.window
        if window is not None and window.showing_form():
            window.show_notice(text, kind)
            return
        levels = {"warning": Qgis.MessageLevel.Warning, "success": Qgis.MessageLevel.Success}
        self.plugin._message(text, levels.get(kind, Qgis.MessageLevel.Info))

    # ================================================================ open
    def open_feature(self, layer, fid: int) -> None:
        plugin = self.plugin
        project = plugin.project
        if project is None or plugin.user_id is None:
            plugin._message("Open a Geodit project in the Geodit panel first.", Qgis.MessageLevel.Info)
            return
        info = plugin.layers.feature_info(layer, fid)
        if info is None:
            plugin._message("Pick a feature on one of the project's Geodit layers.", Qgis.MessageLevel.Info)
            return
        if plugin.layers.is_orphaned(layer):
            plugin._message(ORPHANED_FORM_TEXT, Qgis.MessageLevel.Info)
            return
        if info["gd_id"] is None:
            plugin._message(
                "This feature hasn't been uploaded yet — save your edits and let it sync, then open its form.",
                Qgis.MessageLevel.Warning,
            )
            return
        if info["form_id"] is None:
            plugin._message("No form is attached to this layer.", Qgis.MessageLevel.Info)
            return
        ans_id = info["gd_ans_id"] or self._created.get((project.id, info["shp_id"], info["gd_id"]))
        if ans_id is None and not project.data.can_edit:
            plugin._message("No response has been collected for this feature yet.", Qgis.MessageLevel.Info)
            return
        target = FeatureTarget(
            project_id=project.id,
            shp_id=info["shp_id"],
            fid=info["fid"],
            layer_id=info["layer_id"],
            layer_name=info["layer_name"],
            gd_id=info["gd_id"],
            ans_id=ans_id,
            form_id=info["form_id"],
            attributes=info["attributes"],
            unsaved_edits=bool(info["unsaved_edits"]),
            geometry=info["geometry"],
            crs=info["crs"],
        )
        if self._is_open(target):
            # Clicked again on the map to come back to it: the answers stay as they are.
            self.window.present(activate=self.window.isHidden() or self.window.isMinimized())
            return

        def go() -> None:
            if plugin.project is None or plugin.project.id != target.project_id:
                return  # the project closed while the prompt was up
            self.target = target
            self._view_only_notice = ""
            window = self.ensure_window()
            # The click came from the map: the keyboard stays there unless the window only appears now.
            window.present(activate=window.isHidden() or window.isMinimized())
            plugin.refresh_caps_if_older(CAPS_MAX_AGE_S)
            self.load()

        self.guard(go)

    def _is_open(self, target: FeatureTarget) -> bool:
        """This feature's form is the one in the window, loaded."""
        shown = self.target
        return (
            self.window is not None
            and self.session is not None
            and shown is not None
            and (shown.project_id, shown.layer_id, shown.fid, shown.gd_id, shown.ans_id)
            == (target.project_id, target.layer_id, target.fid, target.gd_id, target.ans_id)
        )

    def _leave_upload(self, parent=None) -> bool:
        """A file is still uploading: True to leave the form without it."""
        if self.window is None or self.session is None or not self.window.media_uploading():
            return True
        answer = QMessageBox.question(
            parent or self.window,
            "A file is still uploading",
            "A file is still uploading for this form. Leave the form anyway? The file won't be added.",
        )
        return answer == QMessageBox.StandardButton.Yes

    def guard(self, then: Callable[[], None], *, parent=None) -> None:
        """Run ``then`` once the open form's changes are saved or discarded:
        before another feature opens, the window closes, the project is left,
        the user signs out or QGIS quits. ``parent`` owns the prompt when it is
        asked from outside the form window."""
        if self._guarding:
            return  # the prompt is already up
        window, session = self.window, self.session
        if window is None or session is None:
            then()
            return
        self._guarding = True
        try:
            choice = self._ask_to_leave(session, parent)
        finally:
            self._guarding = False
        if choice == "save":
            window.present()  # a save that fails its checks has to be seen
            self.submit(after=then)
        elif choice == "leave":
            then()

    def _ask_to_leave(self, session: FormSession, parent) -> str:
        """``"leave"``, ``"save"`` or ``"stay"`` for the open form."""
        uploading = self.window is not None and self.window.media_uploading()
        if not self._leave_upload(parent):
            return "stay"
        if self.session is not session:
            return "stay"  # the form went away under the prompt (the project closed)
        if self._saving or not session.is_dirty():
            return "leave"
        box = QMessageBox(parent or self.window)
        box.setWindowTitle("Unsaved changes")
        if uploading:
            # Leaving without the file: a save would wait for it, so it isn't offered.
            box.setText("The feature form has unsaved changes. Discard them?")
            save = None
        else:
            box.setText("The feature form has unsaved changes. Save them first?")
            save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if self.session is not session:
            return "stay"
        clicked = box.clickedButton()
        if save is not None and clicked is save:
            return "save"
        return "leave" if clicked is discard else "stay"

    def window_closed(self) -> None:
        """The form window was closed: nothing is open any more."""
        self.session = None
        self.target = None
        self.services.cancel_pick()
        if self.window is not None:
            self.window.show_nothing_open()

    def allow_exit(self) -> bool:
        """QGIS is quitting. False keeps it open: the form has unsaved answers
        and the user chose Cancel — or Save, which has to finish first."""
        left: List[bool] = []
        self.guard(lambda: left.append(True), parent=self.plugin.iface.mainWindow())
        return bool(left)

    # ================================================================ load
    def load(self, *, after_save: bool = False) -> None:
        """Read the form and the stored answers, then show them. ``after_save``:
        the form stays on screen, still busy, and comes back on the same page."""
        target = self.target
        project = self.plugin.project
        if target is None or project is None or self.window is None:
            return
        self._load_seq += 1
        seq = self._load_seq
        self.session = None
        caps = project.data
        server = self.plugin.base_url
        form_key = (server, target.project_id, target.form_id)
        cached = self._form_cache.get(form_key)
        cached_payload = cached[1] if cached and time.time() - cached[0] < FORM_CACHE_S else None
        need_team = caps.can_view and target.ans_id is not None and (server, target.project_id) not in self._team_cache
        view = self.window.current_view() if after_save else None
        if not after_save:
            self.window.show_loading(self._header(None))

        def work(client):
            payload = cached_payload or client.form_data(target.project_id, target.form_id)
            items: List[dict] = []
            rows: Optional[List[dict]] = None
            if target.ans_id is not None:
                items = client.ans_data_list(target.project_id, target.form_id, [target.ans_id])
                if caps.can_view:
                    try:
                        rows = client.ans_rows(target.project_id, target.form_id, [target.ans_id])
                    except (PermissionDenied, NotFound, ValidationFailed):
                        rows = None
            team = None
            if need_team:
                try:
                    team = {}
                    for row in client.team_list_basic(target.project_id):
                        member = _int_or_none(row.get("member_id"))
                        if member is not None:
                            team[member] = str(row.get("name") or row.get("username") or f"User {member}")
                except (PermissionDenied, NotFound):
                    team = None
            latest: Dict[int, int] = {}
            if target.ans_id is None:
                form = parse_form(payload)
                for question in form.questions():
                    if question.q_type == QType.ID and is_schema_unique_id(question.attributes):
                        try:
                            latest[question.id] = client.latest_id(target.project_id, target.form_id, question.id)
                        except (ApiError, ValueError):
                            pass  # a failed hint falls back to the configured base
            return LoadResult(payload, items, rows, team, latest)

        def done(result: Optional[LoadResult], exc) -> None:
            if seq != self._load_seq or self.window is None or self.target is not target:
                return
            if exc is not None:
                self.plugin._handle_call_error(exc)
                if isinstance(exc, NotFound):
                    message = "This layer's form wasn't found on the server. Sync, then try again."
                else:
                    message = f"Couldn't load the form. {self.plugin._error_text(exc)}"
                self.window.show_error(self._header(None), message, self.load)
                return
            if cached_payload is None:  # fetched now: a cache hit keeps its age
                self._form_cache[form_key] = (time.time(), result.form_payload)
            if result.team is not None:
                self._team_cache[(server, target.project_id)] = result.team
            self._show(result, view)

        self.plugin._run("Geodit: loading the feature form", work, done)

    def _show(self, result: LoadResult, view: Optional[Tuple[Any, int]] = None) -> None:
        """``view``: the page tab and scroll position to come back to, after a save."""
        target = self.target
        project = self.plugin.project
        form = parse_form(result.form_payload)
        if target.ans_id is None and result.latest:
            # Create: each schema-built id continues from the latest submitted
            # counter (patched into a copy of the form, as the web does).
            form = copy.deepcopy(form)
            for question in form.questions():
                latest = result.latest.get(question.id, 0)
                if latest > 0:
                    question.attributes["unique_value"] = latest + 1
        self.form = form
        self.meta = None
        self._not_found_notice = ""
        if target.ans_id is not None and result.rows is not None:
            row = next((r for r in result.rows if _int_or_none(r.get("id")) == target.ans_id), None)
            if row is None:
                self._not_found_notice = (
                    "This response wasn't found on the server — it may have been deleted. Sync to refresh the layer."
                )
            else:
                self.meta = ResponseMeta(
                    status=_int_or_none(row.get("status")),
                    surveyor_id=_int_or_none(row.get("surveyor_id")),
                    surveyed_on=_format_instant(row.get("surveyed_on")),
                    edited_on=_format_instant(row.get("edited_on")),
                    verifier_id=_int_or_none(row.get("verifier_id")),
                    verified_on=_format_instant(row.get("verified_on")),
                )
        initial = ans_items_to_answers(result.items, form) if target.ans_id is not None else None
        self.session = FormSession(
            form,
            initial_answers=initial,
            feature_attributes=target.attributes,
            options=self._options(project.data if project else None, target),
        )
        if view is not None:
            for i, tab in enumerate(self.session.tabs):
                if tab.key == view[0]:
                    self.session.tab_index = i
        self.window.show_form(self.session, self._header(self.meta))
        if view is not None:
            self.window.scroll_to(view[1])
            self.window.show_notice(f"Saved {time.strftime('%H:%M')}", "success")

    def _options(self, caps, target: FeatureTarget) -> SessionOptions:
        if caps is None:
            return SessionOptions(read_only=True, submit_changed_only=target.ans_id is not None)
        can_edit = caps.can_edit and not self._view_only_notice
        return SessionOptions(
            read_only=not can_edit,
            allow_read_only_edit=can_edit and caps.edit_read_only,
            can_duplicate_entry=caps.duplicate_entry,
            can_remove_entry=caps.remove_entry,
            show_hidden_pages=caps.show_hidden_pages,
            show_hidden_questions=caps.show_hidden_questions,
            submit_changed_only=target.ans_id is not None,
        )

    def _header(self, meta: Optional[ResponseMeta]):
        from .qgis_ui.response.window import FormHeader

        target = self.target
        project = self.plugin.project
        caps = project.data if project else None
        if target is None:
            return FormHeader()
        notices: List[Tuple[str, str]] = []
        if self._view_only_notice:
            notices.append(("warning", self._view_only_notice))
        if self._not_found_notice:
            notices.append(("warning", self._not_found_notice))
        if target.ans_id is None and target.unsaved_edits:
            notices.append(
                (
                    "info",
                    "Downloads are paused for this layer while it has unsaved edits, so a response added elsewhere "
                    "may not show here yet. Save your edits to refresh it.",
                )
            )
        team = self._team_cache.get((self.plugin.base_url, target.project_id), {})

        def who(member: Optional[int]) -> str:
            if member is None:
                return ""
            return team.get(member, f"User {member}")

        editable = bool(caps and caps.can_edit and not self._view_only_notice)
        verifier_id = meta.verifier_id if meta else None
        return FormHeader(
            feature_id=target.gd_id,
            ans_id=target.ans_id,
            layer=target.layer_name,
            view_only=not editable,
            show_meta=bool(meta is not None and caps is not None and caps.can_view and target.ans_id is not None),
            status=meta.status if meta else None,
            status_editable=editable,
            surveyor=who(meta.surveyor_id) if meta else "",
            surveyor_id=meta.surveyor_id if meta else None,
            edited_on=meta.edited_on if meta else "",
            # As on the web: "Verified by" names whoever set the status, else "Awaiting review".
            verifier=who(verifier_id),
            verifier_id=verifier_id,
            verified_on=meta.verified_on if meta else "",
            notices=notices,
        )

    # ================================================================ save
    def submit(self, after: Optional[Callable[[], None]] = None) -> None:
        session, target, window = self.session, self.target, self.window
        if session is None or target is None or window is None:
            return
        if self._saving:
            if self._saving_target is not target:  # this form's own save shows "Saving…"
                window.set_submit_error("The previous form is still saving — try again in a moment.")
            return
        if window.media_uploading():
            window.set_submit_error("Wait for the file to finish uploading, then save.")
            return
        prepared = session.prepare_submit(probe_unique=True)
        window.refresh()
        if prepared is None:
            return
        if target.ans_id is not None and prepared.payload.is_empty():
            if after is not None:
                after()
            else:
                self.notify("No changes to save.")
            return
        self._saving = True
        self._saving_target = target
        window.set_submit_error("")
        window.set_saving(True)
        batches = batch_unique_groups(prepared.unique_groups)
        payload = prepared.payload
        answers = [_to_wire(a) for a in payload.answers]

        def work(client):
            hits = []
            for batch in batches:
                try:
                    verdict = client.ans_unique_constraint(
                        target.project_id,
                        form_id=target.form_id,
                        items=_to_wire(batch_body(batch)),
                        exclude_ans_id=target.ans_id,
                    )
                except SessionExpired:
                    raise
                except NetworkError:
                    raise ProbeFailed(UNIQUE_CHECK_UNAVAILABLE) from None
                except ApiError as exc:
                    detail = exc.message or ""
                    raise ProbeFailed(
                        f"Couldn't verify that these answers are unique — {detail}"
                        if detail
                        else UNIQUE_CHECK_UNAVAILABLE
                    ) from None
                if not verdict.ok:
                    hits.extend(flagged_items(batch, verdict.groups))
            if hits:
                return "unique", hits
            if target.ans_id is None:
                result = client.ans_create(
                    target.project_id,
                    form_id=target.form_id,
                    feature_id=target.gd_id,
                    feature_shp_id=target.shp_id,
                    answers=answers,
                )
            else:
                result = client.ans_update(
                    target.project_id, target.ans_id, form_id=target.form_id, answers=answers, clear=payload.clear
                )
            return "saved", result

        def done(value, exc) -> None:
            self._saving = False
            self._saving_target = None
            saved = exc is None and value[0] == "saved"
            if saved:
                # Recorded even when another feature is open by now: the link
                # must survive until the pull brings the feature's gd_ans_id.
                new_id = _int_or_none((value[1] or {}).get("ans_id")) or target.ans_id
                if target.ans_id is None and new_id is not None:
                    self._created[(target.project_id, target.shp_id, target.gd_id)] = new_id
                self.plugin.request_sync("saved")
                target.ans_id = new_id
            if self.window is None or self.target is not target or self.session is not session:
                if saved:
                    self._saved_elsewhere(target)
                else:
                    self._unsaved_elsewhere(target, exc)
                return
            if exc is not None:
                window.set_saving(False)
                self._save_failed(exc, target, session)
                return
            if not saved:
                window.set_saving(False)
                session.apply_unique_hits(value[1])
                window.refresh()
                return
            self.session = None
            if after is not None:
                after()
            if self.target is target and self.window is not None and not self.window.isHidden():
                # Still this feature's form: it stays on screen, busy, until the
                # stored answers are back — then on the same page, with "Saved".
                self.load(after_save=True)
            else:
                self._saved_elsewhere(target)

        self.plugin._run("Geodit: saving the feature form", work, done)

    def _saved_elsewhere(self, target: FeatureTarget) -> None:
        """The save landed after its form was closed or moved on to another feature."""
        self.plugin._message(f"Saved the response for feature {target.gd_id}.", Qgis.MessageLevel.Success, 3)

    def _unsaved_elsewhere(self, target: FeatureTarget, exc: Optional[BaseException]) -> None:
        """The save failed after its form was closed or moved on to another
        feature: say so — the typed answers went with the form."""
        plugin = self.plugin
        where = f"feature {target.gd_id} in {target.layer_name}"
        if exc is None:
            reason = "some answers already exist in another response"
        elif isinstance(exc, ProbeFailed):
            reason = str(exc).rstrip(".")
        else:
            plugin._handle_call_error(exc)
            if isinstance(exc, SessionExpired):
                return  # the sign-in page says so
            if isinstance(exc, Conflict) and target.ans_id is None:
                linked = exc.linked_ans_id
                if linked is not None:
                    self._created[(target.project_id, target.shp_id, target.gd_id)] = linked
                plugin.request_sync("saved")
                reason = "someone else added a response to it first"
            elif isinstance(exc, (NetworkError, ServerError)) and target.ans_id is None:
                # A create may still have landed (no idempotency key): sync, never retry.
                plugin.request_sync("saved")
                plugin._message(
                    f"Couldn't confirm the new response for {where}: it may still have gone through. "
                    "Reopen the feature after the sync to check.",
                    Qgis.MessageLevel.Warning,
                    0,
                )
                return
            else:
                reason = plugin._error_text(exc).rstrip(".")
        plugin._message(
            f"The response for {where} wasn't saved: {reason}. Open the feature again to redo your changes.",
            Qgis.MessageLevel.Warning,
            0,
        )

    def _save_failed(self, exc: BaseException, target: FeatureTarget, session: FormSession) -> None:
        plugin, window = self.plugin, self.window
        if isinstance(exc, ProbeFailed):
            window.set_submit_error(str(exc))
            return
        plugin._handle_call_error(exc)
        if isinstance(exc, SessionExpired):
            window.set_submit_error("Your session has expired. Sign in again, then save.")
            return
        if isinstance(exc, Conflict) and target.ans_id is None:
            linked = exc.linked_ans_id
            plugin.request_sync("saved")
            if linked is not None:
                self._created[(target.project_id, target.shp_id, target.gd_id)] = linked
            answer = QMessageBox.question(
                window,
                "This feature already has a response",
                "Someone added a response to this feature while you were filling this in, so yours wasn't saved. "
                "Open the existing response? (Copy anything you typed first — it will be replaced.)",
            )
            if answer == QMessageBox.StandardButton.Yes and linked is not None:
                target.ans_id = linked
                self.session = None
                self.load()
            else:
                window.set_submit_error("Not saved: this feature already has a response.")
            return
        if isinstance(exc, PermissionDenied):
            self._view_only_notice = "Your role can't edit responses in this project (Settings → Web access)."
            self._view_only_caps = plugin.project.data if plugin.project is not None else None
            plugin._refresh_caps()
            session.set_options(
                SessionOptions(
                    read_only=True,
                    show_hidden_pages=session.options.show_hidden_pages,
                    show_hidden_questions=session.options.show_hidden_questions,
                )
            )
            window.set_header(self._header(self.meta))
            window.refresh()
            window.set_submit_error("Not saved: " + (exc.message or "permission denied."))
            return
        if isinstance(exc, PlanExpired):
            window.set_submit_error(
                "The project owner's plan has expired. Recharge to continue — your answers stay here."
            )
            return
        if isinstance(exc, NotFound) and target.ans_id is not None:  # 404, or 410 deleted
            self._created.pop((target.project_id, target.shp_id, target.gd_id), None)
            plugin.request_sync("saved")
            window.set_submit_error("This response was deleted on the server. Sync to refresh the layer.")
            return
        if isinstance(exc, (ValidationFailed, ApiError)) and not isinstance(exc, (NetworkError, ServerError)):
            details = "; ".join(m for msgs in exc.errors.values() for m in msgs) if exc.errors else ""
            window.set_submit_error(f"Couldn't save: {exc.message}" + (f" ({details})" if details else ""))
            return
        # A network or server failure: a create may still have landed (no
        # idempotency key), so never retry it — sync, and let the next open
        # find the link.
        if target.ans_id is None:
            plugin.request_sync("saved")
            window.set_submit_error(
                "Couldn't confirm the save. It may still have gone through — the layer will sync; reopen the "
                "feature before trying again."
            )
        else:
            window.set_submit_error("Couldn't save your changes. Please try again.")

    # ================================================================ status
    def set_status(self, status: int) -> None:
        target, meta, project = self.target, self.meta, self.plugin.project
        if target is None or target.ans_id is None or meta is None or project is None or not project.data.can_edit:
            return
        if meta.status == status:
            return
        previous = (meta.status, meta.verifier_id, meta.verified_on)
        meta.status = status
        self.window.set_header(self._header(meta))

        def done(result, exc) -> None:
            if self.target is not target or self.meta is not meta:
                return
            if exc is not None:
                self.plugin._handle_call_error(exc)
                meta.status, meta.verifier_id, meta.verified_on = previous
                self.window.set_header(self._header(meta))
                if isinstance(exc, NotFound):  # 404, or 410: deleted on the web
                    self._created.pop((target.project_id, target.shp_id, target.gd_id), None)
                    self.plugin.request_sync("saved")
                    self.notify("This response was deleted on the server. Sync to refresh the layer.", "warning")
                    return
                self.notify(f"Couldn't change the status: {self.plugin._error_text(exc)}", "warning")
                if isinstance(exc, PermissionDenied):
                    self.plugin._refresh_caps()
                return
            meta.status = _int_or_none((result or {}).get("status")) or status
            meta.verifier_id = _int_or_none((result or {}).get("verifier_id"))
            meta.verified_on = _format_instant((result or {}).get("verified_on"))
            self.window.set_header(self._header(meta))
            self.notify(f"Marked as {STATUS_TEXT.get(meta.status, 'Pending')}", "success")

        self.plugin._run(
            "Geodit: setting the response status",
            lambda client: client.ans_status(target.project_id, target.ans_id, form_id=target.form_id, status=status),
            done,
        )

    # ================================================================ lifecycle
    def project_changed(self, project) -> None:
        """The permissions were re-read: apply the Web access switches in place."""
        target, session = self.target, self.session
        if target is None or project is None or project.id != target.project_id:
            return
        if self._view_only_notice and project.data.can_edit and project.data != self._view_only_caps:
            self._view_only_notice = ""  # edit access changed since the refusal, and allows it now
        if session is not None:
            before = session.options.read_only
            session.set_options(self._options(project.data, target))
            if self.window is not None:
                self.window.set_header(self._header(self.meta))
                self.window.refresh()
                if not before and session.options.read_only and session.is_dirty():
                    self.notify(
                        "You can no longer edit responses in this project — your changes can't be saved.", "warning"
                    )

    def project_closed(self) -> None:
        if self.session is not None and self.session.is_dirty():
            self.plugin._message("Unsaved changes in the feature form were discarded.", Qgis.MessageLevel.Warning)
        self._load_seq += 1
        self.session = None
        self.target = None
        self.meta = None
        self._created.clear()
        self.services.clear_previews()
        self.services.cancel_pick()
        if self.window is not None:
            self.window.show_nothing_open()
            self.window.dismiss()  # a form without its project has nothing to show

    def status_text(self) -> str:
        return STATUS_TEXT.get(self.meta.status, "") if self.meta and self.meta.status else ""
