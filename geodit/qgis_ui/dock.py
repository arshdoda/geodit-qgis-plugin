"""The Geodit dock: sign in → pick a map project → sync status.

A passive view: it renders state and forwards user actions to the controller
(``GeoditPlugin``). Built in code (no .ui files) against ``qgis.PyQt`` with
fully scoped enums so it runs on Qt5 (QGIS 3) and Qt6 (QGIS 4). Colours,
fonts and icons come from ``theme.py``; the building blocks from
``widgets.py``.

Every page fills the panel's width and folds when it runs out: sign-in puts
the Geodit panel beside the form, the projects flow into one to three columns,
and the project page reads top to bottom as its header (with the Data tool,
the web map's), the sync status band, and what needs attention (only when
something does).

Contract with the controller and tests: the ``stack`` page indices below,
``status_label`` (its text is read back), ``code_error`` (shown/hidden, never
replaced) and the public ``show_*`` / ``set_*`` methods.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple, Union

from qgis.gui import QgsFilterLineEdit, QgsPasswordLineEdit
from qgis.PyQt.QtCore import QEvent, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
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
from ..core.status import plural
from ..sync.context import SyncReport
from .theme import NAVY, Theme, device_scale, font, line_icon, logo_pixmap, mix, role_tone, stylesheet
from .widgets import (
    CHEVRON_ROLE,
    DOT_ROLE,
    NOTE_ROLE,
    PILL_ROLE,
    SUBTITLE_ROLE,
    SUBTITLE_TONE_ROLE,
    TITLE_ROLE,
    Avatar,
    Banner,
    CardDelegate,
    EmptyState,
    FitList,
    FlowRow,
    NameLabel,
    PairList,
    Pill,
    SegmentedControl,
    SplitRow,
    StatusBand,
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
_DATA_HINT = "Click on a feature to open its data."  # the web map's Data-tool hint, word for word
_DATA_TIP = "Open a feature's survey data: turn this on, then click a feature on the map."
_NO_FORM_TIP = "No layer in this project has a survey form."
_ATTENTION_SHOWN = 3  # rows before "Show all"
_PROJECT_TILE = 320  # the narrowest project card: one to three columns in a dock
_NOTE_ICON = {"info": ("info", "info"), "error": ("error", "danger"), "success": ("check", "success")}


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


def _column(spacing: int = 10) -> Tuple[QWidget, QVBoxLayout]:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    return widget, layout


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


def _restyle(widget: QWidget) -> None:
    """Re-read the stylesheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class AttentionPanel(QFrame):
    """Needs attention: an amber panel under the sync status, with the rows
    of ``issues`` (a flat ``CardDelegate`` list) — sync warnings first, then
    the features and layers the last sync couldn't handle."""

    def __init__(self, issues: FitList, delegate: CardDelegate, parent=None) -> None:
        super().__init__(parent)
        self._delegate = delegate
        self._theme = Theme.current()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.icon = QLabel()
        self.icon.setFixedSize(16, 16)
        self.title = label("Needs attention", bold=True, wrap=False)
        self.count = Pill("", "warning", strong=True)
        self.more = _tool("link")
        head.addWidget(self.icon)
        head.addWidget(self.title)
        head.addWidget(self.count, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        head.addWidget(self.more)
        layout.addLayout(head)
        layout.addWidget(issues)
        self.apply_theme(self._theme)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        bg, fg = theme.tone("warning")
        if theme.dark:
            bg = mix(theme.surface, bg, 0.55)
        edge = mix(bg, fg, 0.35)
        self.setStyleSheet(
            f"AttentionPanel {{ background: {bg.name()}; border: 1px solid {edge.name()}; border-radius: 8px; }}"
            f" QListView {{ background: transparent; border: none; }}"
        )
        self._delegate.flat_hover = mix(bg, fg, 0.14)
        self._delegate.flat_line = mix(bg, edge, 0.6)
        self._delegate.note_color = fg
        self.icon.setPixmap(line_icon("warning", fg, 16).pixmap(16, 16))


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
        self._busy = False
        self._icon_buttons: List[Tuple[QToolButton, str, int]] = []  # (button, icon name, size)
        self._account_buttons: List[Tuple[QToolButton, QAction]] = []  # (avatar button, its "Signed in as" line)
        self._area_blocked = False
        self._shown_project: Optional[ProjectInfo] = None
        self._shown_notes: Optional[list] = None  # last notes rendered (skip identical re-renders)
        self._shown_issues: Optional[tuple] = None
        self._attention_notes: List[str] = []  # sync warnings, shown first in Needs attention
        self._attention_items: List[dict] = []  # the last report's features and layers
        self._attention_expanded = False
        self._form_tool_on = False
        self._form_layers: Optional[List[Tuple[str, str]]] = None  # (layer, form); None: not known yet
        # A double-click on a project card: its second click lands on the
        # project page that replaced the list. That page ignores it.
        self._click_hold = QTimer(self)
        self._click_hold.setSingleShot(True)
        self._click_hold.timeout.connect(self._release_project_page)  # a method: dropped with the dock

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
            for delegate in (self._project_delegate, self._issue_delegate):
                delegate.theme = theme
            for widget in self._root.findChildren(QWidget):
                apply = getattr(widget, "apply_theme", None)
                if callable(apply):
                    apply(theme)
            self.project_list.viewport().update()
            self.issues.viewport().update()
            for button, name, size in self._icon_buttons:
                button.setIcon(line_icon(name, theme.text if name != "back" else theme.accent_text, size))
            scale = device_scale(self)
            self.logo.setPixmap(logo_pixmap(44, scale))
            self.access_icon.setPixmap(line_icon("lock", theme.muted, 14).pixmap(14, 14))
            self.shield.setPixmap(line_icon("shield", theme.accent_text, 34).pixmap(34, 34))
            self.form_hint_icon.setPixmap(line_icon("database", theme.accent_text, 14).pixmap(14, 14))
            self._style_sync_btn()
            self._paint_form_tool()
            self._fill_form_layers()
            self._refresh_note_icons()
            self._refresh_account_icon()
        finally:
            self._theming = False

    def _icon_button(self, button: QToolButton, name: str, size: int = 16) -> QToolButton:
        button.setIconSize(QSize(size, size))
        self._icon_buttons.append((button, name, size))
        return button

    def _account_button(self) -> QToolButton:
        """The avatar at the top right of both signed-in pages: who is signed
        in, and Sign out."""
        button = _tool("icon", "Account")
        button.setIconSize(QSize(26, 26))
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(button)
        header = menu.addAction("")
        header.setEnabled(False)
        menu.addSeparator()
        sign_out = menu.addAction("Sign out")
        sign_out.triggered.connect(lambda _checked=False: self.c.sign_out())
        button.setMenu(menu)
        self._account_buttons.append((button, header))
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
        card_frame = QFrame()
        card_frame.setProperty("card", "true")
        row = QHBoxLayout(card_frame)
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
        rb.addWidget(card_frame)
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
        self.refresh_btn.clicked.connect(self._refresh_clicked)
        header.addWidget(self.refresh_btn)
        self.account_btn = self._account_button()
        header.addWidget(self.account_btn)
        layout.addLayout(header)
        self.projects_intro = label("Click a project to load its layers into QGIS and keep them in sync.", kind="muted")
        layout.addWidget(self.projects_intro)
        self.projects_busy = _busy_bar()
        layout.addWidget(self.projects_busy)

        self.search = _field("Search projects", QgsFilterLineEdit())
        self.search.setShowSearchIcon(True)
        self.search.setShowClearButton(True)
        self.search.textChanged.connect(self._filter_projects)
        layout.addWidget(self.search)
        # What the last action said: a project that can't be opened, the one
        # just left, a refresh that failed.
        self.projects_notice = Banner("info")
        layout.addWidget(self.projects_notice)
        self.projects_error = Banner("error")
        layout.addWidget(self.projects_error)

        # The cards flow into as many columns as the dock's width allows.
        self.project_list = QListWidget()
        self.project_list.setProperty("cards", "true")
        self.project_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.project_list.setFlow(QListView.Flow.LeftToRight)
        self.project_list.setWrapping(True)
        self.project_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.project_list.setSpacing(4)
        self.project_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.project_list.setMouseTracking(True)
        self.project_list.setUniformItemSizes(True)
        self.project_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._project_delegate = CardDelegate(self.project_list, min_tile_width=_PROJECT_TILE)
        self.project_list.setItemDelegate(self._project_delegate)
        # One click opens (Enter too, on the keyboard's current card).
        self.project_list.itemClicked.connect(self._open)
        self.project_list.itemActivated.connect(self._open)
        layout.addWidget(self.project_list, 1)
        self.projects_empty = EmptyState("map", "No projects to show")
        layout.addWidget(self.projects_empty, 1)
        self.hidden_note = label("", kind="muted")
        layout.addWidget(self.hidden_note)
        return page

    def _refresh_account_icon(self) -> None:
        icon = _avatar_icon(self._user_label, self.theme, 26, device_scale(self))
        for button, _header in self._account_buttons:
            button.setIcon(icon)

    def _refresh_clicked(self) -> None:
        self.projects_notice.set_message("")
        self.c.refresh_projects()

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

    def _item_project(self, item: Optional[QListWidgetItem]) -> Optional[ProjectInfo]:
        if item is None:
            return None
        pid = item.data(_PAYLOAD)
        return next((p for p in self._projects if p.id == pid), None)

    def _open(self, item: Optional[QListWidgetItem]) -> None:
        """A click on a project card (or Enter) opens it — or, for one that
        can't be opened, says why."""
        if self._busy or self.stack.currentIndex() != PAGE_PROJECTS or item is None or item.isHidden():
            return
        project = self._item_project(item)
        if project is None:
            return
        self.project_list.clearSelection()
        if not project.can_open:
            self.projects_notice.set_message(self._blocked_text(project), "warning")
            return
        self.projects_notice.set_message("")
        self._hold_project_page(True)
        self._click_hold.start(QApplication.doubleClickInterval())
        self.c.open_project(project)

    def _hold_project_page(self, hold: bool) -> None:
        self.stack.widget(PAGE_PROJECT).setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, hold)

    def _release_project_page(self) -> None:
        self._hold_project_page(False)

    @staticmethod
    def _blocked_text(project: ProjectInfo) -> str:
        return (
            f"{project.name}: {project.area_label}. QGIS syncs only the survey-area polygons assigned to you. "
            "Assign yourself an area on the web Map page (Assign area), or ask the project owner, then refresh "
            "this list."
        )

    @staticmethod
    def _project_subtitle(project: ProjectInfo) -> str:
        parts = [project.area_label] if project.area_blocker else [project.access_label]
        if not project.is_owner and project.owner_name:
            parts.append(f"owned by {project.owner_name}")
        return " · ".join(parts)

    # -------------------------------------------------------------- project
    def _build_project(self) -> QWidget:
        page, layout = _page(margins=14, spacing=12)
        top = QHBoxLayout()
        top.setSpacing(2)
        back = self._icon_button(
            _tool("back", "Stop syncing this project and go back to the list. Its layers stay in QGIS.", "Projects"),
            "back",
            14,
        )
        back.clicked.connect(lambda: self.c.leave_project())
        top.addWidget(back)
        top.addStretch(1)
        self.project_account_btn = self._account_button()
        top.addWidget(self.project_account_btn)
        self.more_btn = self._icon_button(_tool("icon", "More sync actions"), "more", 18)
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more = QMenu(self.more_btn)
        more.setToolTipsVisible(True)
        self.full_action = more.addAction("Full re-sync…")
        self.full_action.setToolTip(
            "Download everything in your area again and drop local copies of features that no "
            "longer exist on the server. Unsynced local edits are kept."
        )
        self.full_action.triggered.connect(lambda _checked=False: self.c.full_resync())
        self.discard_action = more.addAction("Discard unsynced changes…")
        self.discard_action.setToolTip(
            "Put the layers back to the server's copy. Your versions are kept in a “discarded edits” layer."
        )
        self.discard_action.triggered.connect(lambda _checked=False: self.c.discard_local_changes())
        self.more_btn.setMenu(more)
        top.addWidget(self.more_btn)
        layout.addLayout(top)

        # ---- Header: the project, what you may do in it, and the Data tool.
        title_block, tb = _column(4)
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.project_title = NameLabel(scale=1.3, bold=True)  # a long name breaks anywhere
        self.project_role = Pill("", "neutral")
        title_row.addWidget(self.project_title)
        title_row.addWidget(self.project_role, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch(1)
        tb.addLayout(title_row)
        # Only when something is restricted: the role pill says the rest.
        self.access_box = QWidget()
        access_row = QHBoxLayout(self.access_box)
        access_row.setContentsMargins(0, 0, 0, 0)
        access_row.setSpacing(6)
        self.access_icon = QLabel()
        self.access_icon.setFixedSize(14, 14)
        self.access_label = label("", kind="muted")
        access_row.addWidget(self.access_icon, 0, Qt.AlignmentFlag.AlignTop)
        access_row.addWidget(self.access_label, 1)
        tb.addWidget(self.access_box)
        # The web map's Data tool: while it's on, a click on a feature opens its survey data.
        self.form_btn = _secondary("Data")
        self.form_btn.setIconSize(QSize(16, 16))
        self.form_btn.setCheckable(True)
        self.form_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.form_btn.setToolTip(_DATA_TIP)
        self.form_btn.clicked.connect(self._form_clicked)
        # Beside the name on a wide dock, under it on a narrow one.
        self.header_row = SplitRow(title_block, self.form_btn, tail_align=Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.header_row)

        # ---- What the Data tool does, and which layer has which form.
        self.form_readout = QFrame()
        self.form_readout.setProperty("readout", "true")
        rl = QVBoxLayout(self.form_readout)
        rl.setContentsMargins(12, 10, 12, 10)
        rl.setSpacing(8)
        hint_row = QHBoxLayout()
        hint_row.setSpacing(8)
        self.form_hint_icon = QLabel()
        self.form_hint_icon.setFixedSize(14, 14)
        self.form_hint = label(_DATA_HINT)
        mono = font(0.92)
        mono.setFamily("monospace")
        self.form_hint.setFont(mono)
        hint_row.addWidget(self.form_hint_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        hint_row.addWidget(self.form_hint, 1)
        rl.addLayout(hint_row)
        self.form_layers_caption = section_label("Layers with a form")
        self.form_layers_caption.setWordWrap(True)  # never what holds a narrow dock wide
        self.form_layers_caption.setContentsMargins(22, 0, 0, 0)  # in line with the rows
        rl.addWidget(self.form_layers_caption)
        # Layer | form side by side on a wide dock, the form under its layer on a narrow one.
        self.form_layers = PairList()
        self.form_layers.setContentsMargins(22, 0, 0, 0)
        rl.addWidget(self.form_layers)
        self.form_layers_unknown = label("The layers with a form show here after the first sync.", kind="muted")
        rl.addWidget(self.form_layers_unknown)
        self.form_readout.hide()
        layout.addWidget(self.form_readout)

        # ---- Sync: the status band, tinted by the state.
        self.band = StatusBand()
        lead, ll = QWidget(), QHBoxLayout()
        lead.setLayout(ll)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(12)
        texts, tl = _column(2)
        self.status_headline = label("", scale=1.1, bold=True)
        self.status_label = label("", kind="muted")
        tl.addWidget(self.status_headline)
        tl.addWidget(self.status_label)
        ll.addWidget(self.band.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        ll.addWidget(texts, 1)
        # Secondary while syncing is automatic; the main action when it isn't.
        self.sync_btn = _secondary("Sync now")
        self.sync_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.sync_btn.setMinimumWidth(120)
        self.sync_btn.setIconSize(QSize(16, 16))
        self.sync_btn.clicked.connect(lambda: self.c.sync_now())
        self.full_btn = self.full_action  # kept for callers that toggle it
        # Sync now at the right end, or under the status when the dock is narrow.
        self.status_row = SplitRow(lead, self.sync_btn, lead_room=200)
        self.band.body.addWidget(self.status_row)
        self.progress = QProgressBar()
        self.progress.setProperty("thin", "true")
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.band.body.addWidget(self.progress)
        self.step_label = label("", kind="muted")
        self.step_label.hide()
        self.band.body.addWidget(self.step_label)
        # What the last sync said that isn't a warning: why it failed, that it was canceled…
        self.band_notes, self.band_notes_layout = _column(6)
        self.band_notes.hide()
        self.band.body.addWidget(self.band_notes)
        self.band.body.addWidget(self.band.separator())
        self.auto_sync = QCheckBox("Sync automatically")
        self.auto_sync.toggled.connect(self._auto_sync_toggled)
        self.interval = QComboBox()
        for minutes in _INTERVALS:
            self.interval.addItem(self._interval_text(minutes), minutes)
        self.interval.currentIndexChanged.connect(self._interval_changed)
        every = QWidget()
        every_row = QHBoxLayout(every)
        every_row.setContentsMargins(0, 0, 0, 0)
        every_row.setSpacing(6)
        self.interval_label = QLabel("every")
        every_row.addWidget(self.interval_label)
        every_row.addWidget(self.interval)
        # "every 5 min" under the checkbox only on a very narrow dock: a checkbox's
        # text can't wrap, so it is kept short.
        auto_box = FlowRow([self.auto_sync, every], spacing=6)
        self.auto_box = auto_box
        self.sync_hint = label("", kind="muted")
        self.sync_hint.setFont(font(0.92))
        # The hint beside the controls when it fits on that line, under them when not.
        self.sync_row = SplitRow(auto_box, self.sync_hint, spacing=16, grow="tail")
        self.band.body.addWidget(self.sync_row)
        layout.addWidget(self.band)

        # ---- Needs attention: under the sync status, only when something does.
        self.issues = FitList()
        self.issues.setProperty("cards", "true")
        self._issue_delegate = CardDelegate(self.issues, subtitle_lines=3, flat=True)
        self.issues.setItemDelegate(self._issue_delegate)
        self.issues.itemClicked.connect(self._issue_clicked)
        self.attention = AttentionPanel(self.issues, self._issue_delegate)
        self.issues_count = self.attention.count
        self.attention_more = self.attention.more
        self.attention_more.clicked.connect(self._toggle_attention)
        self.attention.hide()
        layout.addWidget(self.attention)
        layout.addStretch(1)
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

    def _auto_sync_toggled(self, on: bool) -> None:
        self.c.set_auto_sync(on)
        self._update_sync_controls()

    def _update_sync_controls(self) -> None:
        """The interval, the Sync now button and the hint follow automatic sync."""
        self.interval.setEnabled(self.auto_sync.isChecked())
        self.interval_label.setEnabled(self.auto_sync.isChecked())
        self._style_sync_btn()
        self._update_sync_hint()

    def _style_sync_btn(self) -> None:
        primary = not self.auto_sync.isChecked()
        variant = "primary" if primary else "secondary"
        if self.sync_btn.property("variant") != variant:
            self.sync_btn.setProperty("variant", variant)
            _restyle(self.sync_btn)
        self.sync_btn.setIcon(line_icon("refresh", QColor(NAVY) if primary else self.theme.text, 16))

    def _update_sync_hint(self) -> None:
        project = self._shown_project
        editable = project is not None and project.can_edit
        if self._area_blocked:
            text = "Changes you already saved still upload; nothing downloads until an area is assigned to you."
        elif not self.auto_sync.isChecked():
            text = (
                "Automatic sync is off: saved edits upload, and changes made elsewhere download, only when you "
                "click Sync now."
                if editable
                else "Automatic sync is off: changes made elsewhere download only when you click Sync now."
            )
        elif editable:
            text = "Only saved edits sync — use “Save Layer Edits”. They upload a few seconds after you save."
        else:
            text = "Changes made on the web or in the app download automatically."
        self.sync_hint.setText(text)

    def _issue_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(_PAYLOAD)
        if not data:
            return
        shp_id, fid, kind = data
        if kind == "remove":  # a layer removed on the server
            self.c.remove_layer(int(shp_id))
        else:
            self.c.zoom_to(int(shp_id), int(fid), kind)

    def _set_state(self, state: str, headline: Optional[str] = None) -> None:
        self._state = state
        self.band.set_state(state)
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
        else:
            text = ""  # full access: nothing to point out
        self.access_label.setText(text)
        self.access_box.setVisible(bool(text))
        self.access_icon.setVisible(self._area_blocked or not project.can_edit)
        self._update_sync_hint()

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
        self.projects_notice.set_message("")
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

    def set_user(self, user_label: str) -> None:
        """Who is signed in, for the account menus."""
        self._user_label = user_label
        for button, header in self._account_buttons:
            header.setText(f"Signed in as {user_label}")
            button.setToolTip(f"Signed in as {user_label}")
        self._refresh_account_icon()

    def show_projects(
        self,
        projects: Sequence[ProjectInfo],
        user_label: str,
        error: str = "",
        hidden: Optional[HiddenCounts] = None,
    ) -> None:
        self._projects = list(projects)
        self.set_user(user_label)
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
                item.setToolTip(self._blocked_text(p))
            else:
                item.setData(CHEVRON_ROLE, True)
                item.setToolTip(f"{p.name}\n{p.role_label} · {p.access_label}\nClick to open.")
            self.project_list.addItem(item)
        self.projects_count.set(str(len(self._projects)) if self._projects else "", "neutral")
        self.projects_intro.setVisible(bool(self._projects))
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

    def set_projects_notice(self, text: str, kind: str = "info") -> None:
        """A note above the project list: why a project can't be opened, or
        what happened to the one just left."""
        self.projects_notice.set_message(text, kind)

    @staticmethod
    def _hidden_text(hidden: HiddenCounts) -> str:
        parts = []
        if hidden.expired:
            parts.append(f"{plural(hidden.expired, 'project')} hidden: the owner's plan has expired.")
        if hidden.no_access:
            parts.append(f"{plural(hidden.no_access, 'project')} hidden: the Map page is turned off for your role.")
        return "\n".join(parts)

    # ------------------------------------------------------------ the Data tool
    def _form_clicked(self) -> None:
        self.c.toggle_feature_pick(self.form_btn.isChecked())
        self.set_form_tool_active(bool(self.c.form_tool_active()))

    def set_form_tool_active(self, active: bool) -> None:
        """The Data tool (the feature pick tool) is on the map canvas, or not."""
        self._form_tool_on = bool(active)
        self.form_btn.blockSignals(True)
        self.form_btn.setChecked(self._form_tool_on)
        self.form_btn.blockSignals(False)
        self._paint_form_tool()

    def _paint_form_tool(self) -> None:
        on = self._form_tool_on
        self.form_btn.setIcon(line_icon("database", self.theme.accent_text if on else self.theme.text, 16))
        # While it's on: the web's hint, and which layer opens which form.
        self.form_readout.setVisible(on and self._form_layers != [])
        self._update_form_btn()

    def _update_form_btn(self) -> None:
        # Off only when the project is known to have no layer with a form.
        none = self._form_layers == []
        self.form_btn.setEnabled(self._form_tool_on or not none)
        self.form_btn.setToolTip(_NO_FORM_TIP if none and not self._form_tool_on else _DATA_TIP)

    def set_form_layers(self, layers: Optional[Sequence[Tuple[str, str]]]) -> None:
        """The open project's layers linked to a survey form, as ``(layer,
        form name)`` — the form name "" while unknown. ``None`` while the
        layers themselves are unknown (before the first sync)."""
        self._form_layers = None if layers is None else [(str(a), str(b or "")) for a, b in layers]
        self._fill_form_layers()
        self._paint_form_tool()

    def _fill_form_layers(self) -> None:
        layers = self._form_layers or []
        rows: List[Tuple[QWidget, Optional[QWidget]]] = []
        for layer, form in layers:
            cell = None
            if form:
                cell = QWidget()
                cl = QHBoxLayout(cell)
                cl.setContentsMargins(0, 0, 0, 0)
                cl.setSpacing(6)
                icon = QLabel()
                icon.setFixedSize(14, 14)
                icon.setPixmap(line_icon("file", self.theme.accent_text, 14).pixmap(14, 14))
                cl.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
                cl.addWidget(NameLabel(form), 1)
            rows.append((NameLabel(layer, bold=True), cell))
        self.form_layers.set_rows(rows)
        self.form_layers_caption.setVisible(bool(layers))
        self.form_layers.setVisible(bool(layers))
        self.form_layers_unknown.setVisible(self._form_layers is None)

    def form_layer_rows(self) -> List[Tuple[str, str]]:
        """What the Data tool's list shows, for tests and the preview script."""
        return list(self._form_layers or [])

    # ------------------------------------------------------------ project
    def show_project(self, project: ProjectInfo, *, auto_sync: bool, interval: int) -> None:
        self._area_blocked = False  # the controller says otherwise right after, if so
        self._fill_project(project)
        self.auto_sync.blockSignals(True)
        self.interval.blockSignals(True)
        self.auto_sync.setChecked(auto_sync)
        self._set_interval(interval)
        self.auto_sync.blockSignals(False)
        self.interval.blockSignals(False)
        self._update_sync_controls()
        self._attention_expanded = False
        self.set_status("Waiting for the first sync.", [], state="idle")
        self.set_issues(None)
        self.set_form_layers(None)
        self.stack.setCurrentIndex(PAGE_PROJECT)

    def update_project(self, project: ProjectInfo) -> None:
        """The open project's details (permissions) changed; keep status and issues."""
        self._fill_project(project)

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        for widget in (self.sign_in_btn, self.verify_btn, self.continue_btn, self.refresh_btn):
            widget.setEnabled(not busy)
        for bar in (self.sign_in_busy, self.code_busy, self.projects_busy):
            bar.setVisible(busy)
        self.sign_in_btn.setText("Signing in…" if busy else "Sign in")
        self.verify_btn.setText("Verifying…" if busy else "Verify")

    def set_syncing(self, syncing: bool, quiet: bool = False) -> None:
        """``quiet``: a background tick (timer, after a save) — only the button
        says so; the status band and the progress bar stay as they are."""
        shown = syncing and not quiet
        self.progress.setVisible(shown)
        self.progress.setValue(0)
        self.step_label.setVisible(False)
        self.status_label.setVisible(not shown)  # the progress stands in for the last result meanwhile
        self.sync_btn.setEnabled(not syncing)
        self.sync_btn.setText("Syncing…" if syncing else "Sync now")
        self.full_action.setEnabled(not syncing and not self._area_blocked)
        self.discard_action.setEnabled(not syncing)
        if shown:
            self._set_state("syncing")

    def set_area_blocked(self, blocked: bool) -> None:
        """No survey area is assigned: the layers are read-only, and a Full
        re-sync would have nothing to download."""
        self._area_blocked = bool(blocked)
        self.full_action.setEnabled(self.sync_btn.isEnabled() and not self._area_blocked)
        if self._shown_project is not None:
            self._fill_project(self._shown_project)
        else:
            self._update_sync_hint()

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
        """``notes``: what the sync said. Warnings ask you to act and go to
        Needs attention; the rest (why it failed, that it was canceled…) stays
        in the band, under the status."""
        self.status_label.setText(text)
        if state is not None:
            self._set_state(state, headline)
        notes = [("warning", n) if isinstance(n, str) else tuple(n) for n in notes]
        if notes == self._shown_notes:
            return  # unchanged: keep what's shown (no flicker every tick)
        self._shown_notes = notes
        while self.band_notes_layout.count():
            item = self.band_notes_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()  # deleted later: not drawn over the page meanwhile
                widget.deleteLater()
        shown = 0
        for kind, message in notes:
            if message and kind != "warning":
                self.band_notes_layout.addWidget(self._band_note(kind, message))
                shown += 1
        self.band_notes.setVisible(shown > 0)
        self._attention_notes = [message for kind, message in notes if message and kind == "warning"]
        self._render_attention()

    def set_sync_state(self, state: str, headline: str, text: str) -> None:
        """The status alone (a save made "Up to date" untrue): the notes and
        Needs attention stay. A sync shown running keeps its progress; its
        result replaces this."""
        if self.progress.isVisible():
            return
        self.status_label.setText(text)
        self._set_state(state, headline)

    def _band_note(self, kind: str, text: str) -> QWidget:
        row = QWidget()
        row.setProperty("note_kind", kind)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        icon = QLabel()
        icon.setObjectName("note_icon")
        icon.setFixedSize(14, 14)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        text_label = label(text)
        text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(text_label, 1)
        self._paint_note_icon(row)
        return row

    def _paint_note_icon(self, row: QWidget) -> None:
        name, tone = _NOTE_ICON.get(str(row.property("note_kind")), ("info", "info"))
        icon = row.findChild(QLabel, "note_icon")
        if icon is not None:
            icon.setPixmap(line_icon(name, self.theme.tone(tone)[1], 14).pixmap(14, 14))

    def _refresh_note_icons(self) -> None:
        for i in range(self.band_notes_layout.count()):
            widget = self.band_notes_layout.itemAt(i).widget()
            if widget is not None:
                self._paint_note_icon(widget)

    def band_note_texts(self) -> List[str]:
        """The notes shown in the status band (for tests and the preview script)."""
        texts = []
        for i in range(self.band_notes_layout.count()):
            widget = self.band_notes_layout.itemAt(i).widget()
            if widget is not None:
                texts.extend(lbl.text() for lbl in widget.findChildren(QLabel) if lbl.text())
        return texts

    def set_issues(self, report: Optional[SyncReport]) -> None:
        signature = None
        if report is not None:
            signature = tuple(
                (lr.shp_id, tuple(lr.held), tuple(lr.rejected), lr.discarded_total, lr.orphaned)
                for lr in report.layers.values()
            )
        if signature is not None and signature == self._shown_issues:
            return  # unchanged: keep the list
        self._shown_issues = signature
        items: List[dict] = []
        if report is not None:
            for lr in report.layers.values():
                if lr.orphaned:
                    items.append(
                        {
                            "title": lr.name,
                            "text": "Removed on the server — kept on this computer, no longer synced. "
                            "Click to remove it.",
                            "tone": "neutral",
                            "payload": (lr.shp_id, 0, "remove"),
                            "tip": f"{lr.name} was removed from the project on the server; QGIS keeps your copy.\n"
                            "Click to remove the layer from QGIS and delete that copy (you're asked first).",
                        }
                    )
                for fid, reason in lr.held:
                    items.append(self._feature_issue(lr.name, lr.shp_id, fid, reason, "warning"))
                for fid, code, detail in lr.rejected:
                    text = f"{detail} — edit the feature to retry"
                    items.append(
                        self._feature_issue(lr.name, lr.shp_id, fid, text, "danger", f"{text} (server response {code})")
                    )
                if lr.discarded_total:
                    items.append(
                        {
                            "title": lr.name,
                            "text": f"{plural(lr.discarded_total, 'edit')} kept in “{lr.name} — discarded edits”",
                            "tone": "neutral",
                            "tip": "Edits the server couldn't take (the feature was deleted there, or you discarded them)",
                        }
                    )
        self._attention_items = items
        self._render_attention()

    @staticmethod
    def _feature_issue(layer_name: str, shp_id: int, fid: int, text: str, tone: str, tip: str = "") -> dict:
        return {
            "title": f"{layer_name} · feature {fid}",
            "text": text,
            "tone": tone,
            "payload": (shp_id, fid, "layer"),
            "tip": f"{tip or text}\nClick to select it and zoom to it.",
        }

    def _render_attention(self) -> None:
        self.issues.clear()
        for text in self._attention_notes:
            item = QListWidgetItem(text)
            item.setData(NOTE_ROLE, True)
            item.setData(SUBTITLE_ROLE, text)
            item.setToolTip(text)
            self.issues.addItem(item)
        for entry in self._attention_items:
            item = QListWidgetItem(entry["title"])
            item.setData(TITLE_ROLE, entry["title"])
            item.setData(SUBTITLE_ROLE, entry["text"])
            item.setData(DOT_ROLE, entry["tone"])
            if entry.get("payload"):
                item.setData(_PAYLOAD, entry["payload"])
                item.setData(CHEVRON_ROLE, True)
            item.setToolTip(entry.get("tip") or entry["text"])
            self.issues.addItem(item)
        count = self.issues.count()
        many = count > _ATTENTION_SHOWN
        for row in range(count):
            self.issues.setRowHidden(row, many and not self._attention_expanded and row >= _ATTENTION_SHOWN)
        self.issues_count.set(str(count) if count else "", "warning")
        self.attention_more.setVisible(many)
        self.attention_more.setText("Show fewer" if self._attention_expanded else f"Show all ({count})")
        self.attention.setVisible(count > 0)
        self.issues.fit()

    def _toggle_attention(self) -> None:
        self._attention_expanded = not self._attention_expanded
        self._render_attention()
