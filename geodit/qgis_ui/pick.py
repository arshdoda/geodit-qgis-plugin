"""Map tools of the feature form.

``FeaturePickTool`` is the Geodit panel's Data tool — geodit-ui's map Data tool:
a click hands every feature under it, on the synced layers (the top layer
first), to ``on_hits`` — which opens the one, or lets the user pick among them;
Esc clears the selection. ``PointPickTool`` captures one map point for a
LOCATION answer, then hands the canvas back.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from qgis.core import QgsFeature, QgsPointXY, QgsVectorLayer
from qgis.gui import QgsMapToolEmitPoint, QgsMapToolIdentify
from qgis.PyQt.QtCore import QPoint, Qt


class FeaturePickTool(QgsMapToolIdentify):
    def __init__(
        self,
        canvas,
        layers: Callable[[], List[QgsVectorLayer]],
        on_hits: Callable[[List[Tuple[QgsVectorLayer, QgsFeature]], QPoint], None],
        on_miss: Callable[[], None],
    ) -> None:
        super().__init__(canvas)
        self._layers = layers
        self._on_hits = on_hits
        self._on_miss = on_miss
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() != Qt.MouseButton.LeftButton:
            return
        layers = [layer for layer in self._layers() if layer.isSpatial()]
        if not layers:
            self._on_miss()
            return
        pixel = event.pixelPoint()
        results = self.identify(pixel.x(), pixel.y(), layers, QgsMapToolIdentify.IdentifyMode.TopDownAll)
        hits: List[Tuple[QgsVectorLayer, QgsFeature]] = []
        seen = set()
        for result in results:  # in the order of ``layers``: the top one first
            key = (result.mLayer.id(), result.mFeature.id())
            if key not in seen:
                seen.add(key)
                hits.append((result.mLayer, QgsFeature(result.mFeature)))
        if hits:
            self._on_hits(hits, pixel)
        else:
            self._on_miss()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Escape:
            for layer in self._layers():
                layer.removeSelection()
            event.accept()
            return
        super().keyPressEvent(event)


class PointPickTool(QgsMapToolEmitPoint):
    """One click → ``done(point in canvas CRS)``. Esc, ``cancel()`` or another
    map tool taking the canvas → ``done(None)``. ``done`` runs once."""

    def __init__(self, canvas, done: Callable[[Optional[QgsPointXY]], None]) -> None:
        super().__init__(canvas)
        self._done = done
        self._fired = False
        self.leaving = False  # another map tool is taking the canvas right now
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.canvasClicked.connect(self._clicked)

    def _finish(self, point: Optional[QgsPointXY]) -> None:
        if self._fired:
            return
        self._fired = True
        self._done(point)

    def cancel(self) -> None:
        """Give up without a point (the form asked, or is closing)."""
        self._finish(None)

    def _clicked(self, point: QgsPointXY, button) -> None:
        if button == Qt.MouseButton.LeftButton:
            self._finish(QgsPointXY(point))

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Escape and not self._fired:
            self._finish(None)
            event.accept()
            return
        super().keyPressEvent(event)

    def deactivate(self) -> None:
        self.leaving = True
        try:
            self._finish(None)  # the user chose another tool: the pick is over
        finally:
            self.leaving = False
        super().deactivate()
