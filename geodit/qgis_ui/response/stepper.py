"""The feature form's page tabs, the Qt twin of geodit-ui ``Stepper.tsx`` (``StepperRow``).

Each tab gets a rounded pill with:
- a number (a check once passed);
- the page name (``Rooms · 2`` for a repeating page's later entries);
- an eye-off mark on a page hidden from surveyors;
- the error count of a page passed with errors;
- a warning mark on a page whose rule isn't satisfied;
- one trailing action: ⧉ on a repeating page's first entry adds another, × on a later entry removes it.

The row scrolls sideways (the mouse wheel too) and keeps the current tab in view.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QSizePolicy, QWidget

from ..theme import NAVY, Theme, font, line_icon, mix

DUPLICATE, REMOVE = "duplicate", "remove"
_GAP = 6
_ICON = 12
_ACTION = 18
_NAME_MAX_PX = 140
_SCROLLBAR_ROOM = 8


@dataclass(frozen=True)
class Step:
    index: int
    page_id: int
    page_key: int
    name: str
    author_hidden: bool = False
    errors: int = 0
    rule_problem: bool = False
    active: bool = False
    done: bool = False
    action: Optional[str] = None  # DUPLICATE / REMOVE / None


def _pos(event) -> QPointF:
    return QPointF(event.position()) if hasattr(event, "position") else QPointF(event.pos())


class StepPill(QWidget):
    def __init__(self, row: StepperRow) -> None:
        super().__init__()
        self._row = row
        self.step: Optional[Step] = None
        self._theme = Theme.current()
        self._enabled = True
        self._hover = False
        self._hover_action = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    # ------------------------------------------------------------ state
    def set_step(self, step: Step, *, enabled: bool) -> None:
        if step == self.step and enabled == self._enabled:
            return
        self.step = step
        self._enabled = enabled
        self.setFont(font(0.92, bold=step.active))
        self.setToolTip(self._tooltip(False))
        self.updateGeometry()
        self.update()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    # ------------------------------------------------------------ geometry
    def _metrics(self):
        fm = QFontMetrics(self.font())
        height = max(26, fm.height() + 10)
        badge = height - 8
        return fm, height, badge

    def _count_text(self) -> str:
        return str(self.step.errors) if self.step and self.step.errors else ""

    def _layout(self):
        """(badge, name, eye, count, warning, action) rects and the full width."""
        step = self.step
        fm, height, badge = self._metrics()
        x = 4.0
        badge_rect = QRectF(x, (height - badge) / 2, badge, badge)
        x += badge + _GAP
        # A little slack: a bold advance can round down and elide a name that fits.
        name_w = min(fm.horizontalAdvance(step.name) + 2, _NAME_MAX_PX)
        name_rect = QRectF(x, 0, name_w, height)
        x += name_w
        eye = count = warn = action = None
        if step.author_hidden:
            x += _GAP
            eye = QRectF(x, (height - _ICON) / 2, _ICON, _ICON)
            x += _ICON
        if step.errors:
            small = QFontMetrics(font(0.75, bold=True))
            w = max(16, small.horizontalAdvance(self._count_text()) + 8)
            x += _GAP
            count = QRectF(x, (height - 16) / 2, w, 16)
            x += w
        if step.rule_problem:
            x += _GAP
            warn = QRectF(x, (height - _ICON) / 2, _ICON, _ICON)
            x += _ICON
        if step.action:
            x += 2
            action = QRectF(x, (height - _ACTION) / 2, _ACTION, _ACTION)
            x += _ACTION + 4
        else:
            x += 10
        return badge_rect, name_rect, eye, count, warn, action, int(x + 0.5)

    def sizeHint(self) -> QSize:
        if self.step is None:
            return QSize(0, 0)
        _, height, _ = self._metrics()
        return QSize(self._layout()[-1], height)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def action_rect(self) -> Optional[QRectF]:
        return self._layout()[5] if self.step is not None else None

    # ------------------------------------------------------------ colours
    def _colors(self):
        """(background, border, text, badge background, badge text)."""
        t, step = self._theme, self.step
        white = QColor("#ffffff")
        if step.active:
            return mix(t.surface, t.accent, 0.12), mix(t.surface, t.accent, 0.55), t.accent_text, t.accent, QColor(NAVY)
        if step.errors:
            bg, fg = t.tone("danger")
            return bg, mix(bg, fg, 0.35), fg, t.dot("danger"), white
        if step.rule_problem:
            bg, fg = t.tone("warning")
            return bg, mix(bg, fg, 0.35), fg, t.dot("warning"), QColor(NAVY)
        if step.done:
            bg, fg = t.tone("success")
            return bg, mix(bg, fg, 0.35), fg, t.dot("success"), white
        border = mix(t.border, t.text, 0.25) if self._hover else t.border
        text = t.text if self._hover else t.muted
        return t.surface, border, text, mix(t.surface, t.text, 0.1), t.muted

    # ------------------------------------------------------------ painting
    def paintEvent(self, _event) -> None:
        step = self.step
        if step is None:
            return
        bg, border, fg, badge_bg, badge_fg = self._colors()
        badge, name, eye, count, warn, action, _ = self._layout()
        fm = QFontMetrics(self.font())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self._enabled:
            painter.setOpacity(0.6)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.fillPath(path, bg)
        painter.setPen(QPen(border, 1))
        painter.drawPath(path)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(badge_bg)
        painter.drawEllipse(badge)
        if step.done:
            pen = QPen(badge_fg, 1.8)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            c, s = badge.center(), badge.width() / 18
            tick = QPainterPath(QPointF(c.x() - 3.5 * s, c.y()))
            tick.lineTo(QPointF(c.x() - 1 * s, c.y() + 2.5 * s))
            tick.lineTo(QPointF(c.x() + 3.5 * s, c.y() - 2.5 * s))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(tick)
        else:
            painter.setPen(badge_fg)
            painter.setFont(font(0.75, bold=True))
            painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), str(step.index + 1))

        painter.setFont(self.font())
        painter.setPen(fg)
        text = step.name
        if fm.horizontalAdvance(text) > name.width():
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight, int(name.width()))
        painter.drawText(name, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        if eye is not None:
            painter.drawPixmap(
                eye.topLeft().toPoint(), line_icon("eye-off", self._theme.muted, _ICON).pixmap(_ICON, _ICON)
            )
        if count is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._theme.dot("danger"))
            painter.drawRoundedRect(count, 8, 8)
            painter.setPen(QColor("#ffffff"))
            painter.setFont(font(0.75, bold=True))
            painter.drawText(count, int(Qt.AlignmentFlag.AlignCenter), self._count_text())
        if warn is not None:
            painter.drawPixmap(warn.topLeft().toPoint(), line_icon("warning", fg, _ICON).pixmap(_ICON, _ICON))
        if action is not None:
            if self._hover_action and self._enabled:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(mix(bg, self._theme.text, 0.12))
                painter.drawRoundedRect(action, 5, 5)
            icon = line_icon("copy" if step.action == DUPLICATE else "x", fg, 14).pixmap(14, 14)
            painter.drawPixmap(QPointF(action.center().x() - 7, action.center().y() - 7).toPoint(), icon)
        painter.end()

    # ------------------------------------------------------------ input
    def _tooltip(self, over_action: bool) -> str:
        step = self.step
        if step is None:
            return ""
        if over_action and step.action == DUPLICATE:
            return f"Add another {step.name}"
        if over_action and step.action == REMOVE:
            return f"Remove this {step.name.split(' · ')[0]} entry"
        parts = [step.name]
        if step.author_hidden:
            parts.append("hidden from surveyors")
        if step.errors:
            parts.append(f"{step.errors} to fix")
        elif step.rule_problem:
            parts.append("rule not satisfied")
        return " · ".join(parts)

    def _over_action(self, pos: QPointF) -> bool:
        rect = self.action_rect()
        return rect is not None and rect.contains(pos)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        over = self._over_action(_pos(event))
        if over != self._hover_action:
            self._hover_action = over
            self.setToolTip(self._tooltip(over))
            self.update()
        super().mouseMoveEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._hover = self._hover_action = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        pos = _pos(event)
        if (
            event.button() != Qt.MouseButton.LeftButton
            or not self._enabled
            or self.step is None
            or not QRectF(self.rect()).contains(pos)
        ):
            return
        if self._over_action(pos):
            self._row.trigger_action(self.step)
        else:
            self._row.select(self.step)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and self._enabled and self.step:
            self._row.select(self.step)
            return
        super().keyPressEvent(event)


class StepperRow(QScrollArea):
    """The row of pills; ``set_steps`` updates them in place."""

    def __init__(
        self,
        on_select: Callable[[int], None],
        on_duplicate: Callable[[int], None],
        on_remove: Callable[[int, int], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._on_select = on_select
        self._on_duplicate = on_duplicate
        self._on_remove = on_remove
        self.pills: List[StepPill] = []
        self._active = -1
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.apply_theme(Theme.current())
        inner = QWidget()
        inner.setObjectName("GeoditStepper")
        inner.setStyleSheet("QWidget#GeoditStepper { background: transparent; }")
        self._layout = QHBoxLayout(inner)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        self.setWidget(inner)
        self.viewport().setAutoFillBackground(False)

    def apply_theme(self, theme: Theme) -> None:
        """A thin bar under the pills, only when they overflow."""
        handle = mix(theme.border, theme.text, 0.15).name()
        self.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:horizontal { height: 5px; margin: 0; border: none; background: transparent; }"
            f"QScrollBar::handle:horizontal {{ background: {handle}; border-radius: 2px; min-width: 24px; }}"
            "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; border: none; }"
            "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }"
        )

    def reset(self) -> None:
        """A new form: start from the first tab, scrolled to the start."""
        self._active = -1
        self.horizontalScrollBar().setValue(0)

    def set_steps(self, steps: List[Step], *, disabled: bool) -> None:
        while len(self.pills) < len(steps):
            pill = StepPill(self)
            self._layout.insertWidget(len(self.pills), pill, 0, Qt.AlignmentFlag.AlignTop)
            self.pills.append(pill)
        while len(self.pills) > len(steps):
            pill = self.pills.pop()
            self._layout.removeWidget(pill)
            pill.hide()
            pill.deleteLater()
        for pill, step in zip(self.pills, steps):
            pill.set_step(step, enabled=not disabled)
        height = self.pills[0].sizeHint().height() if self.pills else 0
        self.setFixedHeight(height + _SCROLLBAR_ROOM)
        active = next((s.index for s in steps if s.active), -1)
        if active != self._active and 0 <= active < len(self.pills):
            self._active = active
            pill = self.pills[active]
            QTimer.singleShot(0, lambda: self._reveal(pill))

    def _reveal(self, pill: StepPill) -> None:
        if not sip.isdeleted(pill) and pill in self.pills:
            self.ensureWidgetVisible(pill, 24, 0)

    def select(self, step: Step) -> None:
        self._on_select(step.index)

    def trigger_action(self, step: Step) -> None:
        if step.action == DUPLICATE:
            self._on_duplicate(step.page_id)
        elif step.action == REMOVE:
            self._on_remove(step.page_id, step.page_key)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        bar = self.horizontalScrollBar()
        delta = event.angleDelta()
        amount = delta.x() or delta.y()
        if amount and bar.maximum() > 0:
            bar.setValue(bar.value() - amount)
            event.accept()
            return
        event.ignore()
