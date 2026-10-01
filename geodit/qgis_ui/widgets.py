"""Small building blocks for the dock: banners, pills, avatar, segmented
control, status band, empty state, the card-row delegate shared by the
project grid and the "Needs attention" list, and layout helpers that fill the
width and fold when it runs out (columns, a row whose end goes under its start,
a row that stacks when it doesn't fit, name/value rows that stack, a name
label that breaks long names anywhere).

Everything paints from the current ``Theme`` (see ``theme.py``), so a QGIS
theme switch only needs ``apply_theme`` on each widget and a repaint.
Written against ``qgis.PyQt`` with fully scoped enums (Qt5 and Qt6).
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QTextLayout,
    QTextOption,
)
from qgis.PyQt.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListWidget,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .theme import Theme, font, line_icon, mix

TITLE_ROLE = Qt.ItemDataRole.UserRole + 1
SUBTITLE_ROLE = Qt.ItemDataRole.UserRole + 2
PILL_ROLE = Qt.ItemDataRole.UserRole + 3  # (text, tone)
DOT_ROLE = Qt.ItemDataRole.UserRole + 4  # tone name
SUBTITLE_TONE_ROLE = Qt.ItemDataRole.UserRole + 5  # tone name for the subtitle text (default: muted)
CHEVRON_ROLE = Qt.ItemDataRole.UserRole + 6  # True: a click on the row does something (opens, zooms)
NOTE_ROLE = Qt.ItemDataRole.UserRole + 7  # True: a message row (warning icon + text, no title)

_BANNER_ICON = {"info": "info", "warning": "warning", "error": "error", "success": "check"}
_BANNER_TONE = {"info": "info", "warning": "warning", "error": "danger", "success": "success"}


def label(text: str = "", *, kind: Optional[str] = None, wrap: bool = True, scale: float = 1.0, bold=False) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(wrap)
    lbl.setTextFormat(Qt.TextFormat.PlainText if "<" not in text else Qt.TextFormat.AutoText)
    if kind:
        lbl.setProperty("kind", kind)
    if scale != 1.0 or bold:
        lbl.setFont(font(scale, bold=bold))
    return lbl


def section_label(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setProperty("kind", "section")
    f = font(0.85, bold=True)
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)
    lbl.setFont(f)
    return lbl


def divider() -> QFrame:
    line = QFrame()
    line.setProperty("divider", "true")
    line.setFixedHeight(1)
    return line


def card(title: str = "") -> Tuple[QFrame, QVBoxLayout]:
    """A bordered card (``QFrame[card="true"]``) headed by a small uppercase
    ``title``, as the web's panels are; lay its content into the returned layout."""
    frame = QFrame()
    frame.setProperty("card", "true")
    body = QVBoxLayout(frame)
    body.setContentsMargins(12, 10, 12, 12)
    body.setSpacing(8)
    if title:
        body.addWidget(section_label(title))
    return frame, body


def _natural_width(widget: QWidget) -> int:
    """The width ``widget`` needs on one line: a wrapping label's whole text,
    which its size hint (a guess at a wrapped shape) undersells."""
    if isinstance(widget, QLabel) and widget.wordWrap():
        margins = widget.contentsMargins()
        return QFontMetrics(widget.font()).horizontalAdvance(widget.text()) + margins.left() + margins.right() + 4
    return max(widget.sizeHint().width(), widget.minimumWidth())  # a minimum width outgrows the hint


