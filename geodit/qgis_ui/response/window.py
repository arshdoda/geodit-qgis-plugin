"""The "Feature form" window — the Qt twin of geodit-ui's map Data sheet.

A view over one ``FormSession``: it renders what the session says (tabs,
visible questions and options, values, errors, rule banners) and forwards every
edit to it; the controller (``geodit.responses``) loads, saves and talks to the
server. States: a message (no form / no response), loading, error with Retry,
and the form.

Its own window, owned by the QGIS main window and not modal: the map stays
clickable, so the next feature (or a location answer's point) is picked with
the form open. One window serves every feature, remembers its size and uses
all of its width. The header is the web answer modal's: the title, the
feature / response / layer ids (click one to copy it), then Surveyor, Verified
by and Status side by side. Unlike the web sheet, Save is on every page — it
still checks all of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QByteArray, QEvent, QRectF, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QKeySequence, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...forms.model import CHOICE_QTYPES
from ...forms.rules import is_author_hidden_question
from ...forms.session import FormSession
from ..theme import Theme, font, line_icon, stylesheet
from ..widgets import Avatar, Banner, Columns, EmptyState, Pill, divider, label
from .editors import EditorContext, QuestionEditor, create_editor
from .stepper import DUPLICATE, REMOVE, Step, StepperRow

STATUS_OPTIONS = ((1, "Pending"), (3, "Approved"), (2, "Rejected"))  # the web's order
DEFAULT_SIZE = QSize(720, 860)  # the form column plus its margins; the height is capped to the screen
MIN_SIZE = QSize(440, 420)
PAGE_MESSAGE, PAGE_LOADING, PAGE_ERROR, PAGE_FORM = range(4)


@dataclass
class FormHeader:
    """What the header shows: the web answer modal's feature line (feature id,
    response id or "new response", layer) and, for a stored response, who
    surveyed and verified it and its status."""

    feature_id: Optional[int] = None
    ans_id: Optional[int] = None
    layer: str = ""
    view_only: bool = False
    show_meta: bool = False
    status: Optional[int] = None
    status_editable: bool = False
    surveyor: str = ""
    surveyor_id: Optional[int] = None
    edited_on: str = ""
    verifier: str = ""
    verifier_id: Optional[int] = None
    verified_on: str = ""
    notices: List[Tuple[str, str]] = field(default_factory=list)  # (kind, text)


def _mono(text: str = "") -> QLabel:
    lbl = label(text, kind="muted", wrap=False)
    mono = font(0.92)
    mono.setFamily("monospace")
    lbl.setFont(mono)
    return lbl


class _CopyId(QToolButton):
    """A monospace id that copies itself on click, with a copy mark that turns
    into a tick for a moment (the web's ``CopyableMono``)."""

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("variant", "copy")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)  # the mark after the id
        self.setIconSize(QSize(12, 12))
        mono = font(0.92)
        mono.setFamily("monospace")
        self.setFont(mono)
        self._value = ""
        self._copied = False
        self._theme = Theme.current()
        self.clicked.connect(self._copy)
        self._paint()

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value
        self._copied = False
        self.setText(value)
        self.setToolTip(f"Copy {value}")
        self._paint()

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._value)
        self._copied = True
        self._paint()
        QTimer.singleShot(1400, self._uncopy)  # a method: dropped with the button

    def _uncopy(self) -> None:
        self._copied = False
        self._paint()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self._paint()

    def _paint(self) -> None:
        if self._copied:
            self.setIcon(line_icon("tick", self._theme.tone("success")[1], 12))
        else:
            self.setIcon(line_icon("copy", self._theme.muted, 12))


class _ViewOnlyBadge(QFrame):
    """The web's neutral "View only" tag: an outlined pill with an eye."""

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("badge", "true")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 2, 10, 2)
        row.setSpacing(6)
        self._icon = QLabel()
        self._icon.setFixedSize(14, 14)
        row.addWidget(self._icon)
        row.addWidget(label("View only", wrap=False))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.apply_theme(Theme.current())

    def apply_theme(self, theme: Theme) -> None:
        self._icon.setPixmap(line_icon("eye", theme.muted, 14).pixmap(14, 14))


