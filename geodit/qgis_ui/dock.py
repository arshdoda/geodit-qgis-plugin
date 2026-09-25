"""The Geodit dock: sign in → pick a map project → sync status.

A passive view: it renders state and forwards user actions to the controller
(``GeoditPlugin``). Built in code (no .ui files) against ``qgis.PyQt`` with
fully scoped enums so it runs on Qt5 (QGIS 3) and Qt6 (QGIS 4). Colours,
fonts and icons come from ``theme.py``; the building blocks from
``widgets.py``.

Contract with the controller and tests: the ``stack`` page indices below,
``status_label`` (its text is read back), ``code_error`` (shown/hidden, never
replaced) and the public ``show_*`` / ``set_*`` methods.
"""

from __future__ import annotations

import time
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from qgis.gui import QgsFilterLineEdit, QgsPasswordLineEdit
from qgis.PyQt.QtCore import QEvent, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QIcon, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..config import RememberedUser
from ..core.projects import HiddenCounts, ProjectInfo
from ..sync.context import SyncReport
from .theme import Theme, device_scale, font, line_icon, logo_pixmap, role_tone, stylesheet
from .widgets import (
    DOT_ROLE,
    PILL_ROLE,
    SUBTITLE_ROLE,
    SUBTITLE_TONE_ROLE,
    TITLE_ROLE,
    Avatar,
    Banner,
    CardDelegate,
    EmptyState,
    Pill,
    SegmentedControl,
    StatusDot,
    divider,
    label,
    section_label,
)

PAGE_SIGN_IN, PAGE_TWO_FACTOR, PAGE_PROJECTS, PAGE_PROJECT = range(4)
_PAYLOAD = Qt.ItemDataRole.UserRole
_INTERVALS = (1, 2, 5, 10, 15, 30, 60)
_HEADLINES = {
    "idle": "Not synced yet",
    "syncing": "Syncing…",
    "ok": "Up to date",
    "pending": "Changes waiting to upload",
    "paused": "Uploads paused",
    "readonly": "View only",
    "error": "Sync problem",
}
Note = Union[str, Tuple[str, str]]  # text (a warning) or (kind, text)


