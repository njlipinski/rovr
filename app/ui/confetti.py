"""Confetti burst for the While You Were Away summary.

A click-through child widget over the dashboard rather than a window of its own:
as a child it needs no platform window flags, and it still animates behind the
modal dialog, whose nested event loop keeps delivering timer and paint events.
"""
import random

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QWidget

from app.ui.styles import CONFETTI_COLORS

_DENSITY = 0.2       # pieces per px of window width, so a wide window is not sparser
_INTERVAL_MS = 33    # ~30 fps
_GRAVITY = 0.3
_MAX_FALL = 9        # terminal velocity, px/frame


class ConfettiOverlay(QWidget):
    """Falling confetti over `parent`, deleting itself once it has all landed."""

    def __init__(self, parent):
        super().__init__(parent)
        # Click-through, and no background of its own, so the dashboard stays
        # usable and visible underneath.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(parent.rect())
        # [x, y, x velocity, y velocity, size, color]
        self._pieces = [
            [random.uniform(0, self.width()), random.uniform(-0.6 * self.height(), 0),
            random.uniform(-1.2, 1.2), random.uniform(1, 3),
            random.uniform(5, 11), QColor(random.choice(CONFETTI_COLORS))]
            for _ in range(int(_DENSITY * self.width()))
        ]
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(_INTERVAL_MS)
        self.show()
        self.raise_()

    def _tick(self):
        for p in self._pieces:
            p[3] = min(p[3] + _GRAVITY, _MAX_FALL)
            p[0] += p[2]
            p[1] += p[3]
        if all(p[1] > self.height() for p in self._pieces):
            self._timer.stop()
            self.deleteLater()
        else:
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        for x, y, _, _, size, color in self._pieces:
            painter.fillRect(int(x), int(y), int(size * 0.6), int(size), color)
        painter.end()