class _StatusMark(QWidget):
    """The web's avatar-sized status mark: a tick (approved) or a cross
    (rejected) on a tint; a dash in a dashed ring (pending); a dashed ring
    with "—" for someone not there yet (no verifier)."""

    def __init__(self, mode: str = "pending") -> None:
        super().__init__()
        self._mode = mode
        self._theme = Theme.current()
        self.setFixedSize(36, 36)

    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.update()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        t = self._theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        tone = {"approved": "success", "rejected": "danger"}.get(self._mode)
        if tone:
            bg, fg = t.tone(tone)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawEllipse(rect)
            icon = line_icon("tick" if tone == "success" else "x", fg, 16)
            icon.paint(painter, self.rect().adjusted(10, 10, -10, -10))
        else:
            pen = QPen(t.border, 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(rect)
            if self._mode == "empty":
                painter.setPen(t.muted)
                painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), "—")
            else:
                line_icon("minus", t.muted, 16).paint(painter, self.rect().adjusted(10, 10, -10, -10))
        painter.end()


def _meta_cell(mark: QWidget, caption: str, *lines: QLabel) -> QWidget:
    """A Surveyor / Verified by / Status cell: the mark, then a caption over its lines."""
    cell = QWidget()
    row = QHBoxLayout(cell)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(10)
    row.addWidget(mark, 0, Qt.AlignmentFlag.AlignVCenter)
    text = QVBoxLayout()
    text.setSpacing(1)
    cap = label(caption.upper(), kind="muted", wrap=False)
    cap_font = font(0.8, bold=True)
    cap_font.setLetterSpacing(cap_font.SpacingType.PercentageSpacing, 106)
    cap.setFont(cap_font)
    text.addWidget(cap)
    for line in lines:
        text.addWidget(line)
    row.addLayout(text, 1)
    return cell


