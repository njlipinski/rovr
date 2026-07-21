"""supervisor dashboard — pool, personal queue, in-progress, and master list"""
import os
import shutil
from PyQt6.QtWidgets import (
    QPushButton, QSplitter, QMessageBox, QTableWidgetItem,
    QDialog, QVBoxLayout, QLabel, QComboBox, QDialogButtonBox,
)
from PyQt6.QtCore import Qt
from app.ui.dashboard import (
    Dashboard, WordSelectTextEdit, SizePersistentDialog,
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
from app.paths import find_fits_file
from config import PANCAM_PATH


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


class EditSceneDialog(SizePersistentDialog):
    """Supervisor admin dialog: edit a scene's status and directly reassign
    its owner, peer reviewer, supervisor, and claimed-by fields."""
    _size_key = 'edit_scene'

    def __init__(self, conn, scene, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Scene")
        self.setMinimumWidth(360)
        self._restore_size()
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

_MASTER_STATUS_COL = next(i for i, (h, _) in enumerate(_MASTER_COLS) if h == "Status")


def _master_status_counts(table):
    """Per-status breakdown of the master table's current rows, read straight
    from the Status column so it always matches what's actually displayed."""
    counts = {}
    for row in range(table.rowCount()):
        item = table.item(row, _MASTER_STATUS_COL)
        label = item.text() if item else ''
        counts[label] = counts.get(label, 0) + 1
    parts = []
    for status in sorted(SceneStatus.LABELS):
        label = SceneStatus.LABELS[status]
        parts.append(f"{counts.get(label, 0)} {label.title()}")
    return ", ".join(parts)


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
        self.master_section = make_section(
            "All Scenes", self.master_table, self.master_tray, count_fn=_master_status_counts
        )
        master_section = self.master_section

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
        self.master_section.refresh_count()

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
            ("Open in Notebook",   "handle_my_queue_open_notebook"),
            ("See Notes",          "handle_my_queue_see_notes"),
            ("Science Notes",      "handle_my_queue_see_science_notes"),
            ("Flag Scene",         "handle_my_queue_flag"),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(getattr(self, handler))
            layout.addWidget(btn)

    def _update_pool_tray(self):
        clear_tray(self.pool_tray)
        if not self.selected_ids(self.pool_table):
            return
        layout = self.pool_tray.layout()
        assert layout is not None
        for label, handler in [
            ("Claim",              "handle_claim"),
            ("Open in ROI Studio", "handle_pool_open_roi"),
            ("Open in Notebook",   "handle_pool_open_notebook"),
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
            ("Open in Notebook",   "handle_in_progress_open_notebook"),
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
            ("Open in Notebook",   "handle_master_open_notebook"),
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

    def _do_approve(self, scene_id, comment=None):
        ok = self._run_db_action(
            lambda: supervisor_review_scene(self.conn, scene_id, self.user['id'], Decision.APPROVE, comment),
            "Approve Failed"
        )
        if ok:
            self._copy_fits_to_ready_for_asdf(scene_id)
        self.refresh_task_list()
        return ok

    def _copy_fits_to_ready_for_asdf(self, scene_id):
        """Mirror the just-approved scene's .fits file into PANCAM_PATH/ready_for_asdf.
        The approval itself has already been recorded in the DB by this point, so a
        copy problem (missing file, network hiccup) is surfaced as a warning rather
        than rolled back or allowed to block the workflow."""
        scene = get_scene_by_id(self.conn, scene_id)
        if scene is None:
            return
        fits_path = find_fits_file(PANCAM_PATH, scene)
        if not fits_path:
            QMessageBox.warning(
                self, "FITS Not Copied",
                f"'{scene['name']}' was approved, but no .fits file could be found "
                "to copy to ready_for_asdf."
            )
            return
        try:
            dest_dir = os.path.join(PANCAM_PATH, "ready_for_asdf")
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(fits_path, os.path.join(dest_dir, os.path.basename(fits_path)))
        except OSError as e:
            QMessageBox.warning(
                self, "FITS Not Copied",
                f"'{scene['name']}' was approved, but its .fits file could not be "
                f"copied to ready_for_asdf:\n{e}"
            )

    def _do_kick_back(self, scene_id, comment=None):
        ok = self._run_db_action(
            lambda: supervisor_review_scene(
                self.conn, scene_id, self.user['id'], Decision.REQUEST_REVISION, comment
            ),
            "Kick Back Failed"
        )
        self.refresh_task_list()
        return ok

    def handle_approve(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        if self._do_approve(scene_id):
            QMessageBox.information(self, "Approved", "Scene approved.")

    def handle_kick_back(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        scene_name = self._scene_name_from(self.my_queue_table)
        self._show_notes(
            scene_id, scene_name,
            on_approve=lambda comment: self._do_approve(scene_id, comment),
            on_kick_back=lambda comment: self._do_kick_back(scene_id, comment),
        )

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
        ok = self._run_db_action(
            lambda: mark_scene_issues(self.conn, scene_id, self.user['id']), "Mark Bad Scene Failed"
        )
        self.refresh_task_list()
        if ok:
            QMessageBox.information(self, "Marked", "Scene marked as having issues.")

    def handle_release(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        ok = self._run_db_action(
            lambda: release_supervisor_review(self.conn, scene_id, self.user['id']), "Release Failed"
        )
        self.refresh_task_list()
        if ok:
            QMessageBox.information(self, "Released", "Scene returned to supervisor pool.")

    def handle_my_queue_open_roi(self):
        scene_id = self.selected_id(self.my_queue_table)
        if scene_id is not None:
            super().handle_open_roi(scene_id)

    def handle_my_queue_open_notebook(self):
        scene_id = self.selected_id(self.my_queue_table)
        if scene_id is not None:
            super().handle_open_notebook(scene_id)

    def handle_my_queue_see_notes(self):
        scene_id = self.selected_id(self.my_queue_table)
        if scene_id is not None:
            self._show_notes(
                scene_id, self._scene_name_from(self.my_queue_table),
                on_approve=lambda comment: self._do_approve(scene_id, comment),
                on_kick_back=lambda comment: self._do_kick_back(scene_id, comment),
            )

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
        scene_ids = self.selected_ids(self.pool_table)
        if not scene_ids:
            return
        claimed, skipped = 0, 0
        def _claim_all():
            nonlocal claimed, skipped
            for scene_id in scene_ids:
                try:
                    if claim_for_supervisor_review(self.conn, scene_id, self.user['id']):
                        claimed += 1
                    else:
                        skipped += 1
                except ValueError:
                    skipped += 1
        if not self._run_db_action(_claim_all, "Claim Failed"):
            self.refresh_task_list()
            return
        self.refresh_task_list()
        if skipped == 0:
            QMessageBox.information(self, "Claimed", f"{claimed} scene(s) claimed and added to your work queue.")
        elif claimed == 0:
            QMessageBox.warning(self, "Claim Failed", "None of the selected scenes are still available.")
        else:
            QMessageBox.information(self, "Partially Claimed", f"{claimed} scene(s) claimed; {skipped} were no longer available.")

    def handle_pool_open_roi(self):
        scene_id = self.selected_id(self.pool_table)
        if scene_id is not None:
            super().handle_open_roi(scene_id)

    def handle_pool_open_notebook(self):
        scene_id = self.selected_id(self.pool_table)
        if scene_id is not None:
            super().handle_open_notebook(scene_id)

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

    def handle_in_progress_open_notebook(self):
        scene_id = self.selected_id(self.in_progress_table)
        if scene_id is not None:
            super().handle_open_notebook(scene_id)

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

    def handle_master_open_notebook(self):
        scene_id = self.selected_id(self.master_table)
        if scene_id is not None:
            super().handle_open_notebook(scene_id)

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
        self._run_db_action(
            lambda: supervisor_edit_scene(
                self.conn, scene_id, self.user['id'], dialog.get_status(),
                owner_id=dialog.get_owner_id(),
                peer_reviewer_id=dialog.get_peer_reviewer_id(),
                scene_supervisor_id=dialog.get_supervisor_id(),
                claimed_by=dialog.get_claimed_by(),
                comments=dialog.get_notes(),
            ),
            "Edit Scene Failed"
        )
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
        self._run_db_action(
            lambda: supervisor_reset_scene(self.conn, scene_id, self.user['id']), "Reset Failed"
        )
        self.refresh_task_list()
