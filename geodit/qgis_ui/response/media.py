"""Media answers as the contract wants them uploaded (Qt side).

Photos become a JPEG whose long side is at most 2048 px, quality 70, carrying
the source's capture-time and GPS tags (a JPEG already that small uploads
unchanged); documents must be PDFs; audio / video are accepted only when
already in the kind's format (QGIS can't transcode). Signatures are drawn in a
pad and saved as a PNG cropped to the ink, redrawn to 600 px on the long side —
geodit-ui ``convert/image.ts`` / ``signature.ts``. ``prepare_*`` run in a worker
thread (QImage, not QPixmap).
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QImage, QImageReader, QPainter, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ...forms.exif import build_exif_app1, exif_from_raw, insert_app1, read_exif, read_jpeg_info
from ...forms.media import (
    SIGNATURE_LIVE_STROKE_PX,
    accept_audio_video,
    format_limit,
    format_size,
    media_spec,
    photo_target,
    signature_ink_box,
    signature_output,
    signature_saved_stroke,
    sniff_bytes,
    sniff_head,
    upload_name,
)


class MediaError(Exception):
    """A file that can't be uploaded, with the message to show."""


def _read(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise MediaError(f"Couldn't read the file: {exc.strerror or exc}") from None


def _check_size(size: int, kind: str) -> None:
    limit = media_spec(kind)["max_bytes"]
    if size <= 0:
        raise MediaError("This file is empty.")
    if size > limit:
        raise MediaError(f"This file is {format_size(size)} — the limit is {format_limit(limit)}.")


def _jpeg_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "JPEG", media_spec("image")["profile"]["jpeg_quality"]):
        raise MediaError("Couldn't encode the photo as JPEG.")
    buffer.close()
    return bytes(data)


def prepare_image(path: str) -> Tuple[bytes, str]:
    """``(jpeg bytes, upload name)`` for a picked photo."""
    data = _read(path)
    name = upload_name(os.path.basename(path), "image")
    fmt = sniff_bytes(data[:1024])
    info = read_jpeg_info(data[: 512 * 1024]) if fmt == "jpeg" else None
    long_side = media_spec("image")["profile"]["long_side_px"]
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    if info is not None and max(info[0], info[1]) <= long_side:
        # Uploaded as it is — but only a JPEG that decodes.
        if not reader.canRead():
            raise MediaError("QGIS can't read this image.")
        _check_size(len(data), "image")
        return data, name
    image = reader.read()
    if image.isNull():
        # §4.3 — a JPEG that can't be re-encoded still goes up as it is.
        if fmt == "jpeg" and 0 < len(data) <= media_spec("image")["max_bytes"]:
            return data, name
        raise MediaError(f"QGIS can't read this image ({reader.errorString() or fmt}).")
    target = photo_target(image.width(), image.height())
    if target is not None:
        image = image.scaled(
            target[0], target[1], Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
    # Flatten onto white: JPEG has no alpha.
    flat = QImage(image.size(), QImage.Format.Format_RGB32)
    flat.fill(QColor("#ffffff"))
    painter = QPainter(flat)
    painter.drawImage(0, 0, image)
    painter.end()
    jpeg = _jpeg_bytes(flat)
    carry = exif_from_raw(read_exif(data)) if fmt == "jpeg" else {}
    app1 = build_exif_app1(carry)
    if app1 is not None:
        jpeg = insert_app1(jpeg, app1)
    _check_size(len(jpeg), "image")
    return jpeg, name


def prepare_document(path: str) -> Tuple[bytes, str]:
    data = _read(path)
    if sniff_head(data[:1024]) != "pdf":
        raise MediaError("Only PDF documents can be uploaded.")
    _check_size(len(data), "document")
    return data, upload_name(os.path.basename(path), "document")


def prepare_audio_video(kind: str, path: str) -> Tuple[bytes, str]:
    problem = accept_audio_video(kind, path)
    if problem:
        raise MediaError(problem)
    return _read(path), upload_name(os.path.basename(path), kind)


def prepare(kind: str, path: str) -> Tuple[bytes, str]:
    if kind == "image":
        return prepare_image(path)
    if kind == "document":
        return prepare_document(path)
    return prepare_audio_video(kind, path)


# ------------------------------------------------------------------ signature
Stroke = List[Tuple[float, float]]


def render_signature_png(strokes: List[Stroke], pad_size: Tuple[float, float]) -> Optional[bytes]:
    """Cropped to the ink + 24 px and redrawn from the strokes so the long side
    is exactly 600 px, black ink on opaque white; None when there's no ink."""
    box = signature_ink_box(strokes, pad_size)
    if box is None:
        return None
    left, top, width, height = box
    scale, out_w, out_h = signature_output(width, height)
    image = QImage(out_w, out_h, QImage.Format.Format_RGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(scale, scale)
    painter.translate(-left, -top)
    pen = QPen(QColor("#000000"))
    pen.setWidthF(signature_saved_stroke(scale) / scale)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    for stroke in strokes:
        if len(stroke) < 2:
            continue
        path = QPainterPath(QPointF(*stroke[0]))
        for point in stroke[1:]:
            path.lineTo(QPointF(*point))
        painter.drawPath(path)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


class SignaturePad(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.strokes: List[Stroke] = []
        self.setMinimumSize(QSize(480, 180))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def clear(self) -> None:
        self.strokes = []
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        pos = event.position() if hasattr(event, "position") else event.pos()
        self.strokes.append([(pos.x(), pos.y())])
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self.strokes:
            return
        pos = event.position() if hasattr(event, "position") else event.pos()
        self.strokes[-1].append((pos.x(), pos.y()))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        painter.setPen(QPen(QColor("#d0d0d0"), 1, Qt.PenStyle.DashLine))
        baseline = self.height() - 36
        painter.drawLine(16, baseline, self.width() - 16, baseline)
        pen = QPen(QColor("#000000"))
        pen.setWidthF(SIGNATURE_LIVE_STROKE_PX)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        for stroke in self.strokes:
            if len(stroke) < 2:
                continue
            path = QPainterPath(QPointF(*stroke[0]))
            for point in stroke[1:]:
                path.lineTo(QPointF(*point))
            painter.drawPath(path)
        painter.end()


class SignatureDialog(QDialog):
    """Draw a signature; ``png`` holds the rendered file after Accept."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Signature")
        self.png: Optional[bytes] = None
        layout = QVBoxLayout(self)
        hint = QLabel("Sign in the box below with the mouse or a pen.")
        hint.setProperty("kind", "muted")
        layout.addWidget(hint)
        self.pad = SignaturePad()
        layout.addWidget(self.pad, 1)
        self.error = QLabel("")
        self.error.setStyleSheet("color: #E03131;")
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        clear = QPushButton("Clear")
        clear.setAutoDefault(False)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        use = QPushButton("Use signature")
        use.setDefault(True)
        clear.clicked.connect(self.pad.clear)
        cancel.clicked.connect(self.reject)
        use.clicked.connect(self._use)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(use)
        layout.addLayout(buttons)
        self.resize(560, 300)

    def _use(self) -> None:
        png = render_signature_png(self.pad.strokes, (float(self.pad.width()), float(self.pad.height())))
        if png is None:
            self.error.setText("Draw your signature first.")
            self.error.show()
            return
        self.png = png
        self.accept()
