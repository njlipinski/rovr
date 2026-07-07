"""supervisor dashboard — pool, personal queue, in-progress, and master list"""
from PyQt6.QtWidgets import (
    QPushButton, QSplitter, QMessageBox, QTableWidgetItem,
    QDialog, QVBoxLayout, QLabel, QComboBox, QDialogButtonBox,
)
from PyQt6.QtCore import Qt
from app.ui.dashboard import (
    Dashboard, KickBackDialog, WordSelectTextEdit,
    make_scene_table, make_button_tray, make_section, clear_tray,
    parse_scene_key, apply_flag_delegate, make_flag_item,
)
from app.db import (
    get_supervisor_queue, get_supervisor_my_queue, get_supervisor_in_progress,
    get_all_scenes, get_scene_by_id, get_all_users, get_user_by_id,
)
from app.controller import (
    claim_for_supervisor_review, release_supervisor_review,
    supervisor_review_scene, supervisor_edit_scene, supervisor_reset_scene,
    mark_scene_issues,
)
from app.models import SceneStatus, Decision, Role


def _make_user_combo(conn, roles, current_id):
    """QComboBox listing active users with the given role(s), plus a "— None —"
    entry. If current_id refers to a user outside that set (e.g. deactivated),
    it's still appended so the combo can show and preserve that assignment."""
    combo = QComboBox()
    combo.addItem("— None —", None)
    listed_ids = set()
    for u in get_all_users(conn):
        if u['role'] in roles and u['active']:
            combo.addItem(u['username'], u['id'])
            listed_ids.add(u['id'])
    if current_id is not None and current_id not in listed_ids:
        current_user = get_user_by_id(conn, current_id)
        label = f"{current_user['username']} (inactive)" if current_user else f"user #{current_id} (deleted)"
        combo.addItem(label, current_id)
    idx = combo.findData(current_id)
    combo.setCurrentIndex(idx if idx >= 0 else 0)
    return combo