def _primary(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("variant", "primary")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setDefault(False)
    btn.setAutoDefault(False)
    return btn


def _secondary(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("variant", "secondary")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setAutoDefault(False)
    return btn


def _field(placeholder: str = "", widget: Optional[QLineEdit] = None) -> QLineEdit:
    edit = widget if widget is not None else QLineEdit()
    edit.setPlaceholderText(placeholder)
    edit.setProperty("field", "true")
    return edit


def _tool(variant: str, tooltip: str = "", text: str = "") -> QToolButton:
    btn = QToolButton()
    btn.setProperty("variant", variant)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setAutoRaise(True)
    if text:
        btn.setText(text)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    return btn


def _busy_bar() -> QProgressBar:
    bar = QProgressBar()
    bar.setProperty("thin", "true")
    bar.setRange(0, 0)
    bar.setTextVisible(False)
    bar.hide()
    return bar


def _page(margins: int = 14, spacing: int = 10) -> Tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return page, layout


def _scroll(page: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(page)
    return area


def _avatar_icon(name: str, theme: Theme, size: int = 26, scale: float = 2.0) -> QIcon:
    avatar = Avatar(size)
    avatar.apply_theme(theme)
    avatar.set_name(name)
    pix = QPixmap(round(size * scale), round(size * scale))
    pix.setDevicePixelRatio(scale)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    avatar.render(painter)
    painter.end()
    avatar.deleteLater()
    return QIcon(pix)


class GeoditDock(QDockWidget):
    def __init__(self, controller, parent=None) -> None:
        super().__init__("Geodit", parent)
        self.setObjectName("GeoditDock")
        self.c = controller
        self.theme = Theme.current()
        self._theme_key: Optional[tuple] = None
        self._theming = False
        self._projects: List[ProjectInfo] = []
        self._user_label = ""
        self._state = "idle"
        self._icon_buttons: List[Tuple[QToolButton, str, int]] = []  # (button, icon name, size)
        self._area_blocked = False
        self._shown_project: Optional[ProjectInfo] = None
        self._shown_notes: Optional[list] = None  # last notes rendered (skip identical re-renders)
        self._shown_issues: Optional[tuple] = None

        self._root = QWidget()
        self._root.setObjectName("GeoditRoot")
        outer = QVBoxLayout(self._root)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.stack.addWidget(_scroll(self._build_sign_in()))
        self.stack.addWidget(_scroll(self._build_two_factor()))
        self.stack.addWidget(self._build_projects())
        self.stack.addWidget(_scroll(self._build_project()))
        self.setWidget(self._root)
        self._apply_theme(force=True)

    # ============================================================ theme
    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if not self._theming and event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
        ):
            QTimer.singleShot(0, self._apply_theme)

    def _apply_theme(self, force: bool = False) -> None:
        """Re-style when the QGIS theme (application palette) changed. The key
        comes from the application palette, not ours — our stylesheet changing
        our palette must not trigger another round."""
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
            for delegate in (self._project_delegate, self._issue_delegate):
                delegate.theme = theme
            self.project_list.viewport().update()
            self.issues.viewport().update()
            for button, name, size in self._icon_buttons:
                button.setIcon(line_icon(name, theme.text if name != "back" else theme.accent_text, size))
            scale = device_scale(self)
            self.logo.setPixmap(logo_pixmap(44, scale))
            self.no_issues_icon.setPixmap(line_icon("check", theme.tone("success")[1], 16).pixmap(16, 16))
            self.access_icon.setPixmap(line_icon("lock", theme.muted, 14).pixmap(14, 14))
            self.shield.setPixmap(line_icon("shield", theme.accent_text, 34).pixmap(34, 34))
            self._refresh_account_icon()
        finally:
            self._theming = False

    def _icon_button(self, button: QToolButton, name: str, size: int = 16) -> QToolButton:
        button.setIconSize(QSize(size, size))
        self._icon_buttons.append((button, name, size))
        return button

    # ============================================================ sign in
    def _build_sign_in(self) -> QWidget:
        page, layout = _page(margins=18, spacing=10)

        self.logo = QLabel()
        self.logo.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(6)
        layout.addWidget(self.logo)
        title = label("Sign in to Geodit", scale=1.35, bold=True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)
        subtitle = label("For project owners, admins and editors.", kind="muted")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(subtitle)
        layout.addSpacing(6)

        # "Continue as …" for a remembered sign-in.
        self.remembered_box = QWidget()
        rb = QVBoxLayout(self.remembered_box)
        rb.setContentsMargins(0, 0, 0, 0)
        rb.setSpacing(10)
        card = QFrame()
        card.setProperty("card", "true")
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        self.remembered_avatar = Avatar(34)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.remembered_label = label("", bold=True, wrap=False)
        self.remembered_server = label("", kind="muted", wrap=False)
        names.addWidget(self.remembered_label)
        names.addWidget(self.remembered_server)
        self.continue_btn = _primary("Continue")
        self.continue_btn.clicked.connect(lambda: self.c.continue_session())
        row.addWidget(self.remembered_avatar)
        row.addLayout(names, 1)
        row.addWidget(self.continue_btn)
        rb.addWidget(card)
        or_row = QHBoxLayout()
        or_row.setSpacing(8)
        or_row.addWidget(divider(), 1)
        or_row.addWidget(label("or use another account", kind="muted", wrap=False))
        or_row.addWidget(divider(), 1)
        rb.addLayout(or_row)
        layout.addWidget(self.remembered_box)

        self.id_kind = SegmentedControl([("username", "Username"), ("phone", "Phone number")])
        self.id_kind.changed.connect(self._id_kind_changed)
        layout.addWidget(self.id_kind)
        self.username = _field("Username")
        layout.addWidget(self.username)
        self.phone_row = QWidget()
        pr = QHBoxLayout(self.phone_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(6)
        self.country_code = _field("+91")
        self.country_code.setText("+91")
        self.country_code.setFixedWidth(64)
        self.phone = _field("Phone number")
        pr.addWidget(self.country_code)
        pr.addWidget(self.phone, 1)
        layout.addWidget(self.phone_row)
        self.password = _field("Password", QgsPasswordLineEdit())
        if hasattr(self.password, "setShowLockIcon"):
            self.password.setShowLockIcon(False)
        layout.addWidget(self.password)
        for edit in (self.username, self.phone, self.password):
            edit.returnPressed.connect(self._submit_sign_in)

        self.remember = QCheckBox("Stay signed in on this computer")
        self.remember.setToolTip(
            "Keeps you signed in for up to 30 days. The sign-in is stored in QGIS's encrypted "
            "password store, which may ask for the QGIS master password."
        )
        layout.addWidget(self.remember)
        self.sign_in_btn = _primary("Sign in")
        self.sign_in_btn.clicked.connect(self._submit_sign_in)
        layout.addWidget(self.sign_in_btn)
        self.sign_in_busy = _busy_bar()
        layout.addWidget(self.sign_in_busy)
        self.sign_in_error = Banner("error")
        layout.addWidget(self.sign_in_error)
        layout.addStretch(1)

        self._id_kind_changed()
        return page

    def _id_kind_changed(self, *_args) -> None:
        phone = self.id_kind.value() == "phone"
        self.username.setVisible(not phone)
        self.phone_row.setVisible(phone)

    def _submit_sign_in(self) -> None:
        self.sign_in_error.set_message("")
        phone = self.id_kind.value() == "phone"
        self.c.sign_in(
            username=None if phone else self.username.text().strip(),
            phone=self.phone.text().strip() if phone else None,
            country_code=self.country_code.text().strip() or "+91",
            password=self.password.text(),
            remember=self.remember.isChecked(),
        )

    # ---------------------------------------------------------- two factor
    def _build_two_factor(self) -> QWidget:
        page, layout = _page(margins=18, spacing=10)
        back = self._icon_button(_tool("back", "Back to sign in", "Back"), "back", 14)
        back.clicked.connect(lambda: self.c.cancel_two_factor())
        row = QHBoxLayout()
        row.addWidget(back)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addSpacing(8)
        self.shield = QLabel()
        self.shield.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.shield)
        title = label("Two-step verification", scale=1.3, bold=True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)
        hint = label("Enter the 6-digit code from your authenticator app, or one of your backup codes.", kind="muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(hint)
        layout.addSpacing(4)
        self.code = _field("123456")
        self.code.setProperty("code", "true")
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code_font = font(1.45, bold=True)
        code_font.setLetterSpacing(code_font.SpacingType.AbsoluteSpacing, 3)
        self.code.setFont(code_font)
        self.code.returnPressed.connect(self._submit_code)
        layout.addWidget(self.code)
        self.verify_btn = _primary("Verify")
        self.verify_btn.clicked.connect(self._submit_code)
        layout.addWidget(self.verify_btn)
        self.code_busy = _busy_bar()
        layout.addWidget(self.code_busy)
        self.code_error = Banner("error")
        layout.addWidget(self.code_error)
        layout.addStretch(1)
        return page

    def _submit_code(self) -> None:
        self.code_error.set_message("")
        self.c.verify_code(self.code.text().strip())

    # ------------------------------------------------------------- projects
    def _build_projects(self) -> QWidget:
        page, layout = _page(margins=14, spacing=10)
        header = QHBoxLayout()
        header.setSpacing(6)
        title = label("Projects", scale=1.3, bold=True, wrap=False)
        self.projects_count = Pill("", "neutral")
        header.addWidget(title)
        header.addWidget(self.projects_count, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)
        self.refresh_btn = self._icon_button(_tool("icon", "Refresh the project list"), "refresh", 16)
        self.refresh_btn.clicked.connect(lambda: self.c.refresh_projects())
        header.addWidget(self.refresh_btn)
        self.account_btn = _tool("icon", "Account")
        self.account_btn.setIconSize(QSize(26, 26))
        self.account_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        account = QMenu(self.account_btn)
        self.account_header = account.addAction("")
        self.account_header.setEnabled(False)
        account.addSeparator()
        refresh = account.addAction("Refresh projects")
        refresh.triggered.connect(lambda _checked=False: self.c.refresh_projects())
        sign_out = account.addAction("Sign out")
        sign_out.triggered.connect(lambda _checked=False: self.c.sign_out())
        self.account_btn.setMenu(account)
        header.addWidget(self.account_btn)
        layout.addLayout(header)
        self.projects_busy = _busy_bar()
        layout.addWidget(self.projects_busy)

        self.search = _field("Search projects", QgsFilterLineEdit())
        self.search.setShowSearchIcon(True)
        self.search.setShowClearButton(True)
        self.search.textChanged.connect(self._filter_projects)
        layout.addWidget(self.search)

        self.project_list = QListWidget()
        self.project_list.setProperty("cards", "true")
        self.project_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.project_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.project_list.setMouseTracking(True)
        self.project_list.setUniformItemSizes(True)
        self.project_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._project_delegate = CardDelegate(self.project_list)
        self.project_list.setItemDelegate(self._project_delegate)
        self.project_list.itemDoubleClicked.connect(lambda _item: self._open_selected())
        self.project_list.itemActivated.connect(lambda _item: self._open_selected())
        self.project_list.currentItemChanged.connect(lambda *_: self._update_open_btn())
        layout.addWidget(self.project_list, 1)
        self.projects_empty = EmptyState("map", "No projects to show")
        layout.addWidget(self.projects_empty, 1)
        self.hidden_note = label("", kind="muted")
        layout.addWidget(self.hidden_note)
        self.open_btn = _primary("Open project")
        self.open_btn.clicked.connect(self._open_selected)
        layout.addWidget(self.open_btn)
        self.projects_error = Banner("error")
        layout.addWidget(self.projects_error)
        return page

    def _refresh_account_icon(self) -> None:
        self.account_btn.setIcon(_avatar_icon(self._user_label, self.theme, 26, device_scale(self)))

    def _filter_projects(self, *_args) -> None:
        needle = self.search.text().strip().casefold()
        shown = 0
        for i in range(self.project_list.count()):
            item = self.project_list.item(i)
            hidden = bool(needle) and needle not in str(item.data(TITLE_ROLE) or "").casefold()
            item.setHidden(hidden)
            shown += 0 if hidden else 1
        has_any = self.project_list.count() > 0
        self.project_list.setVisible(shown > 0)
        self.projects_empty.setVisible(shown == 0)
        if has_any and shown == 0:
            self.projects_empty.set_text("No matching projects", "Try a different search.")
        current = self.project_list.currentItem()
        if current is None or current.isHidden():
            # Prefer a project that can be opened; else the first one shown.
            rows = [i for i in range(self.project_list.count()) if not self.project_list.item(i).isHidden()]
            openable = [i for i in rows if getattr(self._item_project(self.project_list.item(i)), "can_open", False)]
            if openable or rows:
                self.project_list.setCurrentRow((openable or rows)[0])
        self._update_open_btn()

    def _item_project(self, item: Optional[QListWidgetItem]) -> Optional[ProjectInfo]:
        if item is None:
            return None
        pid = item.data(_PAYLOAD)
        return next((p for p in self._projects if p.id == pid), None)

    def _update_open_btn(self) -> None:
        item = self.project_list.currentItem()
        project = self._item_project(item) if item is not None and not item.isHidden() else None
        blocker = project.area_label if project is not None else ""
        self.open_btn.setText(blocker or "Open project")
        self.open_btn.setEnabled(project is not None and project.can_open and not self.projects_busy.isVisible())

    def _open_selected(self) -> None:
        item = self.project_list.currentItem()
        if item is None or item.isHidden():
            return
        project = self._item_project(item)
        if project is not None and project.can_open:
            self.c.open_project(project)

    @staticmethod
    def _project_subtitle(project: ProjectInfo) -> str:
        parts = [project.area_label] if project.area_blocker else [project.access_label]
        if not project.is_owner and project.owner_name:
            parts.append(f"owned by {project.owner_name}")
        return " · ".join(parts)

    # -------------------------------------------------------------- project
    def _build_project(self) -> QWidget:
        page, layout = _page(margins=14, spacing=10)
        top = QHBoxLayout()
        back = self._icon_button(
            _tool("back", "Stop syncing this project and go back to the project list", "Projects"), "back", 14
        )
        back.clicked.connect(lambda: self.c.leave_project())
        top.addWidget(back)
        top.addStretch(1)
        self.more_btn = self._icon_button(_tool("icon", "More actions"), "more", 18)
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more = QMenu(self.more_btn)
        self.full_action = more.addAction("Full re-sync…")
        self.full_action.setToolTip(
            "Download everything in your area again and drop local copies of features that no "
            "longer exist on the server. Unsynced local edits are kept."
        )
        self.full_action.triggered.connect(lambda _checked=False: self.c.full_resync())
        self.discard_action = more.addAction("Discard unsynced changes…")
        self.discard_action.triggered.connect(lambda _checked=False: self.c.discard_local_changes())
        more.addSeparator()
        sign_out = more.addAction("Sign out")
        sign_out.triggered.connect(lambda _checked=False: self.c.sign_out())
        self.more_btn.setMenu(more)
        top.addWidget(self.more_btn)
        layout.addLayout(top)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.project_title = label("", scale=1.3, bold=True)
        self.project_role = Pill("", "neutral")
        title_row.addWidget(self.project_title, 1)
        title_row.addWidget(self.project_role, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(title_row)
        access_row = QHBoxLayout()
        access_row.setSpacing(6)
        self.access_icon = QLabel()
        self.access_icon.setFixedSize(14, 14)
        self.access_label = label("", kind="muted")
        access_row.addWidget(self.access_icon, 0, Qt.AlignmentFlag.AlignTop)
        access_row.addWidget(self.access_label, 1)
        layout.addLayout(access_row)

        card = QFrame()
        card.setProperty("card", "true")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(12, 10, 12, 12)
        cl.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.status_dot = StatusDot()
        self.status_headline = label("", bold=True, wrap=False)
        head.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.status_headline, 1)
        cl.addLayout(head)
        self.status_label = label("", kind="muted")
        cl.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setProperty("thin", "true")
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.hide()
        cl.addSpacing(2)
        cl.addWidget(self.progress)
        self.step_label = label("", kind="muted")
        self.step_label.hide()
        cl.addWidget(self.step_label)
        layout.addWidget(card)

        self.sync_btn = _primary("Sync now")
        self.sync_btn.clicked.connect(lambda: self.c.sync_now())
        layout.addWidget(self.sync_btn)
        self.full_btn = self.full_action  # kept for callers that toggle it

        auto = QHBoxLayout()
        auto.setSpacing(6)
        self.auto_sync = QCheckBox("Sync automatically every")
        self.auto_sync.toggled.connect(lambda on: self.c.set_auto_sync(on))
        self.interval = QComboBox()
        for minutes in _INTERVALS:
            self.interval.addItem(self._interval_text(minutes), minutes)
        self.interval.currentIndexChanged.connect(self._interval_changed)
        auto.addWidget(self.auto_sync)
        auto.addWidget(self.interval)
        auto.addStretch(1)
        layout.addLayout(auto)

        self.notes_box = QWidget()
        self.notes_layout = QVBoxLayout(self.notes_box)
        self.notes_layout.setContentsMargins(0, 0, 0, 0)
        self.notes_layout.setSpacing(6)
        self.notes_box.hide()
        layout.addWidget(self.notes_box)

        issues_head = QHBoxLayout()
        issues_head.setSpacing(6)
        issues_head.addWidget(section_label("Needs attention"))
        self.issues_count = Pill("", "warning")
        issues_head.addWidget(self.issues_count, 0, Qt.AlignmentFlag.AlignVCenter)
        issues_head.addStretch(1)
        layout.addSpacing(4)
        layout.addLayout(issues_head)
        self.issues = QListWidget()
        self.issues.setProperty("cards", "true")
        self.issues.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.issues.setMouseTracking(True)
        self._issue_delegate = CardDelegate(self.issues, subtitle_lines=2)
        self.issues.setItemDelegate(self._issue_delegate)
        self.issues.setToolTip("Double-click to select and zoom to the feature")
        self.issues.itemDoubleClicked.connect(self._zoom_issue)
        self.issues.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.issues.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.issues)
        self.no_issues = QWidget()
        ni = QHBoxLayout(self.no_issues)
        ni.setContentsMargins(0, 0, 0, 0)
        ni.setSpacing(6)
        self.no_issues_icon = QLabel()
        self.no_issues_icon.setFixedSize(16, 16)
        ni.addWidget(self.no_issues_icon)
        ni.addWidget(label("Nothing needs attention.", kind="muted"), 1)
        layout.addWidget(self.no_issues)
        layout.addStretch(1)
        layout.addWidget(divider())
        layout.addWidget(
            label(
                "Only saved edits are synced — use “Save Layer Edits” in QGIS. They upload a few seconds after saving.",
                kind="muted",
            )
        )
        return page

    @staticmethod
    def _interval_text(minutes: int) -> str:
        return "1 hour" if minutes == 60 else f"{minutes} min"

    def _interval_changed(self, *_args) -> None:
        minutes = self.interval.currentData()
        if minutes is not None:
            self.c.set_interval(int(minutes))

    def _set_interval(self, minutes: int) -> None:
        idx = self.interval.findData(int(minutes))
        if idx < 0:
            pos = sum(1 for m in _INTERVALS if m < minutes)
            self.interval.insertItem(pos, self._interval_text(minutes), int(minutes))
            idx = self.interval.findData(int(minutes))
        self.interval.setCurrentIndex(idx)

    def _zoom_issue(self, item: QListWidgetItem) -> None:
        data = item.data(_PAYLOAD)
        if data:
            shp_id, fid, kind = data
            self.c.zoom_to(int(shp_id), int(fid), kind)

    def _set_state(self, state: str, headline: Optional[str] = None) -> None:
        self._state = state
        self.status_dot.set_state(state)
        self.status_headline.setText(headline or _HEADLINES.get(state, ""))

    def _fill_project(self, project: ProjectInfo) -> None:
        self._shown_project = project
        self.project_title.setText(project.name)
        self.project_role.set(project.role_label, role_tone(project.role))
        if self._area_blocked:
            text = "Read-only — no survey area is assigned to you in this project."
        elif not project.can_edit:
            text = "View only — your role can't edit map features in this project (Settings → Page access)."
        elif not project.can_delete:
            text = "You can add and edit features, but not delete them."
        elif project.is_owner:
            text = "Owner · full access."
        else:
            text = "You can add, edit and delete features."
        self.access_label.setText(text)
        self.access_icon.setVisible(self._area_blocked or not project.can_edit)

    # ================================================================ API
    def show_sign_in(self, remembered: Optional[RememberedUser] = None, error: str = "") -> None:
        self.password.clear()
        self.remembered_box.setVisible(remembered is not None)
        if remembered is not None:
            name = remembered.display_name or f"user {remembered.user_id}"
            self.remembered_label.setText(name)
            self.remembered_avatar.set_name(name)
            self.remembered_server.setText("Signed in on this computer")
        self.set_sign_in_error(error)
        self.set_busy(False)
        self.stack.setCurrentIndex(PAGE_SIGN_IN)

    def set_sign_in_error(self, text: str) -> None:
        self.sign_in_error.set_message(text, "error")

    def show_two_factor(self, error: str = "") -> None:
        self.code.clear()
        self.set_code_error(error)
        self.set_busy(False)
        self.stack.setCurrentIndex(PAGE_TWO_FACTOR)
        self.code.setFocus()

    def set_code_error(self, text: str) -> None:
        self.code_error.set_message(text, "error")

    def show_projects(
        self,
        projects: Sequence[ProjectInfo],
        user_label: str,
        error: str = "",
        hidden: Optional[HiddenCounts] = None,
    ) -> None:
        current = self.project_list.currentItem()
        keep = int(current.data(_PAYLOAD)) if current is not None else None
        self._projects = list(projects)
        self._user_label = user_label
        self.account_header.setText(f"Signed in as {user_label}")
        self.account_btn.setToolTip(f"Signed in as {user_label}")
        self._refresh_account_icon()
        self.project_list.clear()
        for p in self._projects:
            item = QListWidgetItem(p.name)
            item.setData(_PAYLOAD, p.id)
            item.setData(TITLE_ROLE, p.name)
            item.setData(SUBTITLE_ROLE, self._project_subtitle(p))
            item.setData(PILL_ROLE, (p.role_label, role_tone(p.role)))
            if p.area_blocker:
                # Listed, but not openable: QGIS syncs only the polygons assigned to you.
                item.setData(DOT_ROLE, "warning")
                item.setData(SUBTITLE_TONE_ROLE, "warning")
                item.setToolTip(
                    f"{p.name}\n{p.area_label}. QGIS syncs only the survey-area polygons assigned to you. "
                    "Assign yourself an area on the web Map page (Assign area), or ask the project owner, "
                    "then refresh this list."
                )
            else:
                item.setToolTip(f"{p.name}\n{p.role_label} · {p.access_label}")
            self.project_list.addItem(item)
            if p.id == keep:
                self.project_list.setCurrentItem(item)
        self.projects_count.set(str(len(self._projects)) if self._projects else "", "neutral")
        hidden = hidden or HiddenCounts()
        if not self._projects:
            if hidden.total:
                self.projects_empty.set_text(
                    "No projects to show", "Your projects are hidden — see the note below for why."
                )
            else:
                self.projects_empty.set_text(
                    "No projects to show",
                    "Map-based projects you own, or where you're an Admin or Editor, appear here.",
                )
        self.hidden_note.setText(self._hidden_text(hidden))
        self.hidden_note.setVisible(bool(hidden.total))
        self._filter_projects()
        self.projects_error.set_message(error, "error")
        self.set_busy(False)
        self.stack.setCurrentIndex(PAGE_PROJECTS)

    @staticmethod
    def _hidden_text(hidden: HiddenCounts) -> str:
        parts = []
        if hidden.expired:
            n = hidden.expired
            parts.append(f"{n} project{'s' if n != 1 else ''} hidden: the owner's plan has expired.")
        if hidden.no_access:
            n = hidden.no_access
            parts.append(f"{n} project{'s' if n != 1 else ''} hidden: the Map page is turned off for your role.")
        return "\n".join(parts)

    def show_project(self, project: ProjectInfo, *, auto_sync: bool, interval: int) -> None:
        self._area_blocked = False  # the controller says otherwise right after, if so
        self._fill_project(project)
        self.auto_sync.blockSignals(True)
        self.interval.blockSignals(True)
        self.auto_sync.setChecked(auto_sync)
        self._set_interval(interval)
        self.auto_sync.blockSignals(False)
        self.interval.blockSignals(False)
        self.set_status("Waiting for the first sync.", [], state="idle")
        self.set_issues(None)
        self.stack.setCurrentIndex(PAGE_PROJECT)

    def update_project(self, project: ProjectInfo) -> None:
        """The open project's details (permissions) changed; keep status and issues."""
        self._fill_project(project)

    def set_busy(self, busy: bool) -> None:
        for widget in (self.sign_in_btn, self.verify_btn, self.continue_btn, self.refresh_btn):
            widget.setEnabled(not busy)
        for bar in (self.sign_in_busy, self.code_busy, self.projects_busy):
            bar.setVisible(busy)
        self.sign_in_btn.setText("Signing in…" if busy else "Sign in")
        self.verify_btn.setText("Verifying…" if busy else "Verify")
        self._update_open_btn()

    def set_syncing(self, syncing: bool, quiet: bool = False) -> None:
        """``quiet``: a background tick (timer, after a save) — only the button
        says so; the status card and the progress bar stay as they are."""
        self.progress.setVisible(syncing and not quiet)
        self.progress.setValue(0)
        self.step_label.setVisible(False)
        self.sync_btn.setEnabled(not syncing)
        self.sync_btn.setText("Syncing…" if syncing else "Sync now")
        self.full_action.setEnabled(not syncing and not self._area_blocked)
        self.discard_action.setEnabled(not syncing)
        if syncing and not quiet:
            self._set_state("syncing")  # the detail line keeps the last result meanwhile

    def set_area_blocked(self, blocked: bool) -> None:
        """No survey area is assigned: the layers are read-only, and a Full
        re-sync would have nothing to download."""
        self._area_blocked = bool(blocked)
        self.full_action.setEnabled(self.sync_btn.isEnabled() and not self._area_blocked)
        if self._shown_project is not None:
            self._fill_project(self._shown_project)

    def set_progress(self, pct: float) -> None:
        """``QgsTask.progressChanged`` (queued to the main thread)."""
        if not self.progress.isVisible():
            return
        self.progress.setValue(max(0, min(100, int(pct))))
        step = ""
        getter = getattr(self.c, "sync_step_text", None)
        if callable(getter):
            step = getter() or ""
        self.step_label.setText(step)
        self.step_label.setVisible(bool(step))

    def set_status(
        self, text: str, notes: Iterable[Note], state: Optional[str] = None, headline: Optional[str] = None
    ) -> None:
        self.status_label.setText(text)
        if state is not None:
            self._set_state(state, headline)
        notes = [("warning", n) if isinstance(n, str) else tuple(n) for n in notes]
        if notes == self._shown_notes:
            return  # unchanged: keep the banners (no flicker every tick)
        self._shown_notes = notes
        while self.notes_layout.count():
            item = self.notes_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        count = 0
        for note in notes:
            kind, message = ("warning", note) if isinstance(note, str) else note
            if not message:
                continue
            banner = Banner(kind)
            banner.apply_theme(self.theme)
            banner.set_message(message, kind)
            self.notes_layout.addWidget(banner)
            count += 1
        self.notes_box.setVisible(count > 0)

    def set_issues(self, report: Optional[SyncReport]) -> None:
        signature = None
        if report is not None:
            signature = tuple(
                (lr.shp_id, tuple(lr.held), tuple(lr.rejected), lr.discarded_total) for lr in report.layers.values()
            )
        if signature is not None and signature == self._shown_issues:
            return  # unchanged: keep the list and the user's selection
        self._shown_issues = signature
        self.issues.clear()
        count = 0
        if report is not None:
            for lr in report.layers.values():
                for fid, reason in lr.held:
                    self._add_issue(lr.name, lr.shp_id, fid, reason, "warning")
                    count += 1
                for fid, code, detail in lr.rejected:
                    self._add_issue(lr.name, lr.shp_id, fid, f"{detail} ({code}) — edit the feature to retry", "danger")
                    count += 1
                if lr.discarded_total:
                    item = QListWidgetItem(lr.name)
                    item.setData(TITLE_ROLE, lr.name)
                    item.setData(
                        SUBTITLE_ROLE,
                        f"{lr.discarded_total} edit(s) kept in “{lr.name} — discarded edits”",
                    )
                    item.setData(DOT_ROLE, "neutral")
                    item.setToolTip(
                        "Edits the server couldn't take (the feature was deleted there, or you discarded them)"
                    )
                    self.issues.addItem(item)
                    count += 1
        self.issues_count.set(str(count) if count else "", "warning")
        self.issues.setVisible(count > 0)
        self.no_issues.setVisible(count == 0)
        if count:
            # Sized to its rows (the page scrolls), up to about six of them.
            rows = sum(self.issues.sizeHintForRow(i) for i in range(min(count, 6)))
            self.issues.setFixedHeight(rows + 2 * self.issues.frameWidth() + 2)

    def _add_issue(self, layer_name: str, shp_id: int, fid: int, text: str, tone: str) -> None:
        item = QListWidgetItem(f"{layer_name} · feature {fid}")
        item.setData(TITLE_ROLE, f"{layer_name} · feature {fid}")
        item.setData(SUBTITLE_ROLE, text)
        item.setData(DOT_ROLE, tone)
        item.setData(_PAYLOAD, (shp_id, fid, "layer"))
        item.setToolTip(text)
        self.issues.addItem(item)


def describe_time(ts: Optional[float]) -> str:
    if ts is None:
        return "never"
    return time.strftime("%H:%M", time.localtime(ts))