def _primary(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("variant", "primary")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setAutoDefault(False)
    return btn


def _secondary(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("variant", "secondary")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setAutoDefault(False)
    return btn


def _icon_button(name: str, tooltip: str, text: str = "") -> QToolButton:
    btn = QToolButton()
    btn.setProperty("variant", "icon")
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setIconSize(QSize(16, 16))
    btn.setProperty("icon_name", name)
    if text:
        btn.setText(text)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    return btn


class _StatusCombo(QComboBox):
    """The response status applies the moment it is picked, so it must never
    change by accident: it takes focus by click only (in a window it would
    otherwise be focused first, and typing "a" would approve), and the wheel
    scrolls the page, not the value."""

    def __init__(self) -> None:
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        event.ignore()


class FeatureFormWindow(QDialog):
    def __init__(self, controller, parent=None) -> None:
        # A normal window (it can be maximised), as QGIS's attribute table is.
        super().__init__(parent, Qt.WindowType.Window)
        # Before any window call: changing the flags already sends ``changeEvent``.
        self.c = controller
        self.theme = Theme.current()
        self._theme_key: Optional[tuple] = None
        self._theming = False
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, False)
        self.setObjectName("GeoditFeatureFormWindow")
        self.setModal(False)
        self.setMinimumSize(MIN_SIZE)
        self._placed = False  # the saved size and place are applied once
        self.session: Optional[FormSession] = None
        self._editors: Dict[int, QuestionEditor] = {}
        self._built_tab: Optional[Tuple[int, int]] = None
        self._saving = False
        self._status_editable = False  # the header lets the status be picked
        self._busy_media = 0
        self._retry: Optional[Callable[[], None]] = None
        self._scroll_target = 0
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(0)
        self._refresh_timer.timeout.connect(self.refresh)

        self._root = QWidget()
        self._root.setObjectName("GeoditRoot")
        column = QWidget()
        outer = QVBoxLayout(column)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)
        outer.addWidget(self._build_header())
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_message())
        self.stack.addWidget(self._build_loading())
        self.stack.addWidget(self._build_error())
        self.stack.addWidget(self._build_form())
        outer.addWidget(self.stack, 1)
        root = QVBoxLayout(self._root)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(column)  # the whole width of the window
        frame = QVBoxLayout(self)
        frame.setContentsMargins(0, 0, 0, 0)
        frame.addWidget(self._root)
        self._apply_theme(force=True)
        self.show_nothing_open()

    # ================================================================ theme
    def changeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if not self._theming and event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            QTimer.singleShot(0, self._apply_theme)

    def _apply_theme(self, force: bool = False) -> None:
        theme = Theme.current()
        key = (theme.window.name(), theme.text.name(), theme.surface.name())
        if (key == self._theme_key and not force) or self._theming:
            return
        self._theming = True
        try:
            self.theme = theme
            self._theme_key = key
            self._root.setStyleSheet(stylesheet(theme))
            for widget in self._root.findChildren(QWidget):
                apply = getattr(widget, "apply_theme", None)
                if callable(apply):
                    apply(theme)
                name = widget.property("icon_name") if isinstance(widget, QToolButton) else None
                if name:
                    widget.setIcon(line_icon(name, theme.text, 16))
            _, warn = theme.tone("warning")
            _, danger = theme.tone("danger")
            self.rule_error.setStyleSheet(f"color: {warn.name()};")
            self.submit_error.setStyleSheet(f"color: {danger.name()};")
        finally:
            self._theming = False

    # ================================================================ window
    def present(self, *, activate: bool = True) -> None:
        """Show the window on top — at its saved size and place the first time.
        ``activate`` off leaves the keyboard with the map (the next feature click)."""
        if not self._placed:
            self._placed = True
            self._restore_geometry()
        if self.isMinimized():  # back as it was: maximised or not
            if self.isMaximized():
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self.show()
        self.raise_()
        if activate:
            self.activateWindow()

    def _restore_geometry(self) -> None:
        saved = self.c.window_geometry()
        if isinstance(saved, str) and saved and self.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii"))):
            return
        screen = self.screen()
        room = screen.availableGeometry().height() if screen is not None else DEFAULT_SIZE.height()
        self.resize(DEFAULT_SIZE.width(), min(DEFAULT_SIZE.height(), int(room * 0.85)))

    def dismiss(self) -> None:
        """Hide without asking: the project closed, or the plugin unloads."""
        self.hide()

    def reject(self) -> None:
        """Every way out — the title bar's close button, Esc, ``close()`` —
        comes here (``QDialog``), and asks about unsaved changes first."""
        self.c.guard(self._closed_by_user)

    def _closed_by_user(self) -> None:
        self.dismiss()
        self.c.window_closed()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not event.spontaneous():  # closed or dismissed, not minimised by the window manager
            self.c.save_window_geometry(bytes(self.saveGeometry().toBase64()).decode("ascii"))
        super().hideEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Escape and self.c.services.picking():
            self.c.services.cancel_pick()  # Esc ends the map pick, not the form
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Save):
            self._save()
            event.accept()
            return
        super().keyPressEvent(event)

    def showing_form(self) -> bool:
        """The form is on screen — where a note about it can be read."""
        return self.isVisible() and not self.isMinimized() and self.stack.currentIndex() == PAGE_FORM

    # ================================================================ build
    def _build_header(self) -> QWidget:
        """The web answer modal's header: title, the feature line, then
        Surveyor, Verified by and Status — side by side when the window is
        wide enough, one under the other when not."""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.title = label("Feature form", scale=1.2, bold=True, wrap=False)
        self.view_only = _ViewOnlyBadge()
        self.view_only.hide()
        row.addWidget(self.title)
        row.addWidget(self.view_only, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        layout.addLayout(row)

        # feature 2817 · ans 90412 · Parcels (or "new response")
        self.ids = QWidget()
        ids = QHBoxLayout(self.ids)
        ids.setContentsMargins(0, 0, 0, 0)
        ids.setSpacing(6)
        self.feature_copy = _CopyId()
        self.ans_caption = _mono("ans")
        self.ans_copy = _CopyId()
        self.layer_dot = _mono("·")
        self.layer_name = _mono()
        for widget in (_mono("feature"), self.feature_copy, _mono("·"), self.ans_caption, self.ans_copy):
            ids.addWidget(widget)
        ids.addWidget(self.layer_dot)
        ids.addWidget(self.layer_name)
        ids.addStretch(1)
        layout.addWidget(self.ids)

        self.status = _StatusCombo()
        for value, text in STATUS_OPTIONS:
            self.status.addItem(text, value)
        self.status.setToolTip("The status changes as soon as you pick it")
        self.status.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)  # compact, as the web's
        self.status.setMinimumWidth(148)
        self.status.activated.connect(self._status_picked)
        self.surveyor_avatar = Avatar(36)
        self.surveyor = label("", wrap=False)
        self.edited = label("", kind="muted", wrap=False)
        self.edited.setFont(font(0.92))
        self.verifier_avatar = Avatar(36)
        self.verifier_none = _StatusMark("empty")
        verifier_mark = QWidget()
        vm = QHBoxLayout(verifier_mark)
        vm.setContentsMargins(0, 0, 0, 0)
        vm.addWidget(self.verifier_avatar)
        vm.addWidget(self.verifier_none)
        self.verifier = label("", wrap=False)
        self.verified = label("", kind="muted", wrap=False)
        self.verified.setFont(font(0.92))
        self.status_mark = _StatusMark()
        self.meta = Columns(
            [
                _meta_cell(self.surveyor_avatar, "Surveyor", self.surveyor, self.edited),
                _meta_cell(verifier_mark, "Verified by", self.verifier, self.verified),
                _meta_cell(self.status_mark, "Status", self.status),
            ],
            min_width=180,
            spacing=12,
        )
        self.meta.hide()
        layout.addWidget(self.meta)
        self.notices = QVBoxLayout()
        self.notices.setSpacing(6)
        layout.addLayout(self.notices)
        layout.addWidget(divider())
        return box

    def ids_text(self) -> str:
        """The feature line as it reads ("feature 2817 · ans 90412 · Parcels")."""
        parts = ["feature " + self.feature_copy.value()]
        parts.append("ans " + self.ans_copy.value() if not self.ans_copy.isHidden() else self.ans_caption.text())
        if self.layer_name.text():
            parts.append(self.layer_name.text())
        return " · ".join(parts)

    def _build_message(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 24, 0, 0)
        self.message = EmptyState("database", "")
        layout.addWidget(self.message)
        layout.addStretch(1)
        return page

    def _build_loading(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 24, 0, 0)
        bar = QProgressBar()
        bar.setProperty("thin", "true")
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        layout.addWidget(bar)
        self.loading_text = label("Loading form…", kind="muted")
        self.loading_text.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.loading_text)
        layout.addStretch(1)
        return page

    def _build_error(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        self.error_banner = Banner("error")
        layout.addWidget(self.error_banner)
        self.retry = _secondary("Retry")
        self.retry.clicked.connect(lambda: self._retry() if self._retry else None)
        layout.addWidget(self.retry, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def _build_form(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.stepper = StepperRow(self._step_picked, self._step_duplicate, self._step_remove)
        layout.addWidget(self.stepper)

        self.banners = QVBoxLayout()
        self.banners.setSpacing(6)
        layout.addLayout(self.banners)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.heading = label("", scale=1.1, bold=True)
        self.page_hidden = Pill("Hidden from surveyors", "neutral")
        self.page_hidden.hide()
        self.count = label("", kind="muted", wrap=False)
        # A repeating page's entries, in words (the page tabs carry the same two actions as ⧉ and ×).
        self.add_entry = _icon_button("plus", "Add another entry of this page", "Add another")
        self.remove_entry = _icon_button("x", "Remove this entry", "Remove")
        self.add_entry.clicked.connect(self._add_entry_clicked)
        self.remove_entry.clicked.connect(self._remove_entry_clicked)
        self.add_entry.hide()
        self.remove_entry.hide()
        head.addWidget(self.heading, 1)
        head.addWidget(self.page_hidden, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.count, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.add_entry, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.remove_entry, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.questions = QWidget()
        self.questions_layout = QVBoxLayout(self.questions)
        self.questions_layout.setContentsMargins(0, 0, 4, 0)
        self.questions_layout.setSpacing(10)
        self.questions_layout.addStretch(1)
        self.scroll.setWidget(self.questions)
        layout.addWidget(self.scroll, 1)
        self.empty_page = label("No questions on this page.", kind="muted")
        self.empty_page.hide()
        layout.addWidget(self.empty_page)

        # What just happened, right above the buttons: "Saved 14:05", a status
        # that didn't apply, where to click for a map pick.
        self.notice = Banner("success")
        layout.addWidget(self.notice)
        self.submit_error = label("")
        self.submit_error.hide()
        self.rule_error = label("")
        self.rule_error.hide()
        layout.addWidget(self.submit_error)
        layout.addWidget(self.rule_error)
        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.back = _secondary("Back")
        self.next = _secondary("Next")
        self.save_btn = _primary("Save changes")
        self.back.clicked.connect(self._back)
        self.next.clicked.connect(self._next)
        self.save_btn.clicked.connect(self._save)
        self.saving = QProgressBar()
        self.saving.setProperty("thin", "true")
        self.saving.setRange(0, 0)
        self.saving.setTextVisible(False)
        self.saving.hide()
        self.counter = label("", kind="muted", wrap=False)
        footer.addWidget(self.back)
        footer.addWidget(self.next)
        footer.addSpacing(6)
        footer.addWidget(self.counter, 0, Qt.AlignmentFlag.AlignVCenter)
        footer.addStretch(1)
        footer.addWidget(self.save_btn)
        layout.addWidget(self.saving)
        layout.addLayout(footer)
        return page

    # ================================================================ header
    def set_header(self, header: FormHeader) -> None:
        if header.feature_id is None:
            self.ids.hide()
            self.setWindowTitle("Feature form[*]")
        else:
            self.ids.show()
            self.feature_copy.set_value(str(header.feature_id))
            if header.ans_id is not None:
                self.ans_caption.setText("ans")
                self.ans_copy.set_value(str(header.ans_id))
                self.ans_copy.show()
                which = f"ans {header.ans_id}"
            else:
                self.ans_caption.setText("new response")
                self.ans_copy.hide()
                which = "new response"
            self.layer_name.setText(header.layer)
            self.layer_dot.setVisible(bool(header.layer))
            # "[*]" is where Qt marks unsaved changes (``setWindowModified``).
            self.setWindowTitle(f"Feature form — feature {header.feature_id} · {which}[*]")
        self.view_only.setVisible(header.view_only)
        self.meta.setVisible(header.show_meta)
        if header.show_meta:
            index = self.status.findData(header.status) if header.status is not None else -1
            self.status.setCurrentIndex(index if index >= 0 else 0)
            self.status_mark.set_mode({3: "approved", 2: "rejected"}.get(header.status or 0, "pending"))
            self.surveyor_avatar.set_member(header.surveyor or "?", header.surveyor_id)
            self.surveyor.setText(header.surveyor or "—")
            self.edited.setText(f"Edited {header.edited_on or '—'}")
            verified = bool(header.verifier)
            self.verifier_avatar.setVisible(verified)
            self.verifier_none.setVisible(not verified)
            if verified:
                self.verifier_avatar.set_member(header.verifier, header.verifier_id)
            self.verifier.setText(header.verifier if verified else "Awaiting review")
            self.verifier.setProperty("kind", "" if verified else "muted")
            self.verifier.style().unpolish(self.verifier)
            self.verifier.style().polish(self.verifier)
            self.verified.setText(f"on {header.verified_on}" if verified and header.verified_on else "")
            self.verified.setVisible(verified and bool(header.verified_on))
        self._status_editable = header.show_meta and header.status_editable
        self._lock_status()
        self._set_banners(self.notices, header.notices)

    def _set_banners(self, layout: QVBoxLayout, items: List[Tuple[str, str]]) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()  # deleted later: not drawn over the page meanwhile
                widget.deleteLater()
        for kind, text in items:
            if not text:
                continue
            banner = Banner(kind)
            banner.apply_theme(self.theme)
            layout.addWidget(banner)  # first: shown without a parent, it would flash up as a window
            banner.set_message(text, kind)

    def _status_picked(self, index: int) -> None:
        value = self.status.itemData(index)
        self.status.clearFocus()  # the keyboard must not keep changing it
        if value is not None:
            self.c.set_status(int(value))

    # ================================================================ states
    def show_nothing_open(self) -> None:
        self.show_message("No feature selected", "Click a feature on a Geodit layer to open its form.")

    def show_message(self, title: str, text: str = "", header: Optional[FormHeader] = None) -> None:
        self._clear_form()
        self.set_header(header or FormHeader())
        self.message.set_text(title, text)
        self.stack.setCurrentIndex(PAGE_MESSAGE)

    def show_loading(self, header: FormHeader, text: str = "Loading form…") -> None:
        self._clear_form()
        self.set_header(header)
        self.loading_text.setText(text)
        self.stack.setCurrentIndex(PAGE_LOADING)

    def show_error(self, header: FormHeader, message: str, retry: Optional[Callable[[], None]]) -> None:
        self._clear_form()
        self.set_header(header)
        self._retry = retry
        self.error_banner.set_message(message, "error")
        self.retry.setVisible(retry is not None)
        self.stack.setCurrentIndex(PAGE_ERROR)

    def show_form(self, session: FormSession, header: FormHeader) -> None:
        self._clear_form()
        self.session = session
        self.set_header(header)
        self.stack.setCurrentIndex(PAGE_FORM)
        if not session.has_content:
            self.show_message("This form has no questions yet.", header=header)
            return
        self.refresh()

    def _clear_form(self) -> None:
        self.session = None
        self._built_tab = None
        for editor in self._editors.values():
            editor.setParent(None)
            editor.deleteLater()
        self._editors = {}
        self.submit_error.hide()
        self.rule_error.hide()
        self.clear_notice()
        self._set_banners(self.banners, [])
        self._saving = False
        self._busy_media = 0  # an upload still running belongs to the form that's gone
        self.saving.hide()
        self.stepper.reset()
        self.setWindowModified(False)

    # ================================================================ notes
    def show_notice(self, text: str, kind: str = "success") -> None:
        """One line about what just happened; the next edit clears it."""
        self.notice.set_message(text, kind)

    def clear_notice(self) -> None:
        self.notice.set_message("")

    # ================================================================ form
    def schedule_refresh(self) -> None:
        self._refresh_timer.start()

    def _on_change(self, ques_id: int, value, page_key: int) -> None:
        if self.session is None:
            return
        self.session.set_answer(ques_id, value, page_key)
        self.submit_error.hide()
        self.clear_notice()
        self.schedule_refresh()

    def media_busy(self, delta: int) -> None:
        self._busy_media = max(0, self._busy_media + delta)
        self.schedule_refresh()

    def media_uploading(self) -> bool:
        return self._busy_media > 0

    def current_view(self) -> Tuple[Optional[Tuple[int, int]], int]:
        """The open page tab and how far it is scrolled — to come back to after a save."""
        tab = self.session.current_tab if self.session is not None else None
        return (tab.key if tab is not None else None), self.scroll.verticalScrollBar().value()

    def scroll_to(self, value: int) -> None:
        """Once the page is laid out (its height isn't known before)."""
        self._scroll_target = value
        QTimer.singleShot(0, self._apply_scroll)  # a method: dropped with the window

    def _apply_scroll(self) -> None:
        self.scroll.verticalScrollBar().setValue(self._scroll_target)

    def _steps(self) -> List[Step]:
        """One pill per tab, as the web's ``StepperRow`` shows them: errors are
        counted on passed tabs only, a rule problem shows once the form shows
        it, and a passed tab without either reads as done."""
        session = self.session
        errors = session.error_count_by_tab()
        problems = session.rule_problem_tab_keys() if session.show_rule_problems else set()
        active = session.safe_tab_index
        steps = []
        for i, tab in enumerate(session.tabs):
            n = errors.get(tab.key, 0)
            is_active = i == active
            rule = not is_active and not n and tab.key in problems
            action = None
            if tab.is_for and tab.page_key == 1 and session.can_add_entry(tab.page_id):
                action = DUPLICATE
            elif tab.is_for and tab.page_key >= 2 and session.can_remove_entry(tab.page_id, tab.page_key):
                action = REMOVE
            steps.append(
                Step(
                    index=i,
                    page_id=tab.page_id,
                    page_key=tab.page_key,
                    name=tab.step_name,
                    author_hidden=tab.author_hidden,
                    errors=n,
                    rule_problem=rule,
                    active=is_active,
                    done=i < active and not n and not rule,
                    action=action,
                )
            )
        return steps

    def refresh(self) -> None:
        session = self.session
        if session is None or self.stack.currentIndex() != PAGE_FORM:
            return
        tabs = session.tabs
        if not tabs:
            return
        busy = self._saving or self._busy_media > 0
        index = session.safe_tab_index
        self.stepper.set_steps(self._steps(), disabled=busy)
        self.stepper.setVisible(session.show_stepper)
        tab = tabs[index]
        if self._built_tab != tab.key:
            self._build_tab(tab)
        self.heading.setText(tab.heading)
        self.page_hidden.setVisible(tab.author_hidden)
        self.add_entry.setVisible(session.can_add_entry(tab.page_id))
        self.remove_entry.setVisible(session.can_remove_entry(tab.page_id, tab.page_key))
        self.add_entry.setEnabled(not busy)
        self.remove_entry.setEnabled(not busy)

        shown = session.shown_questions(tab.page_id, tab.page_key)
        shown_by_id = {q.id: q for q in shown}
        banners, highlighted = session.banners()
        problems = session.show_rule_problems
        for ques_id, editor in self._editors.items():
            presented = shown_by_id.get(ques_id)
            editor.setVisible(presented is not None)
            if presented is None:
                continue
            original = session.ques_by_id[ques_id]
            editor.update_state(
                presented=presented,
                value=session.value(ques_id, tab.page_key),
                options=session.visible_options(original, tab.page_key) if original.q_type in CHOICE_QTYPES else None,
                disabled=session.question_disabled(original) or self._saving,
                error=session.displayed_error(presented, tab.page_key),
                highlight=problems and ques_id in highlighted,
                author_hidden=is_author_hidden_question(original),
            )
        self.count.setText(f"{len(shown)} question{'s' if len(shown) != 1 else ''}" if shown else "")
        self.empty_page.setVisible(not shown)
        self.scroll.setVisible(bool(shown))
        items = []
        if problems:
            for banner in banners:
                text = banner.message
                refs = ", ".join(
                    f"Page {r.page_position} Q{r.question_position}: {r.title}" for r in banner.question_refs
                )
                if refs:
                    text += "\n" + refs
                if banner.note:
                    text += "\n" + banner.note
                items.append(("warning", text))
        self._set_banners(self.banners, items)

        self.rule_error.setText(session.rule_submit_error or "")
        self.rule_error.setVisible(session.rule_submit_error_shown)
        # Paging and saving are separate: Back / Next only move, Save is always there.
        paged = len(tabs) > 1
        self.counter.setText(f"Page {index + 1} of {len(tabs)}" if paged else "")
        self.back.setVisible(paged)
        self.next.setVisible(paged)
        self.back.setEnabled(index > 0 and not busy)
        self.next.setEnabled(not session.is_last_tab and not busy)
        read_only = session.options.read_only
        creating = not session.options.submit_changed_only
        self.save_btn.setVisible(not read_only)
        self.save_btn.setText("Saving…" if self._saving else ("Save response" if creating else "Save changes"))
        self.save_btn.setEnabled(not busy and not read_only and (creating or not session.build_payload().is_empty()))
        self.save_btn.setToolTip("Wait for the file to finish uploading" if self._busy_media else "Ctrl+S")
        self.setWindowModified(not read_only and session.is_dirty())

    def _build_tab(self, tab) -> None:
        session = self.session
        for editor in self._editors.values():
            editor.setParent(None)
            editor.deleteLater()
        self._editors = {}
        page = session.page_by_id.get(tab.page_id)
        ctx = EditorContext(self._on_change, self.c.services, self.theme)
        position = 0
        for question in page.ques_list if page else []:
            editor = create_editor(question, tab.page_key, ctx)
            editor.apply_theme(self.theme)
            self.questions_layout.insertWidget(position, editor)
            position += 1
            self._editors[question.id] = editor
        self._built_tab = tab.key
        self.scroll.verticalScrollBar().setValue(0)

    def _step_picked(self, index: int) -> None:
        if self.session is None or self._busy_media or self._saving:
            return
        self.session.go_to_tab(index)
        self.schedule_refresh()

    def _back(self) -> None:
        if self.session is not None:
            self.session.back()
            self.refresh()

    def _next(self) -> None:
        if self.session is None or self._saving or self._busy_media:
            return
        self.session.next()
        self.refresh()

    def _save(self) -> None:
        """The Save button and Ctrl+S, from any page: the controller checks
        every page and the form moves to the first problem."""
        session = self.session
        if session is None or session.options.read_only or self._saving or self._busy_media:
            return
        self.c.submit()
        self.refresh()

    def _step_duplicate(self, page_id: int) -> None:
        """⧉ on a repeating page: a new blank entry, which becomes the current tab."""
        if self.session is None or self._busy_media or self._saving:
            return
        if self.session.add_entry(page_id):
            self.schedule_refresh()

    def _step_remove(self, page_id: int, page_key: int) -> None:
        """× on an added entry: the later entries renumber, so the page is rebuilt."""
        if self.session is None or self._busy_media or self._saving:
            return
        if self.session.remove_entry(page_id, page_key):
            self._built_tab = None
            self.schedule_refresh()

    def _add_entry_clicked(self) -> None:
        tab = self.session.current_tab if self.session is not None else None
        if tab is not None:
            self._step_duplicate(tab.page_id)

    def _remove_entry_clicked(self) -> None:
        tab = self.session.current_tab if self.session is not None else None
        if tab is not None:
            self._step_remove(tab.page_id, tab.page_key)

    # ================================================================ saving
    def set_saving(self, saving: bool) -> None:
        self._saving = saving
        self.saving.setVisible(saving)
        self._lock_status()
        self.refresh()

    def _lock_status(self) -> None:
        """A status applies the moment it is picked: never while one, or the
        form, is saving (the save's reload would show the status it read)."""
        self.status.setEnabled(self._status_editable and not self._saving)

    def set_submit_error(self, text: str) -> None:
        self.submit_error.setText(text or "")
        self.submit_error.setVisible(bool(text))
