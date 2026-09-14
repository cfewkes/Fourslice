"""
fourslice/gui/icons.py

A small Lucide-style icon set rendered with QPainter, so the redesigned
screens get consistent stroke icons without adding a dependency (and
without shipping a pile of SVGs). Each icon is drawn on the 24x24 Lucide
viewBox at whatever logical size the caller asks for, at 2x device-pixel
ratio so it stays crisp on HiDPI displays.

Icons available: download, bar-chart-2, compass, play-circle, moon,
search, chevron-down, chevron-right, settings. The geometry is copied from the
Lucide set (MIT licensed); fills (compass needle, play triangle, moon)
match Lucide's filled-by-default shapes.

Usage:
    pixmap = lucide("download", "#18181B", size=18)
    button.setIcon(QIcon(lucide("chevron-right", "#FFFFFF", size=16)))
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
)

_RATIO = 2.0          # device-pixel ratio the pixmaps are rendered at
_STROKE = 2.0         # Lucide's stroke width, in the 24x24 viewBox


def _new_canvas(size):
    """A transparent 24x24-viewBox pixmap sized for `size` logical px."""
    pm = QPixmap(int(size * _RATIO), int(size * _RATIO))
    pm.setDevicePixelRatio(_RATIO)
    pm.fill(Qt.GlobalColor.transparent)
    return pm


def _fill_color(painter):
    """Fill with the same color the current stroke pen uses (Lucide's
    filled shapes are filled with currentColor)."""
    return QColor(painter.pen().color())


def _download(p):
    # tray: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" (open at the top)
    tray = QPainterPath(QPointF(21, 15))
    tray.lineTo(QPointF(21, 19))
    tray.cubicTo(QPointF(21, 20.1), QPointF(20.1, 21), QPointF(19, 21))
    tray.lineTo(QPointF(5, 21))
    tray.cubicTo(QPointF(3.9, 21), QPointF(3, 20.1), QPointF(3, 19))
    tray.lineTo(QPointF(3, 15))
    p.drawPath(tray)
    p.drawPolyline(QPolygonF([
        QPointF(7, 10), QPointF(12, 15), QPointF(17, 10),
    ]))
    p.drawLine(QPointF(12, 15), QPointF(12, 3))


def _bar_chart(p):
    p.drawLine(QPointF(18, 20), QPointF(18, 10))
    p.drawLine(QPointF(12, 20), QPointF(12, 4))
    p.drawLine(QPointF(6, 20), QPointF(6, 14))


def _compass(p):
    p.drawEllipse(QPointF(12, 12), 10, 10)
    p.drawLine(QPointF(2,12), QPointF(9,12))
    p.drawLine(QPointF(15,12), QPointF(22,12))
    p.drawEllipse(QPointF(12, 12), 3, 3)
    p.setBrush(Qt.BrushStyle.NoBrush)


def _play_circle(p):
    p.drawEllipse(QPointF(12, 12), 10, 10)
    triangle = QPolygonF([
        QPointF(10, 8), QPointF(16, 12), QPointF(10, 16),
    ])
    p.setBrush(_fill_color(p))
    p.drawPolygon(triangle)
    p.setBrush(Qt.BrushStyle.NoBrush)


def _moon(p):
    # Crescent: outer right half of the 18px circle, concave inner edge.
    p.drawEllipse(QPointF(12, 12), 9, 9)
    p.drawArc(QRectF(3, 3, 18, 18), 270, 180)

def _search(p):
    p.drawEllipse(QPointF(11, 11), 8, 8)
    p.drawLine(QPointF(21, 21), QPointF(16.65, 16.65))


def _chevron_down(p):
    p.drawPolyline(QPolygonF([
        QPointF(6, 9), QPointF(12, 15), QPointF(18, 9),
    ]))


def _chevron_right(p):
    p.drawPolyline(QPolygonF([
        QPointF(9, 18), QPointF(15, 12), QPointF(9, 6),
    ]))


def _sun(p):
    p.drawEllipse(QPointF(12, 12), 4, 4)
    # rays
    p.drawLine(QPointF(12, 2), QPointF(12, 4))
    p.drawLine(QPointF(12, 20), QPointF(12, 22))
    p.drawLine(QPointF(4.22, 4.22), QPointF(5.64, 5.64))
    p.drawLine(QPointF(18.36, 18.36), QPointF(19.78, 19.78))
    p.drawLine(QPointF(1, 12), QPointF(3, 12))
    p.drawLine(QPointF(21, 12), QPointF(23, 12))
    p.drawLine(QPointF(4.22, 19.78), QPointF(5.64, 18.36))
    p.drawLine(QPointF(18.36, 5.64), QPointF(19.78, 4.22))


def _rotate_cw(p):
    path = QPainterPath()
    path.arcMoveTo(QRectF(3, 3, 18, 18), 45)
    path.arcTo(QRectF(3, 3, 18, 18), 45, 270)
    p.drawPath(path)
    p.drawPolyline(QPolygonF([
        QPointF(21, 3), QPointF(21, 9), QPointF(15, 9),
    ]))


def _play(p):
    triangle = QPolygonF([
        QPointF(9, 6), QPointF(19, 12), QPointF(9, 18),
    ])
    p.setBrush(_fill_color(p))
    p.drawPolygon(triangle)
    p.setBrush(Qt.BrushStyle.NoBrush)


def _pause(p):
    p.drawLine(QPointF(10, 5), QPointF(10, 19))
    p.drawLine(QPointF(14, 5), QPointF(14, 19))


def _skip_back(p):
    p.drawLine(QPointF(5, 5), QPointF(5, 19))
    triangle = QPolygonF([
        QPointF(19, 6), QPointF(9, 12), QPointF(19, 18),
    ])
    p.setBrush(_fill_color(p))
    p.drawPolygon(triangle)
    p.setBrush(Qt.BrushStyle.NoBrush)


def _skip_forward(p):
    triangle = QPolygonF([
        QPointF(5, 6), QPointF(15, 12), QPointF(5, 18),
    ])
    p.setBrush(_fill_color(p))
    p.drawPolygon(triangle)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(QPointF(19, 5), QPointF(19, 19))


def _edit(p):
    p.drawPolyline(QPolygonF([
        QPointF(17, 3), QPointF(21, 7), QPointF(7, 21),
        QPointF(3, 21), QPointF(3, 17), QPointF(17, 3),
    ]))


def _plus(p):
    p.drawLine(QPointF(12, 5), QPointF(12, 19))
    p.drawLine(QPointF(5, 12), QPointF(19, 12))


def _settings(p):
    """Lucide-style gear / settings icon (simplified 12-tooth gear)."""
    import math
    cx, cy = 12.0, 12.0
    for i in range(12):
        a = math.pi * 2 * i / 12
        p.drawLine(
            QPointF(cx + 7.5 * math.cos(a), cy + 7.5 * math.sin(a)),
            QPointF(cx + 9.5 * math.cos(a), cy + 9.5 * math.sin(a)),
        )
    p.drawEllipse(QPointF(cx, cy), 7.5, 7.5)
    p.drawEllipse(QPointF(cx, cy), 3, 3)


_DRAWERS = {
    "download": _download,
    "bar-chart-2": _bar_chart,
    "compass": _compass,
    "play-circle": _play_circle,
    "moon": _moon,
    "sun": _sun,
    "search": _search,
    "chevron-down": _chevron_down,
    "chevron-right": _chevron_right,
    "rotate-cw": _rotate_cw,
    "refresh": _rotate_cw,
    "play": _play,
    "pause": _pause,
    "skip-back": _skip_back,
    "skip-forward": _skip_forward,
    "edit": _edit,
    "plus": _plus,
    "settings": _settings,
}


def lucide(name, color, size=20):
    """Return a QPixmap of the named icon at `size` logical px."""
    draw = _DRAWERS[name]
    pm = _new_canvas(size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    scale = size / 24.0
    p.scale(scale, scale)
    pen = QPen(QColor(color), _STROKE,
               Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
               Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    draw(p)
    p.end()
    return pm


def lucide_icon(name, color, size=20):
    """Return a QIcon wrapping the named icon's pixmap."""
    return QIcon(lucide(name, color, size))