class Columns(QWidget):
    """Its widgets side by side, sharing the width, while each keeps at least
    ``min_width``; one under the other below that. The direction follows the
    width only, so resizing can't loop."""

    def __init__(self, widgets: List[QWidget], *, min_width: int, spacing: int = 12, parent=None) -> None:
        super().__init__(parent)
        self._widgets = list(widgets)
        self._min = min_width
        self._spacing = spacing
        self._box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(spacing)
        # The side-by-side minimum must not hold the row wide: it has to get narrow enough to stack.
        self._box.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        for widget in self._widgets:
            self._box.addWidget(widget, 1)

    def _fits(self, width: int) -> bool:
        n = len(self._widgets)
        return n <= 1 or width >= n * self._min + self._spacing * (n - 1)

    def stacked(self) -> bool:
        return self._box.direction() == QBoxLayout.Direction.TopToBottom

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        width = max((w.minimumSizeHint().width() for w in self._widgets), default=0)
        return QSize(width, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        fits = self._fits(self.width())
        direction = QBoxLayout.Direction.LeftToRight if fits else QBoxLayout.Direction.TopToBottom
        if self._box.direction() != direction:
            self._box.setDirection(direction)
            for i in range(len(self._widgets)):
                self._box.setStretch(i, 1 if fits else 0)  # stacked, nothing grows taller than it needs
            self.updateGeometry()
        super().resizeEvent(event)


class SplitRow(QWidget):
    """``lead`` on the left and ``tail`` at the right end while both fit on
    one line; ``tail`` under ``lead``, left aligned, when they don't.
    ``lead_room`` is the width ``lead`` keeps beside ``tail`` — fixed, so the
    row folds at one width whatever ``lead`` says (a status that changes must
    not make it jump); by default ``lead``'s own width. ``grow``: which side
    takes the spare width on one line (a ``tail`` label then reads from the
    right end)."""

    def __init__(
        self,
        lead: QWidget,
        tail: QWidget,
        *,
        lead_room: Optional[int] = None,
        spacing: int = 10,
        tail_align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignVCenter,
        grow: str = "lead",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.lead = lead
        self.tail = tail
        self._lead_room = lead_room
        self._spacing = spacing
        self._tail_align = tail_align
        self._grow_tail = grow == "tail"
        self._box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(spacing)
        self._box.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._box.addWidget(lead, 0 if self._grow_tail else 1)
        self._box.addWidget(tail, 1 if self._grow_tail else 0)
        self._box.setAlignment(tail, self._tail_alignment(True))
        self._align_tail_text(True)

    def _tail_alignment(self, one_line: bool) -> Qt.AlignmentFlag:
        # A growing tail must not carry a horizontal alignment: a box layout
        # keeps an aligned widget at its size hint, and a wrapping label would
        # then wrap narrower than the room it has (and be cut off).
        if self._grow_tail:
            return self._tail_align if one_line else Qt.AlignmentFlag(0)
        return (Qt.AlignmentFlag.AlignRight | self._tail_align) if one_line else Qt.AlignmentFlag.AlignLeft

    def _align_tail_text(self, one_line: bool) -> None:
        if self._grow_tail and isinstance(self.tail, QLabel):
            side = Qt.AlignmentFlag.AlignRight if one_line else Qt.AlignmentFlag.AlignLeft
            self.tail.setAlignment(side | Qt.AlignmentFlag.AlignVCenter)

    def _fits(self, width: int) -> bool:
        if self.tail.isHidden():
            return True
        lead = self._lead_room if self._lead_room is not None else _natural_width(self.lead)
        return width >= lead + self._spacing + _natural_width(self.tail)

    def stacked(self) -> bool:
        return self._box.direction() == QBoxLayout.Direction.TopToBottom

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        width = max(self.lead.minimumSizeHint().width(), self.tail.minimumSizeHint().width())
        return QSize(width, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        fits = self._fits(self.width())
        direction = QBoxLayout.Direction.LeftToRight if fits else QBoxLayout.Direction.TopToBottom
        if self._box.direction() != direction:
            self._box.setDirection(direction)
            self._box.setStretch(0, 1 if fits and not self._grow_tail else 0)
            self._box.setStretch(1, 1 if fits and self._grow_tail else 0)
            self._box.setAlignment(self.tail, self._tail_alignment(fits))
            self._align_tail_text(fits)
            self.updateGeometry()
        super().resizeEvent(event)


class NameLabel(QLabel):
    """A label for a name (a project, a layer, a form): it wraps between words
    where it can and anywhere where it can't, so a long shapefile-style name
    (``JCL_Survey_Data_1-2-3``) never makes the page wider than the panel.
    Plain text, not selectable."""

    def __init__(self, text: str = "", *, scale: float = 1.0, bold: bool = False, parent=None) -> None:
        super().__init__(text, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)  # height for width
        if scale != 1.0 or bold:
            self.setFont(font(scale, bold=bold))

    def _text_layout(self, width: int) -> QTextLayout:
        layout = QTextLayout(self.text(), self.font(), self)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        y = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1, width))
            line.setPosition(QPointF(0, y))
            y += line.height()
        layout.endLayout()
        return layout

    def _margins(self) -> QSize:
        m = self.contentsMargins()
        return QSize(m.left() + m.right(), m.top() + m.bottom())

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        fm = QFontMetricsF(self.font())
        return QSize(math.ceil(fm.horizontalAdvance(self.text())) + 2, math.ceil(fm.height())) + self._margins()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        fm = QFontMetricsF(self.font())
        width = min(fm.horizontalAdvance(self.text()), 4 * fm.horizontalAdvance("W"))
        return QSize(math.ceil(width) + 2, math.ceil(fm.height())) + self._margins()

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        extra = self._margins()
        return math.ceil(self._text_layout(width - extra.width()).boundingRect().height()) + extra.height()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        rect = self.contentsRect()
        layout = self._text_layout(rect.width())
        top = rect.top() + max(0.0, (rect.height() - layout.boundingRect().height()) / 2)
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        layout.draw(painter, QPointF(rect.left(), top))


class PairList(QWidget):
    """Rows of a name and what goes with it (a layer and its form): names in a
    column and the rest beside them while the widest name (at least
    ``name_min``) and ``value_min`` fit, each under its name below that. The
    layout follows the width only, so resizing can't loop."""

    def __init__(self, *, name_min: int = 160, value_min: int = 160, spacing: int = 16, parent=None) -> None:
        super().__init__(parent)
        self._name_min = name_min
        self._value_min = value_min
        self._rows: List[Tuple[QWidget, Optional[QWidget]]] = []
        self._stacked = True
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(spacing)
        # The side-by-side minimum must not hold the list wide: it has to get narrow enough to stack.
        self._grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

    def set_rows(self, rows: Sequence[Tuple[QWidget, Optional[QWidget]]]) -> None:
        """Replace the rows (``value`` None: nothing beside that name); the old widgets are deleted."""
        for pair in self._rows:
            for widget in pair:
                if widget is not None:
                    self._grid.removeWidget(widget)
                    widget.hide()
                    widget.deleteLater()
        self._rows = list(rows)
        self._stacked = not self._fits(self.width())
        self._place()

    def stacked(self) -> bool:
        return self._stacked

    def _fits(self, width: int) -> bool:
        if not self._rows:
            return True
        margins = self.contentsMargins()
        widest = max(_natural_width(name) for name, _ in self._rows)
        need = max(self._name_min, widest) + self._grid.horizontalSpacing() + self._value_min
        return width - margins.left() - margins.right() >= need

    def _place(self) -> None:
        grid, stacked = self._grid, self._stacked
        for pair in self._rows:
            for widget in pair:
                if widget is not None:
                    grid.removeWidget(widget)
        for row, (name, value) in enumerate(self._rows):
            # Stacked, a little air between one pair and the next.
            name.setContentsMargins(0, 6 if stacked and row else 0, 0, 0)
            if stacked:
                grid.addWidget(name, 2 * row, 0)
                if value is not None:
                    grid.addWidget(value, 2 * row + 1, 0)
            else:
                grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
                if value is not None:
                    grid.addWidget(value, row, 1, Qt.AlignmentFlag.AlignTop)
        grid.setVerticalSpacing(2 if stacked else 4)
        grid.setColumnStretch(0, 1 if stacked else 0)
        grid.setColumnStretch(1, 0 if stacked else 1)
        grid.setColumnMinimumWidth(0, 0 if stacked else self._name_min)
        self.updateGeometry()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        widgets = [w for pair in self._rows for w in pair if w is not None]
        margins = self.contentsMargins()
        width = max((w.minimumSizeHint().width() for w in widgets), default=0) + margins.left() + margins.right()
        return QSize(width, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        stacked = not self._fits(self.width())
        if stacked != self._stacked:
            self._stacked = stacked
            self._place()
        super().resizeEvent(event)


class FlowRow(QWidget):
    """Its widgets side by side while they fit, else one under the other (left
    aligned). The direction follows the width only, so resizing can't loop."""

    def __init__(self, widgets: List[QWidget], spacing: int = 8, parent=None) -> None:
        super().__init__(parent)
        self._widgets = list(widgets)
        self._spacing = spacing
        self._box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(spacing)
        # The side-by-side minimum must not hold the row wide: it has to get
        # narrow enough to stack.
        self._box.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        for widget in self._widgets:
            self._box.addWidget(widget, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._box.addStretch(1)

    def _row_width(self) -> int:
        shown = [w for w in self._widgets if not w.isHidden()]
        return sum(w.sizeHint().width() for w in shown) + self._spacing * max(0, len(shown) - 1)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        width = max((w.minimumSizeHint().width() for w in self._widgets), default=0)
        return QSize(width, super().minimumSizeHint().height())

    def stacked(self) -> bool:
        return self._box.direction() == QBoxLayout.Direction.TopToBottom

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        fits = self.width() >= self._row_width()
        direction = QBoxLayout.Direction.LeftToRight if fits else QBoxLayout.Direction.TopToBottom
        if self._box.direction() != direction:
            self._box.setDirection(direction)
            self.updateGeometry()
        super().resizeEvent(event)


def _rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


class Pill(QLabel):
    """A small rounded tag: role ("OWNER"), access ("VIEW ONLY") or a count."""

    def __init__(self, text: str = "", tone: str = "neutral", parent=None, *, strong: bool = False) -> None:
        super().__init__(parent)
        self._tone = tone
        self._strong = strong  # a deeper tint: for a pill on a panel of its own tone
        self._theme = Theme.current()
        f = font(0.78, bold=True)
        self.setFont(f)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.set(text, tone)

    def set(self, text: str, tone: Optional[str] = None) -> None:
        if tone is not None:
            self._tone = tone
        self.setText(text.upper())
        self.setVisible(bool(text))
        self.updateGeometry()
        self.update()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(fm.horizontalAdvance(self.text()) + 16, fm.height() + 6)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, _event) -> None:
        bg, fg = self._theme.tone(self._tone)
        if self._strong:
            bg = mix(bg, fg, 0.18)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.fillPath(_rounded(rect, rect.height() / 2), bg)
        painter.setPen(fg)
        painter.setFont(self.font())
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())
        painter.end()


# The web's member palette (orange, info, success, navy, warning, grey), picked by member id.
AVATAR_TONES = ("owner", "info", "success", "editor", "warning", "neutral")


class Avatar(QWidget):
    """Initials in a tinted circle."""

    def __init__(self, size: int = 28, parent=None, *, tone: str = "owner") -> None:
        super().__init__(parent)
        self._name = ""
        self._tone = tone
        self._theme = Theme.current()
        self.setFixedSize(size, size)

    def set_name(self, name: str) -> None:
        self._name = name or ""
        self.update()

    def set_member(self, name: str, member_id: Optional[int]) -> None:
        """A team member: their initials, in the colour their id picks (as on the web)."""
        self._tone = AVATAR_TONES[abs(int(member_id)) % len(AVATAR_TONES)] if member_id is not None else "neutral"
        self.set_name(name)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    @staticmethod
    def initials(name: str) -> str:
        parts = [p for p in name.replace(".", " ").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    def paintEvent(self, _event) -> None:
        bg, fg = self._theme.tone(self._tone)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawEllipse(rect)
        f = font(1.0, bold=True)
        f.setPixelSize(max(9, round(self.height() * 0.38)))
        painter.setFont(f)
        painter.setPen(fg)
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.initials(self._name))
        painter.end()


class StatusDot(QWidget):
    TONES = {"ok": "success", "syncing": "info", "pending": "warning", "paused": "warning", "error": "danger"}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._theme = Theme.current()
        self.setFixedSize(10, 10)

    def set_state(self, state: str) -> None:
        self._state = state
        self.update()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, _event) -> None:
        color = self._theme.dot(self.TONES.get(self._state, ""))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(1, 1, 8, 8))
        painter.end()