class EditSceneDialog(QDialog):
    """Supervisor admin dialog: edit a scene's status and directly reassign
    its owner, peer reviewer, supervisor, and claimed-by fields."""
    def __init__(self, conn, scene, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Scene")
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Status:"))
        self.status_combo = QComboBox()
        for status, label in SceneStatus.LABELS.items():
            self.status_combo.addItem(label, status)
        idx = self.status_combo.findData(scene['status'])
        self.status_combo.setCurrentIndex(idx if idx >= 0 else 0)
        layout.addWidget(self.status_combo)

        layout.addWidget(QLabel("Owner:"))
        self.owner_combo = _make_user_combo(conn, {Role.ANALYST}, scene['owner_id'])
        layout.addWidget(self.owner_combo)

        layout.addWidget(QLabel("Peer Reviewer:"))
        self.peer_combo = _make_user_combo(conn, {Role.ANALYST}, scene['peer_reviewer_id'])
        layout.addWidget(self.peer_combo)

        layout.addWidget(QLabel("Supervisor:"))
        self.supervisor_combo = _make_user_combo(conn, {Role.SUPERVISOR}, scene['supervisor_id'])
        layout.addWidget(self.supervisor_combo)

        layout.addWidget(QLabel("Claimed By:"))
        self.claimed_combo = _make_user_combo(conn, {Role.ANALYST, Role.SUPERVISOR}, scene['claimed_by'])
        layout.addWidget(self.claimed_combo)

        layout.addWidget(QLabel("Notes (optional):"))
        self.notes = WordSelectTextEdit()
        self.notes.setFixedHeight(72)
        layout.addWidget(self.notes)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_status(self):
        return self.status_combo.currentData()

    def get_owner_id(self):
        return self.owner_combo.currentData()

    def get_peer_reviewer_id(self):
        return self.peer_combo.currentData()

    def get_supervisor_id(self):
        return self.supervisor_combo.currentData()

    def get_claimed_by(self):
        return self.claimed_combo.currentData()

    def get_notes(self):
        return self.notes.toPlainText().strip() or None


# Master list columns: header label → field name (None = parsed from scene_key)
_MASTER_COLS = [
    ("ID",            "id"),
    ("Rover",         None),
    ("Sol",           None),
    ("SeqID",         None),
    ("Obs",           None),
    ("Name",          "name"),
    ("Status",        "status"),
    ("Flags",         "flags"),
    ("Owner",         "owner_username"),
    ("Peer Reviewer", "peer_reviewer_username"),
    ("Supervisor",    "supervisor_username"),
    ("Claimed By",    "claimed_by_username"),
    ("Updated",       "updated_at"),
]


class SupervisorDashboard(Dashboard):

    def __init__(self, conn, user):
        super().__init__(conn, user)
        self._build_main_content()

    # ── Build UI ────────────────────────────────────────────────────────

    def _build_main_content(self):
        # My Work Queue — scenes claimed by this supervisor (status 6)
        self.my_queue_table = make_scene_table(
            ["ID", "Rover", "Sol", "SeqID", "Obs", "Owner", "Flags", "Updated"]
        )
        apply_flag_delegate(self.my_queue_table)
        self.my_queue_tray = make_button_tray()
        self.my_queue_table.itemSelectionChanged.connect(self._update_my_queue_tray)
        my_queue_section = make_section(
            "My Work Queue", self.my_queue_table, self.my_queue_tray
        )

        # Supervisor Pool — shared, unclaimed (status 5)
        self.pool_table = make_scene_table(
            ["ID", "Rover", "Sol", "SeqID", "Status", "Obs", "Owner", "Flags", "Updated"]
        )
        apply_flag_delegate(self.pool_table)
        self.pool_tray = make_button_tray()
        self.pool_table.itemSelectionChanged.connect(self._update_pool_tray)
        pool_section = make_section(
            "Supervisor Pool", self.pool_table, self.pool_tray
        )

        # In Progress — scenes I kicked back, still being revised (status 4)
        self.in_progress_table = make_scene_table(
            ["ID", "Rover", "Sol", "SeqID", "Obs", "Status", "Owner", "Flags", "Updated"]
        )
        apply_flag_delegate(self.in_progress_table)
        self.in_progress_tray = make_button_tray()
        self.in_progress_table.itemSelectionChanged.connect(self._update_in_progress_tray)
        in_progress_section = make_section(
            "In Progress", self.in_progress_table, self.in_progress_tray
        )

        # Master scene list
        self.master_table = make_scene_table([h for h, _ in _MASTER_COLS])
        apply_flag_delegate(self.master_table)
        self.master_tray = make_button_tray()
        self.master_table.itemSelectionChanged.connect(self._update_master_tray)
        master_section = make_section("All Scenes", self.master_table, self.master_tray)

        left_splitter = QSplitter(Qt.Orientation.Vertical)
        left_splitter.addWidget(my_queue_section)
        left_splitter.addWidget(in_progress_section)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.addWidget(pool_section)

        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        top_splitter.addWidget(left_splitter)
        top_splitter.addWidget(right_splitter)

        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.addWidget(top_splitter)
        main_splitter.addWidget(master_section)
        main_splitter.setStretchFactor(0, 1)
        main_splitter.setStretchFactor(1, 2)

        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_task_list)

        self.main_content_layout.addWidget(main_splitter)
        self.main_content_layout.addWidget(refresh_button)

        self.refresh_task_list()

    # ── Populate tables ─────────────────────────────────────────────────

    def refresh_task_list(self):
        sup_id = self.user['id']

        def fill_my_queue(i, scene):
            rover, sol, seq_id, obs = parse_scene_key(scene['scene_key'])
            self.my_queue_table.setItem(i, 0, QTableWidgetItem(str(scene['id'])))
            self.my_queue_table.setItem(i, 1, QTableWidgetItem(rover))
            self.my_queue_table.setItem(i, 2, QTableWidgetItem(sol))
            self.my_queue_table.setItem(i, 3, QTableWidgetItem(seq_id))
            self.my_queue_table.setItem(i, 4, QTableWidgetItem(obs))
            self.my_queue_table.setItem(i, 5, QTableWidgetItem(scene['owner_username'] or '—'))
            self.my_queue_table.setItem(i, 6, make_flag_item(scene['flags']))
            self.my_queue_table.setItem(i, 7, QTableWidgetItem(str(scene['updated_at'] or '—')))
        self._fill_table(
            self.my_queue_table, get_supervisor_my_queue(self.conn, sup_id), fill_my_queue
        )

        def fill_pool(i, scene):
            rover, sol, seq_id, obs = parse_scene_key(scene['scene_key'])
            self.pool_table.setItem(i, 0, QTableWidgetItem(str(scene['id'])))
            self.pool_table.setItem(i, 1, QTableWidgetItem(rover))
            self.pool_table.setItem(i, 2, QTableWidgetItem(sol))
            self.pool_table.setItem(i, 3, QTableWidgetItem(seq_id))
            self.pool_table.setItem(i, 4, QTableWidgetItem(SceneStatus.LABELS[scene['status']]))
            self.pool_table.setItem(i, 5, QTableWidgetItem(obs))
            self.pool_table.setItem(i, 6, QTableWidgetItem(scene['owner_username'] or '—'))
            self.pool_table.setItem(i, 7, make_flag_item(scene['flags']))
            self.pool_table.setItem(i, 8, QTableWidgetItem(str(scene['updated_at'] or '—')))
        self._fill_table(self.pool_table, get_supervisor_queue(self.conn), fill_pool)

        def fill_in_progress(i, scene):
            rover, sol, seq_id, obs = parse_scene_key(scene['scene_key'])
            self.in_progress_table.setItem(i, 0, QTableWidgetItem(str(scene['id'])))
            self.in_progress_table.setItem(i, 1, QTableWidgetItem(rover))
            self.in_progress_table.setItem(i, 2, QTableWidgetItem(sol))
            self.in_progress_table.setItem(i, 3, QTableWidgetItem(seq_id))
            self.in_progress_table.setItem(i, 4, QTableWidgetItem(obs))
            self.in_progress_table.setItem(i, 5, QTableWidgetItem(SceneStatus.LABELS[scene['status']]))
            self.in_progress_table.setItem(i, 6, QTableWidgetItem(scene['owner_username'] or '—'))
            self.in_progress_table.setItem(i, 7, make_flag_item(scene['flags']))
            self.in_progress_table.setItem(i, 8, QTableWidgetItem(str(scene['updated_at'] or '—')))
        self._fill_table(
            self.in_progress_table,
            get_supervisor_in_progress(self.conn, sup_id),
            fill_in_progress,
        )

        def fill_master(i, scene):
            rover, sol, seq_id, obs = parse_scene_key(scene['scene_key'])
            parsed = {'rover': rover, 'sol': sol, 'seqid': seq_id, 'obs': obs}
            for col, (header, field) in enumerate(_MASTER_COLS):
                if field is None:
                    item = QTableWidgetItem(parsed.get(header.lower(), ''))
                elif field == 'status':
                    item = QTableWidgetItem(
                        SceneStatus.LABELS.get(scene[field], str(scene[field]))
                    )
                elif field == 'flags':
                    item = make_flag_item(scene['flags'])
                else:
                    item = QTableWidgetItem(str(scene[field] or '—'))
                self.master_table.setItem(i, col, item)
        self._fill_table(self.master_table, get_all_scenes(self.conn), fill_master)

        self._update_my_queue_tray()
        self._update_pool_tray()
        self._update_in_progress_tray()
        self._update_master_tray()

    # ── Button trays ─────────────────────────────────────────────────────

    def _update_my_queue_tray(self):
        clear_tray(self.my_queue_tray)
        if self.selected_id(self.my_queue_table) is None:
            return
        layout = self.my_queue_tray.layout()
        assert layout is not None
        for label, handler in [
            ("Approve",            "handle_approve"),
            ("Kick Back",          "handle_kick_back"),
            ("Mark Bad Scene",     "handle_mark_bad_scene"),
            ("Release",            "handle_release"),
            ("Open in ROI Studio", "handle_my_queue_open_roi"),
            ("See Notes",          "handle_my_queue_see_notes"),
            ("Science Notes",      "handle_my_queue_see_science_notes"),
            ("Flag Scene",         "handle_my_queue_flag"),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(getattr(self, handler))
            layout.addWidget(btn)

    def _update_pool_tray(self):
        clear_tray(self.pool_tray)
        if self.selected_id(self.pool_table) is None:
            return
        layout = self.pool_tray.layout()
        assert layout is not None
        for label, handler in [
            ("Claim",              "handle_claim"),
            ("Open in ROI Studio", "handle_pool_open_roi"),
            ("See Notes",          "handle_pool_see_notes"),
            ("Science Notes",      "handle_pool_see_science_notes"),
            ("Flag Scene",         "handle_pool_flag"),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(getattr(self, handler))
            layout.addWidget(btn)

    def _update_in_progress_tray(self):
        clear_tray(self.in_progress_tray)
        if self.selected_id(self.in_progress_table) is None:
            return
        layout = self.in_progress_tray.layout()
        assert layout is not None
        for label, handler in [
            ("Open in ROI Studio", "handle_in_progress_open_roi"),
            ("See Notes",          "handle_in_progress_see_notes"),
            ("Science Notes",      "handle_in_progress_see_science_notes"),
            ("Flag Scene",         "handle_in_progress_flag"),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(getattr(self, handler))
            layout.addWidget(btn)

    def _update_master_tray(self):
        clear_tray(self.master_tray)
        if self.selected_id(self.master_table) is None:
            return
        layout = self.master_tray.layout()
        assert layout is not None
        for label, handler in [
            ("Open in ROI Studio", "handle_master_open_roi"),
            ("See Notes",          "handle_master_see_notes"),
            ("Science Notes",      "handle_master_see_science_notes"),
            ("Flag Scene",         "handle_master_flag"),
            ("Edit Scene",         "handle_edit_scene"),
            ("Reset Scene",        "handle_reset_scene"),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(getattr(self, handler))
            layout.addWidget(btn)

    # ── Shared helper ─────────────────────────────────────────────────────

    def _scene_name_from(self, table):
        row = table.currentRow()
        cells = [table.item(row, c) for c in (1, 2, 3)]
        return " ".join(c.text() if c else '' for c in cells)

    # ── My Work Queue handlers ────────────────────────────────────────────

    def _my_queue_scene_id(self):
        scene_id = self.selected_id(self.my_queue_table)
        if scene_id is None:
            QMessageBox.warning(self, "No Selection", "Select a scene first.")
        return scene_id

    def handle_approve(self):
        scene_id = self._my_queue_scene_id()
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
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        scene_name = self._scene_name_from(self.my_queue_table)
        dialog = KickBackDialog(self.conn, scene_id, scene_name, self)
        if dialog.exec() != KickBackDialog.DialogCode.Accepted:
            return
        comments = dialog.get_comments()
        try:
            supervisor_review_scene(
                self.conn, scene_id, self.user['id'], Decision.REQUEST_REVISION, comments
            )
        except ValueError as e:
            QMessageBox.warning(self, "Kick Back Failed", str(e))
            self.refresh_task_list()
            return
        QMessageBox.information(self, "Kicked Back", "Scene returned to analyst with notes.")
        self.refresh_task_list()

    def handle_mark_bad_scene(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        scene_name = self._scene_name_from(self.my_queue_table)
        confirm = QMessageBox.question(
            self, "Mark Bad Scene",
            f"Mark '{scene_name}' as having issues?\n\nThis takes it out of the normal review workflow.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            mark_scene_issues(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Mark Bad Scene Failed", str(e))
            self.refresh_task_list()
            return
        QMessageBox.information(self, "Marked", "Scene marked as having issues.")
        self.refresh_task_list()

    def handle_release(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        try:
            release_supervisor_review(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Release Failed", str(e))
            self.refresh_task_list()
            return
        QMessageBox.information(self, "Released", "Scene returned to supervisor pool.")
        self.refresh_task_list()

    def handle_my_queue_open_roi(self):
        scene_id = self.selected_id(self.my_queue_table)
        if scene_id is not None:
            super().handle_open_roi(scene_id)

    def handle_my_queue_see_notes(self):
        scene_id = self.selected_id(self.my_queue_table)
        if scene_id is not None:
            self._show_notes(scene_id, self._scene_name_from(self.my_queue_table))

    def handle_my_queue_see_science_notes(self):
        scene_id = self.selected_id(self.my_queue_table)
        if scene_id is not None:
            self._show_science_notes(scene_id, self._scene_name_from(self.my_queue_table))

    def handle_my_queue_flag(self):
        scene_id = self.selected_id(self.my_queue_table)
        if scene_id is not None:
            self.handle_flag_scene(scene_id, self._scene_name_from(self.my_queue_table))

    # ── Supervisor Pool handlers ──────────────────────────────────────────

    def handle_claim(self):
        scene_id = self.selected_id(self.pool_table)
        if scene_id is None:
            return
        try:
            success = claim_for_supervisor_review(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Claim Failed", str(e))
            self.refresh_task_list()
            return
        if success:
            QMessageBox.information(self, "Claimed", "Scene added to your work queue.")
        else:
            QMessageBox.warning(self, "Claim Failed", "Scene is no longer available.")
        self.refresh_task_list()

    def handle_pool_open_roi(self):
        scene_id = self.selected_id(self.pool_table)
        if scene_id is not None:
            super().handle_open_roi(scene_id)

    def handle_pool_see_notes(self):
        scene_id = self.selected_id(self.pool_table)
        if scene_id is not None:
            self._show_notes(scene_id, self._scene_name_from(self.pool_table))

    def handle_pool_see_science_notes(self):
        scene_id = self.selected_id(self.pool_table)
        if scene_id is not None:
            self._show_science_notes(scene_id, self._scene_name_from(self.pool_table))

    def handle_pool_flag(self):
        scene_id = self.selected_id(self.pool_table)
        if scene_id is not None:
            self.handle_flag_scene(scene_id, self._scene_name_from(self.pool_table))

    # ── In Progress handlers ──────────────────────────────────────────────

    def handle_in_progress_open_roi(self):
        scene_id = self.selected_id(self.in_progress_table)
        if scene_id is not None:
            super().handle_open_roi(scene_id)

    def handle_in_progress_see_notes(self):
        scene_id = self.selected_id(self.in_progress_table)
        if scene_id is not None:
            self._show_notes(scene_id, self._scene_name_from(self.in_progress_table))

    def handle_in_progress_see_science_notes(self):
        scene_id = self.selected_id(self.in_progress_table)
        if scene_id is not None:
            self._show_science_notes(scene_id, self._scene_name_from(self.in_progress_table))

    def handle_in_progress_flag(self):
        scene_id = self.selected_id(self.in_progress_table)
        if scene_id is not None:
            self.handle_flag_scene(scene_id, self._scene_name_from(self.in_progress_table))

    # ── Master list handlers ──────────────────────────────────────────────

    def handle_master_open_roi(self):
        scene_id = self.selected_id(self.master_table)
        if scene_id is not None:
            super().handle_open_roi(scene_id)

    def handle_master_see_notes(self):
        scene_id = self.selected_id(self.master_table)
        if scene_id is not None:
            self._show_notes(scene_id, self._scene_name_from(self.master_table))

    def handle_master_see_science_notes(self):
        scene_id = self.selected_id(self.master_table)
        if scene_id is not None:
            self._show_science_notes(scene_id, self._scene_name_from(self.master_table))

    def handle_master_flag(self):
        scene_id = self.selected_id(self.master_table)
        if scene_id is not None:
            self.handle_flag_scene(scene_id, self._scene_name_from(self.master_table))

    def handle_edit_scene(self):
        scene_id = self.selected_id(self.master_table)
        if scene_id is None:
            return
        scene = get_scene_by_id(self.conn, scene_id)
        if scene is None:
            return
        dialog = EditSceneDialog(self.conn, scene, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            supervisor_edit_scene(
                self.conn, scene_id, self.user['id'], dialog.get_status(),
                owner_id=dialog.get_owner_id(),
                peer_reviewer_id=dialog.get_peer_reviewer_id(),
                scene_supervisor_id=dialog.get_supervisor_id(),
                claimed_by=dialog.get_claimed_by(),
                comments=dialog.get_notes(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Edit Scene Failed", str(e))
        self.refresh_task_list()

    def handle_reset_scene(self):
        scene_id = self.selected_id(self.master_table)
        if scene_id is None:
            return
        scene_name = self._scene_name_from(self.master_table)
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
