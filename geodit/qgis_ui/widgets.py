"""Small building blocks for the dock: banners, pills, avatar, segmented
control, status dot, empty state and the card-row delegate shared by the
project list and the "Needs attention" list.

Everything paints from the current ``Theme`` (see ``theme.py``), so a QGIS
theme switch only needs ``apply_theme`` on each widget and a repaint.
Written against ``qgis.PyQt`` with fully scoped enums (Qt5 and Qt6).
"""

from __future__ import annotations

from typing import List, Optional

from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QFont, QFontMetrics, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .theme import Theme, font, line_icon

TITLE_ROLE = Qt.ItemDataRole.UserRole + 1
SUBTITLE_ROLE = Qt.ItemDataRole.UserRole + 2
PILL_ROLE = Qt.ItemDataRole.UserRole + 3  # (text, tone)
DOT_ROLE = Qt.ItemDataRole.UserRole + 4  # tone name
SUBTITLE_TONE_ROLE = Qt.ItemDataRole.UserRole + 5  # tone name for the subtitle text (default: muted)

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


def _rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


class Pill(QLabel):
    """A small rounded tag: role ("OWNER"), access ("VIEW ONLY") or a count."""

    def __init__(self, text: str = "", tone: str = "neutral", parent=None) -> None:
        super().__init__(parent)
        self._tone = tone
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
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.fillPath(_rounded(rect, rect.height() / 2), bg)
        painter.setPen(fg)
        painter.setFont(self.font())
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text())
        painter.end()


class Avatar(QWidget):
    """Initials in a tinted circle."""

    def __init__(self, size: int = 28, parent=None) -> None:
        super().__init__(parent)
        self._name = ""
        self._theme = Theme.current()
        self.setFixedSize(size, size)

    def set_name(self, name: str) -> None:
        self._name = name or ""
        self.update()

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
        bg, fg = self._theme.tone("owner")
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
        bg, fg = self._theme.tone(_BANNER_TONE.get(self._kind, "info"))
        self.setStyleSheet(
            f"Banner {{ background: {bg.name()}; border-radius: 8px; border: none; }}"
            f" QLabel {{ color: {self._theme.text.name()}; background: transparent; }}"
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
        layout.addWidget(self._icon)
        layout.addWidget(self.title)
        layout.addWidget(self.text)
        self.apply_theme(Theme.current())

    def set_text(self, title: str, text: str = "") -> None:
        self.title.setText(title)
        self.text.setText(text)
        self.text.setVisible(bool(text))

    def apply_theme(self, theme: Theme) -> None:
        self._icon.setPixmap(line_icon(self._icon_name, theme.muted, 28).pixmap(28, 28))


class CardDelegate(QStyledItemDelegate):
    """Paints list rows as rounded cards: an optional status dot, a bold title,
    a muted subtitle (wrapped to ``subtitle_lines``) and an optional pill on
    the right."""

    V_GAP = 3
    PAD = 12

    def __init__(self, parent=None, *, subtitle_lines: int = 1) -> None:
        super().__init__(parent)
        self.theme = Theme.current()
        self.subtitle_lines = max(1, subtitle_lines)

    def _text_width(self, index) -> int:
        """Room for the text in a row of the (parent) list's current width."""
        view = self.parent()
        width = view.viewport().width() if view is not None else 300
        width -= 2 * self.PAD + 2
        if index.data(DOT_ROLE):
            width -= 16
        return max(40, width)

    def _subtitle_height(self, font: QFont, text: str, width: int) -> int:
        fm = QFontMetrics(font)
        if self.subtitle_lines == 1 or not text:
            return fm.height()
        flags = int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap)
        needed = fm.boundingRect(0, 0, width, fm.height() * 10, flags, text).height()
        return min(needed, fm.height() * self.subtitle_lines)

    def _fonts(self, option):
        title = QFont(option.font)
        title.setBold(True)
        pill = QFont(option.font)
        pill.setBold(True)
        size = pill.pointSizeF()
        if size > 0:
            pill.setPointSizeF(size * 0.78)
        return title, QFont(option.font), pill

    def sizeHint(self, option, index) -> QSize:
        title_font, body_font, _ = self._fonts(option)
        lines = QFontMetrics(title_font).height()
        subtitle = str(index.data(SUBTITLE_ROLE) or "")
        if subtitle:
            lines += 2 + self._subtitle_height(body_font, subtitle, self._text_width(index))
        # Width 0: the list stretches rows to the viewport, so no horizontal scroll.
        return QSize(0, lines + 2 * 10 + 2 * self.V_GAP)

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
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
            if self.subtitle_lines == 1 or sub_h <= bfm.height():
                painter.drawText(
                    sub_rect,
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    bfm.elidedText(subtitle, Qt.TextElideMode.ElideRight, width),
                )
            else:
                painter.setClipRect(sub_rect)
                painter.drawText(sub_rect, int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap), subtitle)
        painter.restore()