_BAND_TONE = {"ok": "success", "syncing": "info", "pending": "info", "paused": "warning", "error": "danger"}
_BAND_ICON = {
    "ok": "check",
    "syncing": "refresh",
    "pending": "upload",
    "paused": "warning",
    "error": "error",
    "readonly": "lock",
    "idle": "info",
}


class StatusBand(QFrame):
    """The sync status as a band tinted by its state: green up to date, blue
    syncing or changes waiting, amber paused, red a problem, grey otherwise.
    ``icon`` shows the state; the dock lays the rest out in ``body``."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._theme = Theme.current()
        self._lines: List[QFrame] = []
        self.icon = QLabel()
        self.icon.setFixedSize(26, 26)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(10)
        self._restyle()

    def state(self) -> str:
        return self._state

    def tone(self) -> str:
        return _BAND_TONE.get(self._state, "neutral")

    def separator(self) -> QFrame:
        """A rule in the band's own edge colour."""
        line = QFrame()
        line.setFixedHeight(1)
        self._lines.append(line)
        self._restyle()
        return line

    def set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._restyle()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self._restyle()

    def _restyle(self) -> None:
        theme = self._theme
        bg, fg = theme.tone(self.tone())
        if theme.dark:
            bg = mix(theme.surface, bg, 0.55)  # as Banner: the full tint reads as a heavy bar
        edge = mix(bg, fg, 0.35)
        self.setStyleSheet(
            f"StatusBand {{ background: {bg.name()}; border: 1px solid {edge.name()}; border-radius: 10px; }}"
        )
        for line in self._lines:
            line.setStyleSheet(f"background: {edge.name()}; border: none;")
        self.icon.setPixmap(line_icon(_BAND_ICON.get(self._state, "info"), fg, 26).pixmap(26, 26))


