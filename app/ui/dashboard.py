"""base dashboard — shared layout, widgets, and utilities for all dashboard types"""
import os
import re
import subprocess
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter,
    QDialog, QTextEdit, QDialogButtonBox, QMessageBox, QFileDialog,
)
from PyQt6.QtCore import Qt
from app.models import SceneStatus
from app.local_settings import get_roi_studio_path, set_roi_studio_path
from app.db import get_scene_thread, add_note, get_scene_by_id
from config import PANCAM_PATH
try:
    from app.version import __version__
except ImportError:
    __version__ = "dev"

# Parses 'MERB/sol0003/P2350/obs0' → ('MERB', '0003', 'P2350', '0')
_KEY_RE = re.compile(r'^(MER[AB])/sol(\d{4})/([^/]+)/obs(\d+)$')

# Fixed height for all button trays across all dashboards
TRAY_HEIGHT = 40


def parse_scene_key(scene_key):
    """Return (rover, sol, seq_id, obs) from a scene_key, or ('','',scene_key,'0') if unparseable."""
    m = _KEY_RE.match(scene_key)
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(4)
    return '', '', scene_key, '0'


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


_DECISION_LABEL = {
    'request_revision': 'Kick Back',
    'status_override':  'Status Override',
    'submitted':        'Submitted',
    'reset':            'Reset',
    'approve':          'Approve',
    'approved':         'Approved',
    'force_released':   'Force Released',
}


class NotesDialog(QDialog):
    """Read-write dialog showing the note thread for a scene and allowing new notes."""
    def __init__(self, conn, scene_id, scene_name, author_id, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.scene_id = scene_id
        self.author_id = author_id
        self.setWindowTitle(f"Notes — {scene_name}")
        self.setMinimumSize(520, 440)
        layout = QVBoxLayout(self)

        self.display = QTextEdit()
        self.display.setReadOnly(True)
        layout.addWidget(self.display)

        layout.addWidget(QLabel("Add a note:"))
        self.input = QTextEdit()
        self.input.setFixedHeight(80)
        self.input.setPlaceholderText("Type a note...")
        layout.addWidget(self.input)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        add_btn = QPushButton("Add Note")
        add_btn.clicked.connect(self._on_add)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(close_btn)
        layout.addWidget(btn_row)

        self._refresh()

    def _refresh(self):
        thread = get_scene_thread(self.conn, self.scene_id)
        if not thread:
            self.display.setPlainText("No notes yet.")
            return
        parts = []
        for row in thread:
            if row['type'] == 'note':
                tag = 'Note'
            else:
                tag = _DECISION_LABEL.get(row['decision'], row['decision'])
            parts.append(
                f"[{row['timestamp']}]  {row['author_name']}  ({tag})\n{row['content']}"
            )
        self.display.setPlainText("\n\n---\n\n".join(parts))

    def _on_add(self):
        body = self.input.toPlainText().strip()
        if not body:
            QMessageBox.warning(self, "Empty Note", "Please enter some text before adding a note.")
            return
        add_note(self.conn, self.scene_id, self.author_id, body)
        self.input.clear()
        self._refresh()


class Dashboard(QMainWindow):
    def __init__(self, conn, user):
        super().__init__()
        self.conn = conn
        self.user = user
        self.setWindowTitle(f"ROVR {__version__} — {user['username']}")
        self.setMinimumSize(800, 500)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        outer_layout = QVBoxLayout(central_widget)
        outer_layout.setContentsMargins(6, 4, 6, 4)
        outer_layout.setSpacing(4)

        # Topbar
        topbar = QWidget()
        topbar.setFixedHeight(32)
        topbar_layout = QHBoxLayout(topbar)
        topbar_layout.setContentsMargins(2, 0, 2, 0)
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
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(4)
        outer_layout.addLayout(bottom_layout)

        self.sidebar = QWidget()
        self.sidebar_layout = QVBoxLayout(self.sidebar)
        self.sidebar_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(self.sidebar, stretch=0)

        self.main_content = QWidget()
        self.main_content_layout = QVBoxLayout(self.main_content)
        self.main_content_layout.setContentsMargins(0, 0, 0, 0)
        self.main_content_layout.setSpacing(4)
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

    def handle_open_roi(self, scene_id):
        """Launch ROI Studio and open the given scene."""
        path = get_roi_studio_path()
        if not path or not os.path.isfile(path):
            path, _ = QFileDialog.getOpenFileName(
                self, "Locate ROI Studio", "", "Executables (*.exe)"
            )
            if not path:
                return
            set_roi_studio_path(path)

        args = [path]
        scene = get_scene_by_id(self.conn, scene_id)
        if scene:
            # scene_key: "MERB/sol0003/P2350/obs0"
            rover, sol_dir, seq_id, _ = scene['scene_key'].split('/')
            obs_ix = scene['obs_ix'] if scene['obs_ix'] is not None else 0
            folder_path = os.path.join(PANCAM_PATH, rover, 'iof', sol_dir)
            args += [folder_path, seq_id, str(obs_ix), 'PCAM']
            if scene['roi_filename']:
                args.append(scene['roi_filename'])

        try:
            subprocess.Popen(args)
        except OSError as e:
            QMessageBox.warning(self, "Launch Failed", f"Could not open ROI Studio:\n{e}")

    def _show_notes(self, scene_id, scene_name):
        NotesDialog(self.conn, scene_id, scene_name, self.user['id'], self).exec()

    def handle_logout(self):
        from app.ui.login import LoginUI
        self.login = LoginUI(self.conn)
        self.login.show()
        self.close()
