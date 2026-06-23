"""base dashboard — shared layout, widgets, and utilities for all dashboard types"""
import re
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter,
    QDialog, QTextEdit, QDialogButtonBox, QMessageBox
)
from PyQt6.QtCore import Qt
from app.models import SceneStatus

# Parses 'MERAsol0042seqID2210' → ('MERA', '0042', '2210')
_NAME_RE = re.compile(r'^([A-Z]+)sol(\d{4})seqID(\d+)$')

# Fixed height for all button trays across all dashboards
TRAY_HEIGHT = 40


def parse_scene_name(name):
    """Return (rover, sol, seqID) from a scene name, or ('', '', name) if unparseable."""
    m = _NAME_RE.match(name)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return '', '', name


def make_scene_table(headers):
    """Return a sortable QTableWidget with the given column headers; col 0 (ID) is always hidden."""
    table = QTableWidget()
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.horizontalHeader().setSortIndicatorShown(True)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setColumnHidden(0, True)
    table.setSortingEnabled(True)
    return table


def make_button_tray():
    """Return a fixed-height QWidget containing an HBoxLayout for context-sensitive buttons."""
    tray = QWidget()
    tray.setFixedHeight(TRAY_HEIGHT)
    tray.setLayout(QHBoxLayout())
    tray.layout().setContentsMargins(0, 0, 0, 0)
    return tray


def clear_tray(tray):
    """Remove all buttons from a tray."""
    layout = tray.layout()
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()


def make_section(label_text, table, tray):
    """Wrap a label + table + button tray into a QSplitter-compatible widget."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 4, 0, 4)
    layout.addWidget(QLabel(label_text))
    layout.addWidget(table)
    layout.addWidget(tray)
    return widget


class KickBackDialog(QDialog):
    """Modal dialog to collect kick-back comments. Requires non-empty text before accepting."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kick Back — Add Notes")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Notes for analyst (required):"))
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("Describe what needs to be revised...")
        layout.addWidget(self.text_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self):
        if not self.text_edit.toPlainText().strip():
            QMessageBox.warning(self, "Notes Required", "Please enter notes before kicking back.")
            return
        self.accept()

    def get_comments(self):
        return self.text_edit.toPlainText().strip()


class NotesDialog(QDialog):
    """Read-only dialog showing all review notes for a scene, oldest first."""
    def __init__(self, scene_name, history, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Notes — {scene_name}")
        self.setMinimumSize(500, 300)
        layout = QVBoxLayout(self)
        text = QTextEdit()
        text.setReadOnly(True)
        notes = [row for row in history if row['comments']]
        if notes:
            text.setPlainText(
                "\n\n---\n\n".join(
                    f"[{row['timestamp']}]  {row['reviewer_name']}  ({row['stage']})\n{row['comments']}"
                    for row in notes
                )
            )
        layout.addWidget(text)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)


class Dashboard(QMainWindow):
    def __init__(self, conn, user):
        super().__init__()
        self.conn = conn
        self.user = user
        self.setWindowTitle(f"ROVR — {user['username']}")
        self.setMinimumSize(800, 500)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        outer_layout = QVBoxLayout(central_widget)

        # Topbar
        topbar = QWidget()
        topbar_layout = QHBoxLayout(topbar)
        topbar_layout.addWidget(QLabel("ROVR"))
        topbar_layout.addStretch()
        topbar_layout.addWidget(QLabel(self.user['username']))
        topbar_layout.addStretch()
        logout_button = QPushButton("Logout")
        logout_button.clicked.connect(self.handle_logout)
        topbar_layout.addWidget(logout_button)
        outer_layout.addWidget(topbar)

        # Bottom: sidebar + main content
        bottom_layout = QHBoxLayout()
        outer_layout.addLayout(bottom_layout)

        self.sidebar = QWidget()
        self.sidebar_layout = QVBoxLayout(self.sidebar)
        bottom_layout.addWidget(self.sidebar, stretch=0)

        self.main_content = QWidget()
        self.main_content_layout = QVBoxLayout(self.main_content)
        bottom_layout.addWidget(self.main_content, stretch=1)

    # ── Shared helpers available to all subclasses ──────────────────────

    @staticmethod
    def _fill_table(table, rows, fill_fn):
        """Populate a table safely: disables sorting during insert to prevent mid-fill reorders."""
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            fill_fn(i, row)
        table.setSortingEnabled(True)

    def selected_id(self, table):
        """Return scene ID from hidden col 0 of the selected row, or None."""
        row = table.currentRow()
        if row < 0 or not table.selectedItems():
            return None
        return int(table.item(row, 0).text())

    def selected_status(self, table):
        """Return status int from col 4 of the selected row, or None."""
        row = table.currentRow()
        if row < 0 or not table.selectedItems():
            return None
        item = table.item(row, 4)
        if not item:
            return None
        label = item.text()
        for status, name in SceneStatus.LABELS.items():
            if name == label:
                return status
        return None

    def handle_logout(self):
        from app.ui.login import LoginUI
        self.login = LoginUI(self.conn)
        self.login.show()
        self.close()
