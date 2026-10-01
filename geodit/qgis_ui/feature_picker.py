"""The feature picker: one click of the Data tool lands on several features of
layers with a survey form, and the user picks the one to open — as the web
map's picker does (geodit-ui ``FeaturePicker``). It lists features, not layers,
the top one first, at most eight: a colour swatch shaped like the geometry, the
feature's name in bold and "Layer · Polygon" under it. Hovering a row (or
moving to it with the keys) highlights that feature on the map; a click or
Enter opens it; Esc, a click elsewhere, a pan or zoom, or another map tool
closes the list.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from qgis.core import (
    Qgis,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsRenderContext,
    QgsVectorLayer,
)
from qgis.gui import QgsHighlight
from qgis.PyQt.QtCore import QPoint, QRect, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QPainterPath
from qgis.PyQt.QtWidgets import (
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
)

from .theme import Theme, font, stylesheet

Hit = Tuple[QgsVectorLayer, QgsFeature]

MAX_ROWS = 8  # as the web: the top eight
WIDTH = 264  # px, as the web's popover
ROW_HEIGHT = 44
SHOWN_ROWS = 6  # taller than this, the list scrolls
_ROW_ROLE = Qt.ItemDataRole.UserRole + 1  # (swatch colour or None, geometry kind, title, caption)

_KINDS = {
    Qgis.GeometryType.Point: "Point",
    Qgis.GeometryType.Line: "Line",
    Qgis.GeometryType.Polygon: "Polygon",
}


def _is_null(value) -> bool:
    if value is None or value == "":
        return True
    is_null = getattr(value, "isNull", None)  # QVariant NULL on QGIS 3
    return bool(is_null()) if callable(is_null) else False


def feature_title(layer: QgsVectorLayer, feature: QgsFeature) -> str:
    """The layer's display name for the feature (QGIS's display expression,
    the nearest thing to the web's label field), else "Feature {Geodit ID}"."""
    expression = layer.displayExpression()
    if expression:
        context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        context.setFeature(feature)
        value = QgsExpression(expression).evaluate(context)
        if not _is_null(value) and str(value).strip():
            return str(value).strip()
    index = feature.fields().indexOf("gd_id")
    gd_id = feature.attribute(index) if index >= 0 else None
    return f"Feature {gd_id}" if not _is_null(gd_id) else f"Feature {feature.id()}"


def feature_color(layer: QgsVectorLayer, feature: QgsFeature, canvas) -> Optional[QColor]:
    """The colour the layer's style draws ``feature`` in, for the row's swatch."""
    renderer = layer.renderer()
    if renderer is None:
        return None
    renderer = renderer.clone()
    context = QgsRenderContext.fromMapSettings(canvas.mapSettings())
    context.expressionContext().appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    try:
        renderer.startRender(context, layer.fields())
        try:
            symbol = renderer.symbolForFeature(feature, context)
        finally:
            renderer.stopRender(context)
    except Exception:  # noqa: BLE001 - a swatch is decoration: any renderer trouble draws it muted
        return None
    return QColor(symbol.color()) if symbol is not None else None


