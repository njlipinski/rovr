"""supervisor dashboard — approval queue and master scene list"""
import subprocess
from PyQt6.QtWidgets import (
    QPushButton, QSplitter, QMessageBox, QTableWidgetItem,
    QDialog, QVBoxLayout, QLabel, QComboBox, QTextEdit, QDialogButtonBox,
)
from PyQt6.QtCore import Qt
from config import ROI_STUDIO_PATH
from app.ui.dashboard import (
    Dashboard, KickBackDialog, NotesDialog,
    make_scene_table, make_button_tray, make_section, clear_tray,
    parse_scene_name, TRAY_HEIGHT
)
from app.db import get_supervisor_queue, get_all_scenes, get_scene_history
from app.controller import supervisor_review_scene, supervisor_set_status, supervisor_reset_scene
from app.models import SceneStatus, Decision


class SetStatusDialog(QDialog):
    """Dialog for supervisor to select a new status for a scene."""
    def __init__(self, current_status, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Scene Status")
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Current status: {SceneStatus.LABELS[current_status]}"))
        layout.addWidget(QLabel("New status:"))
        self.combo = QComboBox()
        for status, label in SceneStatus.LABELS.items():
            if status != current_status:
                self.combo.addItem(label, status)
        layout.addWidget(self.combo)
        layout.addWidget(QLabel("Notes (optional):"))
        self.notes = QTextEdit()
        self.notes.setFixedHeight(72)
        layout.addWidget(self.notes)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_status(self):
        return self.combo.currentData()

    def get_notes(self):
        return self.notes.toPlainText().strip() or None

# Master list columns: header label → field name on the query row
_MASTER_COLS = [
    ("ID",            "id"),
    ("Rover",         None),   # parsed from name
    ("Sol",           None),   # parsed from name
    ("SeqID",         None),   # parsed from name
    ("Status",        "status"),
    ("Owner",         "owner_username"),
    ("Assigned To",   "assigned_to_username"),
    ("Peer Reviewer", "peer_reviewer_username"),
    ("Supervisor",    "supervisor_username"),
    ("Claimed By",    "claimed_by_username"),
    ("ROI File",      "roi_filename"),
    ("Submitted",     "submitted_at"),
    ("Updated",       "updated_at"),
]


class SupervisorDashboard(Dashboard):

    def __init__(self, conn, user):
        super().__init__(conn, user)
        self._build_main_content()

    # ── Build UI ────────────────────────────────────────────────────────

    def _build_main_content(self):
        # Pending Approval queue (status 5)
        self.approval_table = make_scene_table(
            ["ID", "Rover", "Sol", "SeqID", "Status", "Owner"]
        )
        self.approval_tray = make_button_tray()
        self.approval_table.itemSelectionChanged.connect(self._update_approval_tray)
        approval_section = make_section(
            "Pending Approval", self.approval_table, self.approval_tray
        )

        # Master scene list
        self.master_table = make_scene_table([h for h, _ in _MASTER_COLS])
        self.master_tray = make_button_tray()
        self.master_table.itemSelectionChanged.connect(self._update_master_tray)
        master_section = make_section("All Scenes", self.master_table, self.master_tray)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(approval_section)
        splitter.addWidget(master_section)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_task_list)

        self.main_content_layout.addWidget(splitter)
        self.main_content_layout.addWidget(refresh_button)

        self.refresh_task_list()

    # ── Populate tables ─────────────────────────────────────────────────

    def refresh_task_list(self):
        def fill_approval(i, scene):
            rover, sol, seq = parse_scene_name(scene['name'])
            self.approval_table.setItem(i, 0, QTableWidgetItem(str(scene['id'])))
            self.approval_table.setItem(i, 1, QTableWidgetItem(rover))
            self.approval_table.setItem(i, 2, QTableWidgetItem(sol))
            self.approval_table.setItem(i, 3, QTableWidgetItem(seq))
            self.approval_table.setItem(i, 4, QTableWidgetItem(SceneStatus.LABELS[scene['status']]))
            self.approval_table.setItem(i, 5, QTableWidgetItem(scene['owner_username'] or '—'))
        self._fill_table(self.approval_table, get_supervisor_queue(self.conn), fill_approval)

        def fill_master(i, scene):
            rover, sol, seq = parse_scene_name(scene['name'])
            parsed = {'rover': rover, 'sol': sol, 'seqid': seq}
            for col, (_, field) in enumerate(_MASTER_COLS):
                if field is None:
                    val = parsed.get(_MASTER_COLS[col][0].lower(), '')
                elif field == 'status':
                    val = SceneStatus.LABELS.get(scene[field], str(scene[field]))
                else:
                    val = scene[field] or '—'
                self.master_table.setItem(i, col, QTableWidgetItem(str(val)))
        self._fill_table(self.master_table, get_all_scenes(self.conn), fill_master)

        self._update_approval_tray()
        self._update_master_tray()

    # ── Button tray ─────────────────────────────────────────────────────

    def _update_approval_tray(self):
        clear_tray(self.approval_tray)
        if self.selected_id(self.approval_table) is None:
            return
        for label, handler in [
            ("Open in ROI Studio", "handle_open_roi"),
            ("Approve",            "handle_approve"),
            ("Kick Back",          "handle_kick_back"),
            ("See Notes",          "handle_see_notes"),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(getattr(self, handler))
            self.approval_tray.layout().addWidget(btn)

    def _update_master_tray(self):
        clear_tray(self.master_tray)
        if self.selected_id(self.master_table) is None:
            return
        for label, handler in [
            ("Set Status",   "handle_set_status"),
            ("Reset Scene",  "handle_reset_scene"),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(getattr(self, handler))
            self.master_tray.layout().addWidget(btn)

    # ── Handlers ────────────────────────────────────────────────────────

    def _approval_scene_id(self):
        scene_id = self.selected_id(self.approval_table)
        if scene_id is None:
            QMessageBox.warning(self, "No Selection", "Select a scene first.")
        return scene_id

    def handle_open_roi(self):
        scene_id = self._approval_scene_id()
        if scene_id is None:
            return
        try:
            subprocess.Popen([ROI_STUDIO_PATH])
        except OSError as e:
            QMessageBox.warning(self, "Launch Failed", f"Could not open ROI Studio:\n{e}")

    def handle_approve(self):
        scene_id = self._approval_scene_id()
        if scene_id is None:
            return
        try:
            supervisor_review_scene(self.conn, scene_id, self.user['id'], Decision.APPROVE, None)
        except ValueError as e:
            QMessageBox.warning(self, "Approve Failed", str(e))
            self.refresh_task_list()
            return
        QMessageBox.information(self, "Approved", "Scene approved.")
        self.refresh_task_list()

    def handle_kick_back(self):
        scene_id = self._approval_scene_id()
        if scene_id is None:
            return
        dialog = KickBackDialog(self)
        if dialog.exec() != KickBackDialog.DialogCode.Accepted:
            return
        comments = dialog.get_comments()
        try:
            supervisor_review_scene(self.conn, scene_id, self.user['id'], Decision.REQUEST_REVISION, comments)
        except ValueError as e:
            QMessageBox.warning(self, "Kick Back Failed", str(e))
            self.refresh_task_list()
            return
        QMessageBox.information(self, "Kicked Back", "Scene returned to analyst with notes.")
        self.refresh_task_list()

    def handle_see_notes(self):
        scene_id = self._approval_scene_id()
        if scene_id is None:
            return
        row = self.approval_table.currentRow()
        scene_name = " ".join([
            self.approval_table.item(row, c).text() for c in (1, 2, 3)
        ])
        history = get_scene_history(self.conn, scene_id)
        NotesDialog(scene_name, history, self).exec()

    def handle_set_status(self):
        scene_id = self.selected_id(self.master_table)
        if scene_id is None:
            return
        current = self.selected_status(self.master_table)
        if current is None:
            return
        dialog = SetStatusDialog(current, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            supervisor_set_status(self.conn, scene_id, self.user['id'],
                                  dialog.get_status(), dialog.get_notes())
        except ValueError as e:
            QMessageBox.warning(self, "Set Status Failed", str(e))
        self.refresh_task_list()

    def handle_reset_scene(self):
        scene_id = self.selected_id(self.master_table)
        if scene_id is None:
            return
        row = self.master_table.currentRow()
        cells = [self.master_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        confirm = QMessageBox.question(
            self, "Reset Scene",
            f"Reset '{scene_name}'?\n\nThis will clear all ownership and return it to unclaimed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            supervisor_reset_scene(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Reset Failed", str(e))
        self.refresh_task_list()
