"""Visual constants and QSS themes for ROVR.

Everything that controls appearance belongs here:
  - Layout constants (heights, minimum widths)
  - Theme stylesheets (QSS)
  - Shared color values
  - apply_theme() — applies the active stylesheet to the QApplication
"""

# ── Layout constants ──────────────────────────────────────────────────────────

TRAY_HEIGHT   = 40   # fixed height for all button trays
MIN_COL_WIDTH = 30   # minimum column width during interactive resize

# ── Row-highlight colors for new-activity indicator ──────────────────────────

NEW_ACTIVITY_LIGHT = '#dbeafe'   # light blue, readable on white
NEW_ACTIVITY_DARK  = '#1a3a5c'   # dark blue, readable on dark background

# ── Colored action buttons ───────────────────────────────────────────────────
# What the color means, not which button wears it: green moves a scene forward,
# blue opens the review dialog, red sends it back or ends it. Applied per widget
# rather than through a theme rule, because the light theme deliberately runs
# with no stylesheet at all and these must look the same either way. Shades are
# dark enough for white text to clear WCAG AA on both backgrounds.

BUTTON_GREEN = ('#2e7d32', '#1f5c24')   # (base, hover/pressed)
BUTTON_BLUE  = ('#1565c0', '#0d4a94')
BUTTON_RED   = ('#c62828', '#992020')

# Which labels get colored. Keyed by label because that is what both the
# SCENE_ACTIONS registry and the dashboards' explicit (label, handler) pairs
# have in common; a button whose label is absent keeps the plain theme look.
BUTTON_COLOR_BY_LABEL = {
    'Approve':        BUTTON_GREEN,
    'Submit':         BUTTON_GREEN,
    'Review':         BUTTON_BLUE,
    'Kick Back':      BUTTON_RED,
    'Mark Bad Scene': BUTTON_RED,
    'Reset Scene':    BUTTON_RED,
}


def color_button(btn):
    """Apply this button's color, if its label has one. No-op otherwise."""
    colors = BUTTON_COLOR_BY_LABEL.get(btn.text())
    if colors is None:
        return
    base, dark = colors
    # The full box is restated: a per-widget stylesheet outranks the app one for
    # the properties it names, and the padding/height set there must survive.
    btn.setStyleSheet(f"""
        QPushButton {{
            background-color: {base}; color: #ffffff;
            border: 1px solid {dark}; padding: 3px 10px;
            border-radius: 3px; min-height: 20px;
        }}
        QPushButton:hover    {{ background-color: {dark}; }}
        QPushButton:pressed  {{ background-color: {dark}; }}
        QPushButton:disabled {{ background-color: #6e6e6e; color: #cfcfcf; border-color: #5a5a5a; }}
    """)

# ── Dark theme (QSS) ─────────────────────────────────────────────────────────

DARK_STYLESHEET = """
QWidget                      { background-color: #1e1e1e; color: #d4d4d4; }
QMainWindow, QDialog         { background-color: #1e1e1e; }
QLabel                       { background: transparent; }

QGroupBox {
    border: 1px solid #3c3c3c; border-radius: 4px;
    margin-top: 6px; padding-top: 4px; color: #d4d4d4;
}
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }

QPushButton {
    background-color: #2d2d2d; color: #d4d4d4;
    border: 1px solid #444; padding: 3px 10px;
    border-radius: 3px; min-height: 20px;
}
QPushButton:hover    { background-color: #3e3e3e; }
QPushButton:pressed  { background-color: #094771; }
QPushButton:disabled { color: #555; border-color: #2d2d2d; }

QTableWidget {
    background-color: #1e1e1e; alternate-background-color: #252526;
    color: #d4d4d4; gridline-color: #2d2d2d; border: 1px solid #3c3c3c;
}
QTableWidget::item:selected { background-color: #094771; color: #d4d4d4; }
QHeaderView::section {
    background-color: #252526; color: #d4d4d4;
    border: 1px solid #3c3c3c; padding: 3px 6px;
}

QTextEdit, QLineEdit {
    background-color: #2d2d2d; color: #d4d4d4;
    border: 1px solid #3c3c3c; border-radius: 2px;
    padding: 2px; selection-background-color: #094771;
}
QPlainTextEdit {
    background-color: #2d2d2d; color: #d4d4d4;
    border: 1px solid #3c3c3c;
}

QComboBox {
    background-color: #2d2d2d; color: #d4d4d4;
    border: 1px solid #3c3c3c; border-radius: 3px;
    padding: 3px 6px; min-height: 20px;
}
QComboBox QAbstractItemView {
    background-color: #252526; color: #d4d4d4;
    selection-background-color: #094771;
}

QMenu {
    background-color: #252526; color: #d4d4d4;
    border: 1px solid #3c3c3c; padding: 2px 0;
}
QMenu::item              { padding: 5px 24px 5px 12px; }
QMenu::item:selected     { background-color: #094771; }
QMenu::item:checked      { color: #4fc1ff; }
QMenu::separator         { height: 1px; background: #3c3c3c; margin: 3px 0; }

QCheckBox { color: #d4d4d4; spacing: 6px; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: 1px solid #555; border-radius: 2px; background-color: #2d2d2d;
}
QCheckBox::indicator:checked { background-color: #007acc; border-color: #007acc; }

QScrollBar:vertical          { background: #1e1e1e; width: 10px; margin: 0; }
QScrollBar::handle:vertical  { background: #424242; border-radius: 5px; min-height: 20px; }
QScrollBar::handle:vertical:hover { background: #555; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal        { background: #1e1e1e; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #424242; border-radius: 5px; min-width: 20px; }
QScrollBar::handle:horizontal:hover { background: #555; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QSplitter::handle            { background: #3c3c3c; }
QSplitter::handle:horizontal { width: 2px; }
QSplitter::handle:vertical   { height: 2px; }

QToolTip { background-color: #252526; color: #d4d4d4; border: 1px solid #555; padding: 3px; }
"""

# ── Theme application ─────────────────────────────────────────────────────────

def apply_theme(dark: bool) -> None:
    """Apply or clear the dark stylesheet on the running QApplication."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app:
        app.setStyleSheet(DARK_STYLESHEET if dark else '')
