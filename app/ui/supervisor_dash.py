"""supervisor dashboard — pool, personal queue, in-progress, and master list"""
import os
import shutil
from PyQt6.QtWidgets import (
    QSplitter, QMessageBox, QTableWidgetItem,
    QDialog, QVBoxLayout, QLabel, QComboBox, QDialogButtonBox,
)
from PyQt6.QtCore import Qt
from app.ui.dashboard import (
    Dashboard, WordSelectTextEdit, SizePersistentDialog, SCENE_BUTTONS,
    PersistentSplitter,
    make_scene_table, make_section,
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


# Master list columns: header label -> field name (None = parsed from scene_key)
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
        my_queue_section = make_section("My Work Queue", self.my_queue_table)

        # Supervisor Pool — shared, unclaimed (status 5)
        self.pool_table = make_scene_table(
            ["ID", "Rover", "Sol", "SeqID", "Status", "Obs", "Owner", "Flags", "Updated"]
        )
        apply_flag_delegate(self.pool_table)
        pool_section = make_section("Supervisor Pool", self.pool_table)

        # In Progress — scenes I kicked back, still being revised (status 4)
        self.in_progress_table = make_scene_table(
            ["ID", "Rover", "Sol", "SeqID", "Obs", "Status", "Owner", "Flags", "Updated"]
        )
        apply_flag_delegate(self.in_progress_table)
        in_progress_section = make_section("In Progress", self.in_progress_table)

        # Master scene list
        self.master_table = make_scene_table([h for h, _ in _MASTER_COLS])
        apply_flag_delegate(self.master_table)
        self.master_section = make_section(
            "All Scenes", self.master_table, count_fn=_master_status_counts
        )
        master_section = self.master_section

        left_splitter = PersistentSplitter(Qt.Orientation.Vertical, 'supervisor.left')
        left_splitter.addWidget(my_queue_section)
        left_splitter.addWidget(in_progress_section)

        # Single pane, so no handle to drag and nothing to persist.
        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.addWidget(pool_section)

        # Top row: the queues a supervisor works out of. Bottom row: the master
        # list. The tray goes between them, where the seam already was.
        top_row = PersistentSplitter(Qt.Orientation.Horizontal, 'supervisor.top')
        top_row.addWidget(left_splitter)
        top_row.addWidget(right_splitter)

        tray_bar = self.make_tray_bar([
            # 'summary_slide' leads: it is the fastest way to see a scene's
            # ROIs, spectra and metadata, and is meant to be reached before
            # anyone waits on ROI Studio to start.
            (self.my_queue_table, "My Work Queue",
            [("Approve",        "handle_approve"),
            ("Mark Bad Scene", "handle_mark_bad_scene"),
            ("Release",        "handle_release"),
              'summary_slide', *SCENE_BUTTONS]),
            (self.pool_table, "Supervisor Pool",
             [("Claim", "handle_claim"), 'summary_slide', *SCENE_BUTTONS]),
            (self.in_progress_table, "In Progress", SCENE_BUTTONS),
            (self.master_table, "All Scenes",
            [*SCENE_BUTTONS,
            ("Edit Scene",  "handle_edit_scene"),
            ("Reset Scene", "handle_reset_scene")]),
        ])

        # stretch=1: all surplus window height goes to the tables, none of it to
        # the pinned tray band in the middle.
        # weights: the master list keeps the larger share it had before the tray
        # moved between the rows.
        self.main_content_layout.addWidget(
            self.make_rows_splitter('supervisor.rows', top_row, tray_bar, master_section,
                                    weights=(1, 2)),
            stretch=1,
        )

        self.refresh_task_list()

    # ── Populate tables ─────────────────────────────────────────────────

    def _refresh_tables(self):
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

        self.update_shared_tray()

    # ── My Work Queue handlers ────────────────────────────────────────────

    def _my_queue_scene_id(self):
        scene_id = self.selected_id(self.my_queue_table)
        if scene_id is None:
            QMessageBox.warning(self, "No Selection", "Select a scene first.")
        return scene_id

    def _review_callbacks(self, table, scene_id):
        """A supervisor can act on a scene straight from the notes dialog.
        Only from their own queue, which by definition holds nothing but the
        scenes they have claimed for review."""
        if table is self.my_queue_table:
            return {
                'on_approve':   lambda comment: self._do_approve(scene_id, comment),
                'on_kick_back': lambda comment: self._do_kick_back(scene_id, comment),
            }
        return {}

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
        than rolled back or allowed to block the workflow.

        Returns a description of the problem, or None if the copy succeeded. The
        caller decides how to show it — one dialog for a single approval, or one
        combined dialog for a batch, rather than a dialog per scene."""
        scene = get_scene_by_id(self.conn, scene_id)
        if scene is None:
            return None
        fits_path = find_fits_file(PANCAM_PATH, scene)
        if not fits_path:
            return (f"'{scene['name']}' was approved, but no .fits file could be "
                    "found to copy to ready_for_asdf.")
        try:
            dest_dir = os.path.join(PANCAM_PATH, "ready_for_asdf")
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(fits_path, os.path.join(dest_dir, os.path.basename(fits_path)))
        except OSError as e:
            return (f"'{scene['name']}' was approved, but its .fits file could not "
                    f"be copied to ready_for_asdf:\n{e}")
        return None

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
        """Approve every selected scene. The .fits copy runs per scene but its
        failures are pooled into one dialog afterwards — approving a batch
        should not mean dismissing a warning for each file that's missing."""
        fits_problems = []

        def _approve(scene_id):
            supervisor_review_scene(self.conn, scene_id, self.user['id'], Decision.APPROVE, None)
            problem = self._copy_fits_to_ready_for_asdf(scene_id)
            if problem:
                fits_problems.append(problem)

        self.run_bulk_action(
            self.my_queue_table, _approve, "Approve",
            done_msg="{done} scene(s) approved.",
            none_msg="None of the selected scenes could be approved.",
            partial_msg="{done} scene(s) approved; {skipped} were no longer eligible.",
            confirm_msg="Approve {n} scenes?\n\nThis cannot be undone.",
        )
        if fits_problems:
            QMessageBox.warning(self, "FITS Not Copied", "\n\n".join(fits_problems))

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
        self.run_bulk_action(
            self.my_queue_table,
            lambda sid: release_supervisor_review(self.conn, sid, self.user['id']),
            "Release",
            done_msg="{done} scene(s) returned to the supervisor pool.",
            none_msg="None of the selected scenes could be released.",
            partial_msg="{done} scene(s) released; {skipped} were no longer eligible.",
        )

    # ── Supervisor Pool handlers ──────────────────────────────────────────

    def handle_claim(self):
        self.run_bulk_action(
            self.pool_table,
            lambda sid: claim_for_supervisor_review(self.conn, sid, self.user['id']),
            "Claim",
            done_msg="{done} scene(s) claimed and added to your work queue.",
            none_msg="None of the selected scenes are still available.",
            partial_msg="{done} scene(s) claimed; {skipped} were no longer available.",
        )

    # ── Master list handlers ──────────────────────────────────────────────

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