class Banner(QFrame):
    """An inline message: icon + wrapped text on a tinted background."""

    def __init__(self, kind: str = "info", parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        self._theme = Theme.current()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        self._icon = QLabel()
        self._icon.setFixedSize(16, 16)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._text = QLabel()
        self._text.setWordWrap(True)
        self._text.setTextFormat(Qt.TextFormat.PlainText)
        self._text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._text, 1)
        self.hide()
        self._restyle()

    def text(self) -> str:
        return self._text.text()

    def set_message(self, text: str, kind: Optional[str] = None) -> None:
        if kind is not None and kind != self._kind:
            self._kind = kind
            self._restyle()
        self._text.setText(text or "")
        self.setVisible(bool(text))

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self._restyle()

    def _restyle(self) -> None:
        theme = self._theme
        bg, fg = theme.tone(_BANNER_TONE.get(self._kind, "info"))
        if theme.dark:
            bg = mix(theme.surface, bg, 0.55)  # a lighter tint: the full one reads as a heavy bar
        border = mix(bg, fg, 0.35)  # tint + tone border, like the web's callouts
        self.setStyleSheet(
            f"Banner {{ background: {bg.name()}; border-radius: 8px; border: 1px solid {border.name()}; }}"
            f" QLabel {{ color: {theme.text.name()}; background: transparent; border: none; }}"
        )
        self._icon.setPixmap(line_icon(_BANNER_ICON.get(self._kind, "info"), fg, 16).pixmap(16, 16))


