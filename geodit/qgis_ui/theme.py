"""Colours, fonts, icons and the dock stylesheet.

Neutrals come from the QGIS palette, so the dock follows the active QGIS theme
(the default light one, "Night Mapping", "Blend of Gray"); Geodit's orange is
the one accent, with navy text on it as on the Geodit web app. Only widgets
that opt in through a dynamic property (``variant``, ``kind``, ``card``,
``tone``…) are restyled; spin boxes, combo boxes and check boxes keep the
native look, which QSS would otherwise have to redraw sub-control by
sub-control.

Line icons are Lucide (https://lucide.dev, ISC licence), tinted at render time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Tuple

from qgis.PyQt.QtCore import QByteArray, QRectF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from qgis.PyQt.QtWidgets import QApplication

try:  # QtSvg ships with every QGIS build; the fallback is the qsvg image plugin.
    from qgis.PyQt.QtSvg import QSvgRenderer
except ImportError:  # pragma: no cover
    QSvgRenderer = None  # type: ignore[assignment,misc]

ACCENT = "#E09134"  # geodit-orange-500
ACCENT_HOVER = "#C97D24"  # orange-600
ACCENT_PRESSED = "#A1641C"  # orange-700
NAVY = "#111724"  # geodit-navy-900, text on the accent
SUCCESS = "#2F9E44"
WARNING = "#E8590C"
DANGER = "#E03131"
INFO = "#1C7ED6"

LOGO = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons", "geodit.svg")


def mix(a: QColor, b: QColor, t: float) -> QColor:
    """``a`` moved ``t`` (0..1) of the way towards ``b``."""
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def _hex(color: QColor) -> str:
    return color.name()


def _rgba(color: QColor, alpha: float) -> str:
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {round(alpha * 255)})"


@dataclass(frozen=True)
class Theme:
    dark: bool
    window: QColor
    text: QColor
    muted: QColor
    border: QColor
    surface: QColor
    field: QColor
    hover: QColor
    selected: QColor
    accent: QColor
    accent_text: QColor  # accent used as text/links: darker in light mode for contrast

    @staticmethod
    def from_palette(palette: QPalette) -> Theme:
        window = palette.color(QPalette.ColorRole.Window)
        base = palette.color(QPalette.ColorRole.Base)
        text = palette.color(QPalette.ColorRole.WindowText)
        dark = window.lightness() < 128
        accent = QColor(ACCENT)
        surface = mix(window, QColor("#ffffff"), 0.05) if dark else base
        return Theme(
            dark=dark,
            window=window,
            text=text,
            muted=mix(text, window, 0.42),
            border=mix(text, window, 0.80 if dark else 0.84),
            surface=surface,
            field=mix(base, window, 0.35) if dark else base,
            hover=mix(surface, accent, 0.07),
            selected=mix(surface, accent, 0.16),
            accent=accent,
            accent_text=QColor("#EDC18C") if dark else QColor(ACCENT_PRESSED),
        )

    @staticmethod
    def current() -> Theme:
        app = QApplication.instance()
        palette = app.palette() if app is not None else QPalette()
        return Theme.from_palette(palette)

    # ------------------------------------------------------------ tones
    def dot(self, name: str) -> QColor:
        """A saturated colour for status dots."""
        light, dark = {
            "success": ("#2F9E44", "#40C057"),
            "info": ("#1C7ED6", "#4DABF7"),
            "warning": ("#F08C00", "#FAB005"),
            "danger": ("#E03131", "#FF6B6B"),
        }.get(name, (None, None))
        if light is None:
            return self.muted
        return QColor(dark if self.dark else light)

    def tone(self, name: str) -> Tuple[QColor, QColor]:
        """``(background, foreground)`` for pills, dots and banners."""
        base = {
            "owner": QColor(ACCENT),
            "admin": QColor(INFO),
            "editor": QColor("#0CA678"),
            "success": QColor(SUCCESS),
            "warning": QColor(WARNING),
            "danger": QColor(DANGER),
            "info": QColor(INFO),
            "neutral": self.muted,
        }.get(name, self.muted)
        if self.dark:
            return mix(self.surface, base, 0.28), mix(base, QColor("#ffffff"), 0.35)
        return mix(self.surface, base, 0.14), mix(base, QColor("#000000"), 0.30)


def role_tone(role: int) -> str:
    return {1: "owner", 2: "admin", 3: "editor"}.get(int(role), "neutral")


def font(scale: float = 1.0, *, bold: bool = False) -> QFont:
    """The application font (the user's QGIS font setting), scaled."""
    app = QApplication.instance()
    f = QFont(app.font()) if app is not None else QFont()
    if scale != 1.0:
        size = f.pointSizeF()
        if size > 0:
            f.setPointSizeF(size * scale)
        else:
            f.setPixelSize(max(8, round(f.pixelSize() * scale)))
    f.setBold(bold)
    return f


def stylesheet(t: Theme) -> str:
    on_accent = NAVY
    accent_disabled = _hex(mix(t.surface, t.accent, 0.45))
    return f"""
QLabel[kind="muted"] {{ color: {_hex(t.muted)}; }}
QLabel[kind="section"] {{ color: {_hex(t.muted)}; }}
QLabel[kind="link"] {{ color: {_hex(t.accent_text)}; }}

QLineEdit[field="true"] {{
    min-height: 30px; padding: 0 8px;
    border: 1px solid {_hex(t.border)}; border-radius: 6px;
    background: {_hex(t.field)}; color: {_hex(t.text)};
    selection-background-color: {_rgba(t.accent, 0.45)};
}}
QLineEdit[field="true"]:focus {{ border: 1px solid {_hex(t.accent)}; }}
QLineEdit[field="true"]:disabled {{ color: {_hex(t.muted)}; }}
QLineEdit[code="true"] {{ min-height: 40px; }}

QPushButton[variant="secondary"], QPushButton[variant="primary"] {{
    min-height: 30px; padding: 0 14px; border-radius: 6px;
}}
QPushButton[variant="secondary"] {{
    border: 1px solid {_hex(t.border)}; background: {_hex(t.surface)}; color: {_hex(t.text)};
}}
QPushButton[variant="secondary"]:hover {{ background: {_hex(t.hover)}; }}
QPushButton[variant="secondary"]:pressed {{ background: {_hex(t.selected)}; }}
QPushButton[variant="secondary"]:disabled {{ color: {_hex(t.muted)}; }}
QPushButton[variant="primary"] {{
    border: 1px solid {_hex(t.accent)}; background: {_hex(t.accent)}; color: {on_accent}; font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[variant="primary"]:pressed {{ background: {ACCENT_PRESSED}; border-color: {ACCENT_PRESSED}; }}
QPushButton[variant="primary"]:disabled {{
    background: {accent_disabled}; border-color: {accent_disabled}; color: {_rgba(QColor(NAVY), 0.55)};
}}

QToolButton[variant="icon"] {{ border: none; border-radius: 6px; padding: 4px; background: transparent; }}
QToolButton[variant="icon"]:hover {{ background: {_hex(t.hover)}; }}
QToolButton[variant="icon"]:pressed {{ background: {_hex(t.selected)}; }}
QToolButton[variant="icon"]::menu-indicator {{ image: none; width: 0; }}
QToolButton[variant="link"] {{
    border: none; background: transparent; padding: 2px 0; color: {_hex(t.accent_text)};
}}
QToolButton[variant="link"]::menu-indicator {{ image: none; width: 0; }}
QToolButton[variant="back"] {{
    border: none; background: transparent; padding: 2px 4px 2px 0; color: {_hex(t.accent_text)};
}}

QToolButton[segment="first"], QToolButton[segment="last"] {{
    border: 1px solid {_hex(t.border)}; background: {_hex(t.surface)}; color: {_hex(t.muted)};
    padding: 4px 12px; min-height: 22px;
}}
QToolButton[segment="first"] {{ border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
QToolButton[segment="last"] {{ border-top-right-radius: 6px; border-bottom-right-radius: 6px; border-left: none; }}
QToolButton[segment="first"]:checked, QToolButton[segment="last"]:checked {{
    background: {_hex(t.selected)}; color: {_hex(t.text)}; font-weight: 600;
}}
QToolButton[segment="first"]:hover:!checked, QToolButton[segment="last"]:hover:!checked {{
    background: {_hex(t.hover)};
}}

QFrame[card="true"] {{ background: {_hex(t.surface)}; border: 1px solid {_hex(t.border)}; border-radius: 8px; }}
QFrame[divider="true"] {{ background: {_hex(t.border)}; border: none; max-height: 1px; min-height: 1px; }}

QListView[cards="true"] {{ background: transparent; border: none; outline: none; }}

QProgressBar[thin="true"] {{
    border: none; background: {_rgba(t.accent, 0.18)}; border-radius: 2px;
    min-height: 4px; max-height: 4px; text-align: center;
}}
QProgressBar[thin="true"]::chunk {{ background: {_hex(t.accent)}; border-radius: 2px; }}
"""


# ------------------------------------------------------------------ icons
_ICONS: Dict[str, str] = {
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>'
    '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    "back": '<path d="m15 18-6-6 6-6"/>',
    "more": '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
    "check": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "warning": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/>'
    '<path d="M12 9v4"/><path d="M12 17h.01"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "error": '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    "undo": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>',
    "map": '<path d="M14.106 5.553a2 2 0 0 0 1.788 0l3.659-1.83A1 1 0 0 1 21 4.619v12.764a1 1 0 0 1-.553.894'
    "l-4.553 2.277a2 2 0 0 1-1.788 0l-4.212-2.106a2 2 0 0 0-1.788 0l-3.659 1.83A1 1 0 0 1 3 19.381V6.618"
    'a1 1 0 0 1 .553-.894l4.553-2.277a2 2 0 0 1 1.788 0z"/><path d="M15 5.764v15"/><path d="M9 3.236v15"/>',
    "server": '<rect width="20" height="8" x="2" y="2" rx="2"/><rect width="20" height="8" x="2" y="14" rx="2"/>'
    '<path d="M6 6h.01"/><path d="M6 18h.01"/>',
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1'
    'c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
    "lock": '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
}


def _render_svg(svg: bytes, size: int, scale: float) -> QPixmap:
    px = max(1, round(size * scale))
    if QSvgRenderer is not None:
        renderer = QSvgRenderer(QByteArray(svg))
        pix = QPixmap(px, px)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter, QRectF(0, 0, px, px))
        painter.end()
    else:  # pragma: no cover
        from qgis.PyQt.QtGui import QImage

        pix = QPixmap.fromImage(QImage.fromData(QByteArray(svg), "SVG").scaled(px, px))
    pix.setDevicePixelRatio(scale)
    return pix


def line_icon(name: str, color: QColor, size: int = 16) -> QIcon:
    body = _ICONS[name]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color.name()}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    ).encode()
    icon = QIcon()
    for scale in (1.0, 2.0):
        icon.addPixmap(_render_svg(svg, size, scale))
    return icon


def logo_pixmap(size: int, scale: float = 2.0) -> QPixmap:
    with open(LOGO, "rb") as fh:
        return _render_svg(fh.read(), size, scale)


def device_scale(widget) -> float:
    try:
        return max(1.0, float(widget.devicePixelRatioF()))
    except (AttributeError, RuntimeError):
        return 2.0


def icon_size(px: int = 16) -> QSize:
    return QSize(px, px)