class _RowDelegate(QStyledItemDelegate):
    def __init__(self, theme: Theme, parent=None) -> None:
        super().__init__(parent)
        self.theme = theme

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt API
        return QSize(WIDTH - 16, ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index) -> None:
        t = self.theme
        color, kind, title, caption = index.data(_ROW_ROLE)
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        state = option.state
        if state & QStyle.StateFlag.State_Selected or state & QStyle.StateFlag.State_MouseOver:
            path = QPainterPath()
            path.addRoundedRect(QRectF(rect.adjusted(2, 1, -2, -1)), 6, 6)
            painter.fillPath(path, t.selected if state & QStyle.StateFlag.State_Selected else t.hover)
        swatch = QColor(color) if color is not None else t.muted
        cx, cy = rect.left() + 18, rect.center().y()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(swatch)
        if kind == "Point":
            painter.drawEllipse(QRectF(cx - 5, cy - 5, 10, 10))
        elif kind == "Line":
            painter.drawRoundedRect(QRectF(cx - 1.5, cy - 8, 3, 16), 1.5, 1.5)
        else:
            painter.drawRoundedRect(QRectF(cx - 6, cy - 6, 12, 12), 3, 3)
        left = rect.left() + 34
        width = max(10, rect.right() - 10 - left)
        title_font, body_font = font(1.0, bold=True), font(0.9)
        tfm, bfm = QFontMetrics(title_font), QFontMetrics(body_font)
        top = cy - (tfm.height() + bfm.height()) // 2
        painter.setPen(t.text)
        painter.setFont(title_font)
        painter.drawText(
            QRect(left, top, width, tfm.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            tfm.elidedText(title, Qt.TextElideMode.ElideRight, width),
        )
        painter.setPen(t.muted)
        painter.setFont(body_font)
        painter.drawText(
            QRect(left, top + tfm.height(), width, bfm.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            bfm.elidedText(caption, Qt.TextElideMode.ElideRight, width),
        )
        painter.restore()


class FeaturePicker(QFrame):
    """The list of features under one click; ``on_pick(layer, fid)`` opens one.
    ``closed`` fires as it goes away (at once — its deletion comes later)."""

    closed = pyqtSignal()

    def __init__(self, canvas, hits: List[Hit], on_pick: Callable[[QgsVectorLayer, int], None]) -> None:
        super().__init__(canvas.window(), Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setObjectName("GeoditFeaturePicker")
        self._canvas = canvas
        self._on_pick = on_pick
        self._hits = list(hits[:MAX_ROWS])
        self._highlight: Optional[QgsHighlight] = None
        self._watching = False
        theme = Theme.current()
        self._theme = theme
        self.setStyleSheet(
            stylesheet(theme) + f"#GeoditFeaturePicker {{ background: {theme.surface.name()}; border: 1px solid "
            f"{theme.border.name()}; border-radius: 8px; }}"
            "#GeoditFeaturePicker QListWidget { background: transparent; border: none; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 10, 6, 6)
        layout.setSpacing(2)
        self.title = QLabel(f"{len(self._hits)} features here")
        self.title.setFont(font(1.0, bold=True))
        self.title.setContentsMargins(8, 0, 8, 0)
        self.hint = QLabel("Pick the one you want to open")
        self.hint.setProperty("kind", "muted")
        self.hint.setContentsMargins(8, 0, 8, 4)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)
        self.list = QListWidget()
        self.list.setMouseTracking(True)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setItemDelegate(_RowDelegate(theme, self.list))
        for layer, feature in self._hits:
            item = QListWidgetItem()
            kind = _KINDS.get(layer.geometryType(), "Feature")
            title = feature_title(layer, feature)
            item.setData(_ROW_ROLE, (feature_color(layer, feature, canvas), kind, title, f"{layer.name()} · {kind}"))
            item.setToolTip(title)
            self.list.addItem(item)
        self.list.setFixedHeight(ROW_HEIGHT * min(len(self._hits), SHOWN_ROWS) + 4)
        self.list.itemEntered.connect(self._preview)
        self.list.currentItemChanged.connect(self._current_changed)
        self.list.itemClicked.connect(self._pick)
        self.list.itemActivated.connect(self._pick)
        layout.addWidget(self.list)
        self.setFixedWidth(WIDTH)

    # ------------------------------------------------------------ showing
    def show_at(self, click: QPoint) -> None:
        """Open 10 px under ``click`` (global), kept on the screen — above the
        click when there's no room under it."""
        self.adjustSize()
        screen = QGuiApplication.screenAt(click) or QGuiApplication.primaryScreen()
        x, y = click.x(), click.y() + 10
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left(), min(x, area.right() - self.width()))
            if y + self.height() > area.bottom():
                y = max(area.top(), click.y() - 10 - self.height())
        self.move(x, y)
        # A pan, a zoom or another map tool means the user moved on.
        self._canvas.extentsChanged.connect(self.close)
        self._canvas.mapToolSet.connect(self.close)
        self._watching = True
        self.show()
        self.list.setFocus()
        self.list.setCurrentRow(0)  # as the web: the first row's feature lights up at once

    def rows(self) -> List[Tuple[str, str]]:
        """(title, caption) per row, for tests."""
        return [tuple(self.list.item(i).data(_ROW_ROLE)[2:]) for i in range(self.list.count())]

    def pick_row(self, row: int) -> None:
        self._pick(self.list.item(row))

    # ------------------------------------------------------------ events
    def _current_changed(self, current, _previous) -> None:
        self._preview(current)

    def _preview(self, item: Optional[QListWidgetItem]) -> None:
        self._clear_highlight()
        if item is None:
            return
        layer, feature = self._hits[self.list.row(item)]
        highlight = QgsHighlight(self._canvas, feature, layer)
        accent = QColor(self._theme.accent)
        highlight.setColor(accent)
        accent.setAlpha(60)
        highlight.setFillColor(accent)
        highlight.setWidth(3)
        highlight.show()
        self._highlight = highlight

    def _pick(self, item: Optional[QListWidgetItem]) -> None:
        if item is None:
            return
        layer, feature = self._hits[self.list.row(item)]
        self.close()
        self._on_pick(layer, feature.id())

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._clear_highlight()
        if self._watching:
            self._watching = False
            for signal in (self._canvas.extentsChanged, self._canvas.mapToolSet):
                try:
                    signal.disconnect(self.close)
                except (TypeError, RuntimeError):
                    pass
            self.closed.emit()
        super().hideEvent(event)

    def _clear_highlight(self) -> None:
        highlight, self._highlight = self._highlight, None
        if highlight is not None:
            highlight.hide()
            scene = self._canvas.scene()
            if scene is not None:
                scene.removeItem(highlight)