class SegmentedControl(QWidget):
    """Two or more mutually exclusive options, e.g. Username | Phone."""

    changed = pyqtSignal(str)

    def __init__(self, options: List[tuple], parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._keys: List[str] = []
        for i, (key, text) in enumerate(options):
            btn = QToolButton()
            btn.setText(text)
            btn.setCheckable(True)
            btn.setProperty("segment", "first" if i == 0 else "last")
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._group.addButton(btn, i)
            self._keys.append(key)
            layout.addWidget(btn)
        self._group.buttons()[0].setChecked(True)
        self._group.buttonClicked.connect(self._clicked)

    def _clicked(self, button) -> None:
        self.changed.emit(self._keys[self._group.id(button)])

    def value(self) -> str:
        return self._keys[max(0, self._group.checkedId())]

    def set_value(self, key: str) -> None:
        if key in self._keys:
            self._group.button(self._keys.index(key)).setChecked(True)


class EmptyState(QWidget):
    def __init__(self, icon: str, title: str, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self._icon_name = icon
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 24, 16, 24)
        layout.setSpacing(6)
        self._icon = QLabel()
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = label(title, scale=1.05, bold=True)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text = label(text, kind="muted")
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Centred as one block: given a tall space, the three lines stay together.
        layout.addStretch(1)
        layout.addWidget(self._icon)
        layout.addWidget(self.title)
        layout.addWidget(self.text)
        layout.addStretch(1)
        self.apply_theme(Theme.current())

    def set_text(self, title: str, text: str = "") -> None:
        self.title.setText(title)
        self.text.setText(text)
        self.text.setVisible(bool(text))

    def apply_theme(self, theme: Theme) -> None:
        self._icon.setPixmap(line_icon(self._icon_name, theme.muted, 28).pixmap(28, 28))


class FitList(QListWidget):
    """A list exactly as tall as its shown rows — it sits in a page that
    scrolls — laid out again when its width changes, so wrapped rows keep
    their height."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._width = -1

    def fit(self) -> None:
        self.doItemsLayout()
        rows = [i for i in range(self.count()) if not self.isRowHidden(i)]
        height = sum(self.sizeHintForRow(i) for i in rows)
        self.setFixedHeight(height + 2 * self.frameWidth())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if self.width() != self._width:
            self._width = self.width()
            QTimer.singleShot(0, self.fit)  # a method: dropped with the list


class CardDelegate(QStyledItemDelegate):
    """Paints list rows as rounded cards: an optional status dot, a bold title,
    a muted subtitle (wrapped to ``subtitle_lines``), an optional pill on the
    right and, on rows a click acts on, a chevron at the end.

    ``min_tile_width``: the list wraps its cards into a grid of columns at
    least that wide (the list must flow left to right and wrap). ``flat``:
    rows without a card, divided by rules — for a list inside a tinted panel
    (``flat_hover`` / ``flat_line`` are that panel's colours); a ``NOTE_ROLE``
    row is a message with a warning icon, and a wide list puts each row's
    subtitle beside its title."""

    V_GAP = 3
    PAD = 12
    CHEVRON = 16
    FLAT_PAD = 8
    FLAT_WIDE = 600  # from this list width, a flat row's subtitle sits beside its title

    def __init__(self, parent=None, *, subtitle_lines: int = 1, min_tile_width: int = 0, flat: bool = False) -> None:
        super().__init__(parent)
        self.theme = Theme.current()
        self.subtitle_lines = max(1, subtitle_lines)
        self.min_tile_width = min_tile_width
        self.flat = flat
        self.flat_hover: Optional[QColor] = None
        self.flat_line: Optional[QColor] = None
        self.note_color: Optional[QColor] = None
        self._icons: dict = {}  # (name, colour) -> QIcon, rendered once per theme

    def _icon(self, name: str, color: QColor, size: int) -> QIcon:
        key = (name, color.name(), size)
        if key not in self._icons:
            self._icons[key] = line_icon(name, color, size)
        return self._icons[key]

    # ------------------------------------------------------------ geometry
    def _view_width(self) -> int:
        view = self.parent()
        return view.viewport().width() if view is not None else 300

    def tile_width(self) -> int:
        """A grid card's width: as many columns of ``min_tile_width`` as fit.
        A list that flows left to right lays out in its widest viewport less a
        scroll bar's room (even with no scroll bar shown), and puts ``spacing``
        on both sides of every card."""
        view = self.parent()
        if view is None:
            return self.min_tile_width
        bar = view.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent, None, view.verticalScrollBar())
        room = view.maximumViewportSize().width() - bar
        gap = 2 * view.spacing()
        columns = max(1, room // (self.min_tile_width + gap))
        return max(80, room // columns - gap - 1)

    def _row_width(self) -> int:
        return self.tile_width() if self.min_tile_width else self._view_width()

    def _text_width(self, index) -> int:
        """Room for the text in a row of the (parent) list's current width."""
        width = self._row_width()
        if self.flat:
            width -= 2 * self.FLAT_PAD + (24 if index.data(NOTE_ROLE) else 18)
        else:
            width -= 2 * self.PAD + 2
            if index.data(DOT_ROLE):
                width -= 16
        if index.data(CHEVRON_ROLE):
            width -= self.CHEVRON + 8
        return max(40, width)

    def _wrapped_height(self, font: QFont, text: str, width: int, lines: int) -> int:
        fm = QFontMetrics(font)
        if lines == 1 or not text:
            return fm.height()
        flags = int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap)
        needed = fm.boundingRect(0, 0, max(10, width), fm.height() * 20, flags, text).height()
        return min(needed, fm.height() * lines)

    def _subtitle_height(self, font: QFont, text: str, width: int) -> int:
        return self._wrapped_height(font, text, width, self.subtitle_lines)

    def _fonts(self, option):
        title = QFont(option.font)
        title.setBold(True)
        pill = QFont(option.font)
        pill.setBold(True)
        size = pill.pointSizeF()
        if size > 0:
            pill.setPointSizeF(size * 0.78)
        return title, QFont(option.font), pill

    def _title_column(self, title_font: QFont, title: str, width: int) -> int:
        """A wide flat row's title column: the title's width, at most 40 % of the row."""
        return min(QFontMetrics(title_font).horizontalAdvance(title) + 4, int(width * 0.4))

    def sizeHint(self, option, index) -> QSize:
        title_font, body_font, _ = self._fonts(option)
        if self.flat:
            return QSize(0, self._flat_height(index, title_font, body_font))
        lines = QFontMetrics(title_font).height()
        subtitle = str(index.data(SUBTITLE_ROLE) or "")
        if subtitle:
            lines += 2 + self._subtitle_height(body_font, subtitle, self._text_width(index))
        # Width 0 in a list: rows stretch to the viewport, so no horizontal scroll.
        width = self.tile_width() if self.min_tile_width else 0
        return QSize(width, lines + 2 * 10 + 2 * self.V_GAP)

    def _flat_height(self, index, title_font: QFont, body_font: QFont) -> int:
        width = self._text_width(index)
        subtitle = str(index.data(SUBTITLE_ROLE) or "")
        if index.data(NOTE_ROLE):
            return self._wrapped_height(body_font, subtitle, width, 8) + 2 * self.FLAT_PAD
        title = str(index.data(TITLE_ROLE) or "")
        title_h = QFontMetrics(title_font).height()
        if not subtitle:
            return title_h + 2 * self.FLAT_PAD
        if self._row_width() >= self.FLAT_WIDE:
            column = self._title_column(title_font, title, width)
            sub_h = self._wrapped_height(body_font, subtitle, width - column - 12, self.subtitle_lines)
            return max(title_h, sub_h) + 2 * self.FLAT_PAD
        sub_h = self._subtitle_height(body_font, subtitle, width)
        return title_h + 2 + sub_h + 2 * self.FLAT_PAD

    # ------------------------------------------------------------ painting
    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if self.flat:
            self._paint_flat(painter, opt, index)
            return
        t = self.theme
        title_font, body_font, pill_font = self._fonts(opt)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        hover = bool(opt.state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(opt.rect).adjusted(0.5, self.V_GAP + 0.5, -0.5, -self.V_GAP - 0.5)
        path = _rounded(card, 8)
        painter.fillPath(path, t.selected if selected else (t.hover if hover else t.surface))
        painter.setPen(QPen(t.accent if selected else t.border, 1))
        painter.drawPath(path)

        left = card.left() + self.PAD
        right = card.right() - self.PAD
        dot = index.data(DOT_ROLE)
        if dot:
            color = t.dot(dot)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(left + 4, card.center().y()), 4, 4)
            left += 16

        if index.data(CHEVRON_ROLE):
            size = self.CHEVRON
            box = QRect(round(right - size), round(card.center().y() - size / 2), size, size)
            self._icon("chevron-right", t.muted, size).paint(painter, box)
            right = box.left() - 8

        pill = index.data(PILL_ROLE)
        if pill:
            text, tone = pill
            fm = QFontMetrics(pill_font)
            w = fm.horizontalAdvance(text.upper()) + 16
            h = fm.height() + 6
            pill_rect = QRectF(right - w, card.center().y() - h / 2, w, h)
            bg, fg = t.tone(tone)
            painter.fillPath(_rounded(pill_rect, h / 2), bg)
            painter.setFont(pill_font)
            painter.setPen(fg)
            painter.drawText(pill_rect, int(Qt.AlignmentFlag.AlignCenter), text.upper())
            right = pill_rect.left() - 8

        width = max(10, int(right - left))
        title = str(index.data(TITLE_ROLE) or index.data(Qt.ItemDataRole.DisplayRole) or "")
        subtitle = str(index.data(SUBTITLE_ROLE) or "")
        tfm = QFontMetrics(title_font)
        bfm = QFontMetrics(body_font)
        sub_h = self._subtitle_height(body_font, subtitle, width) if subtitle else 0
        block = tfm.height() + ((2 + sub_h) if subtitle else 0)
        top = card.center().y() - block / 2
        painter.setFont(title_font)
        painter.setPen(t.text)
        painter.drawText(
            QRectF(left, top, width, tfm.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            tfm.elidedText(title, Qt.TextElideMode.ElideRight, width),
        )
        if subtitle:
            painter.setFont(body_font)
            sub_tone = index.data(SUBTITLE_TONE_ROLE)
            painter.setPen(t.tone(sub_tone)[1] if sub_tone else t.muted)
            sub_rect = QRectF(left, top + tfm.height() + 2, width, sub_h)
            self._draw_subtitle(painter, bfm, sub_rect, subtitle)
        painter.restore()

    def _draw_subtitle(self, painter, bfm: QFontMetrics, rect: QRectF, text: str) -> None:
        if self.subtitle_lines == 1 or rect.height() <= bfm.height():
            painter.drawText(
                rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                bfm.elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width())),
            )
        else:
            painter.save()
            painter.setClipRect(rect)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap), text)
            painter.restore()

    def _paint_flat(self, painter, opt, index) -> None:
        t = self.theme
        title_font, body_font, _ = self._fonts(opt)
        hover = bool(opt.state & QStyle.StateFlag.State_MouseOver) and bool(index.data(CHEVRON_ROLE))
        rect = QRectF(opt.rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if hover and self.flat_hover is not None:
            painter.fillPath(_rounded(rect.adjusted(0, 1, 0, -1), 6), self.flat_hover)
        if index.row() > 0 and self.flat_line is not None:
            painter.setPen(QPen(self.flat_line, 1))
            painter.drawLine(QPointF(rect.left(), rect.top() + 0.5), QPointF(rect.right(), rect.top() + 0.5))
        left = rect.left() + self.FLAT_PAD
        right = rect.right() - self.FLAT_PAD
        width = self._text_width(index)
        subtitle = str(index.data(SUBTITLE_ROLE) or "")
        bfm = QFontMetrics(body_font)
        if index.data(NOTE_ROLE):
            color = self.note_color or t.tone("warning")[1]
            self._icon("warning", color, 16).paint(
                painter, QRect(round(left), round(rect.top() + self.FLAT_PAD + 1), 16, 16)
            )
            left += 24
            height = self._wrapped_height(body_font, subtitle, width, 8)
            painter.setFont(body_font)
            painter.setPen(t.text)
            text_rect = QRectF(left, rect.top() + self.FLAT_PAD, width, height)
            painter.save()
            painter.setClipRect(text_rect)
            painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap), subtitle)
            painter.restore()
            painter.restore()
            return
        dot = index.data(DOT_ROLE)
        if dot:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(t.dot(dot))
            painter.drawEllipse(
                QPointF(left + 6, rect.top() + self.FLAT_PAD + QFontMetrics(title_font).height() / 2), 4, 4
            )
        left += 18
        if index.data(CHEVRON_ROLE):
            size = self.CHEVRON
            box = QRect(round(right - size), round(rect.center().y() - size / 2), size, size)
            self._icon("chevron-right", t.muted, size).paint(painter, box)
        title = str(index.data(TITLE_ROLE) or "")
        tfm = QFontMetrics(title_font)
        top = rect.top() + self.FLAT_PAD
        painter.setFont(title_font)
        painter.setPen(t.text)
        if subtitle and self._row_width() >= self.FLAT_WIDE:
            column = self._title_column(title_font, title, width)
            painter.drawText(
                QRectF(left, top, column, tfm.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                tfm.elidedText(title, Qt.TextElideMode.ElideRight, column),
            )
            sub_width = width - column - 12
            sub_h = self._wrapped_height(body_font, subtitle, sub_width, self.subtitle_lines)
            painter.setFont(body_font)
            painter.setPen(t.muted)
            self._draw_subtitle(painter, bfm, QRectF(left + column + 12, top, sub_width, sub_h), subtitle)
        else:
            painter.drawText(
                QRectF(left, top, width, tfm.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                tfm.elidedText(title, Qt.TextElideMode.ElideRight, width),
            )
            if subtitle:
                sub_h = self._subtitle_height(body_font, subtitle, width)
                painter.setFont(body_font)
                painter.setPen(t.muted)
                self._draw_subtitle(painter, bfm, QRectF(left, top + tfm.height() + 2, width, sub_h), subtitle)
        painter.restore()
