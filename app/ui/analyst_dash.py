"""analyst dashboard — work queue, peer review pool, and scene pool"""
from PyQt6.QtWidgets import QPushButton, QSplitter, QTabWidget, QMessageBox, QTableWidgetItem
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from app.ui.dashboard import (
    Dashboard, SCENE_BUTTONS,
    make_scene_table, make_section,
    parse_scene_key, apply_flag_delegate, make_flag_item,
)
from app.local_settings import get_all_scene_viewed_times, get_dark_mode
from app.ui.styles import NEW_ACTIVITY_LIGHT, NEW_ACTIVITY_DARK
from app.db import (
    get_analyst_queue, get_ready_queue, get_scene_pool, get_analyst_in_progress,
    get_analyst_completed, get_all_scenes,
)
from app.controller import (
    claim_from_pool, claim_scene_for_review, submit_scene,
    release_scene_to_pool, peer_review_scene
)
from app.models import SceneStatus, Decision

# Context-sensitive buttons for My Queue by status. Bare strings are generic
# SCENE_ACTIONS keys, bound to the table by build_tray(); tuples are My Queue's
# own (label, handler name) pairs.
_MY_QUEUE_BUTTONS = {
    SceneStatus.CLAIMED: [
        'open_roi', 'open_notebook',
        ("Submit", "handle_submit"),
        'notes', 'science_notes', 'flag',
        ("Release", "handle_release"),
    ],
    SceneStatus.IN_REVIEW: [
        'open_roi', 'open_notebook',
        ("Approve",   "handle_approve"),
        ("Kick Back", "handle_kick_back"),
        'notes', 'science_notes', 'flag',
        ("Release", "handle_release"),
    ],
    SceneStatus.NEEDS_REVISION: [
        'open_roi', 'open_notebook',
        ("Submit", "handle_submit"),
        'notes', 'science_notes', 'flag',
    ],
}


class AnalystDashboard(Dashboard):

    def __init__(self, conn, user):
        super().__init__(conn, user)
        self._build_main_content()

    # ── Build UI ────────────────────────────────────────────────────────

    def _build_main_content(self):
        # My Work Queue — Status at col 4 so selected_status() still works
        self.my_queue_table = make_scene_table(
            ["ID", "Rover", "Sol", "SeqID", "Status", "Obs", "Analyst 1", "Flags", "Name", "Updated"]
        )
        apply_flag_delegate(self.my_queue_table)
        my_section = make_section("My Work Queue", self.my_queue_table)

        # In Progress — scenes I've contributed to that are still moving
        self.in_progress_table = make_scene_table(
            ["ID", "Rover", "Sol", "SeqID", "Obs", "My Role", "Status", "Current Holder", "Name", "Updated"]
        )
        in_progress_section = make_section("In Progress", self.in_progress_table)

        # Ready for Peer Review
        self.review_queue_table = make_scene_table(["ID", "Rover", "Sol", "SeqID", "Obs", "Flags", "Name", "Owner", "Updated"])
        apply_flag_delegate(self.review_queue_table)
        review_section = make_section("Ready for Peer Review", self.review_queue_table)

        # Unclaimed Scenes
        self.scene_pool_table = make_scene_table(["ID", "Rover", "Sol", "SeqID", "Obs", "Flags", "Name", "Updated"])
        apply_flag_delegate(self.scene_pool_table)
        pool_section = make_section("Unclaimed Scenes", self.scene_pool_table)

        # My Completed Scenes
        self.completed_table = make_scene_table(["ID", "Rover", "Sol", "SeqID", "Obs", "My Role", "Name", "Updated"])
        completed_section = make_section("My Completed Scenes", self.completed_table)

        # All Scenes — full master list, so analysts can add notes to scenes they don't own
        self.all_scenes_table = make_scene_table(
            ["ID", "Rover", "Sol", "SeqID", "Status", "Obs", "Owner", "Flags", "Name", "Updated"]
        )
        apply_flag_delegate(self.all_scenes_table)
        all_scenes_section = make_section("All Scenes", self.all_scenes_table)

        pool_tabs = QTabWidget()
        pool_tabs.addTab(pool_section, "Unclaimed Scenes")
        pool_tabs.addTab(completed_section, "My Completed Scenes")
        pool_tabs.addTab(all_scenes_section, "All Scenes")

        left_splitter = QSplitter(Qt.Orientation.Vertical)
        left_splitter.addWidget(my_section)
        left_splitter.addWidget(in_progress_section)
        left_splitter.setStretchFactor(0, 1)
        left_splitter.setStretchFactor(1, 1)

        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.addWidget(review_section)
        right_splitter.addWidget(pool_tabs)
        right_splitter.setStretchFactor(0, 1)
        right_splitter.setStretchFactor(1, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_splitter)
        splitter.addWidget(right_splitter)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_task_list)

        tray_bar = self.make_tray_bar([
            (self.my_queue_table,     "My Work Queue",         self._my_queue_actions),
            (self.in_progress_table,  "In Progress",           SCENE_BUTTONS),
            (self.review_queue_table, "Ready for Peer Review",
             [("Claim for Review", "handle_claim_for_review"), 'flag']),
            (self.scene_pool_table,   "Unclaimed Scenes",
             [("Claim Scene", "handle_claim_from_pool"), 'flag']),
            (self.completed_table,    "My Completed Scenes",
             ['open_roi', 'open_notebook', 'notes', 'science_notes']),
            (self.all_scenes_table,   "All Scenes",            SCENE_BUTTONS),
        ])

        self.main_content_layout.addWidget(splitter)
        self.main_content_layout.addWidget(tray_bar)
        self.main_content_layout.addWidget(refresh_button)

        self.refresh_task_list()

    # ── Populate tables ─────────────────────────────────────────────────

    def refresh_task_list(self):
        analyst_id = self.user['id']

        def fill_my_queue(i, scene):
            rover, sol, seq_id, obs = parse_scene_key(scene['scene_key'])
            self.my_queue_table.setItem(i, 0, QTableWidgetItem(str(scene['id'])))
            self.my_queue_table.setItem(i, 1, QTableWidgetItem(rover))
            self.my_queue_table.setItem(i, 2, QTableWidgetItem(sol))
            self.my_queue_table.setItem(i, 3, QTableWidgetItem(seq_id))
            self.my_queue_table.setItem(i, 4, QTableWidgetItem(SceneStatus.LABELS[scene['status']]))
            self.my_queue_table.setItem(i, 5, QTableWidgetItem(obs))
            self.my_queue_table.setItem(i, 6, QTableWidgetItem(scene['owner_username'] or '—'))
            self.my_queue_table.setItem(i, 7, make_flag_item(scene['flags']))
            self.my_queue_table.setItem(i, 8, QTableWidgetItem(scene['name']))
            self.my_queue_table.setItem(i, 9, QTableWidgetItem(str(scene['updated_at'] or '—')))
        self._fill_table(self.my_queue_table, get_analyst_queue(self.conn, analyst_id), fill_my_queue)

        scene_viewed = get_all_scene_viewed_times()
        _NEW = QColor(NEW_ACTIVITY_DARK if get_dark_mode() else NEW_ACTIVITY_LIGHT)

        def fill_in_progress(i, scene):
            rover, sol, seq_id, obs = parse_scene_key(scene['scene_key'])
            self.in_progress_table.setItem(i, 0, QTableWidgetItem(str(scene['id'])))
            self.in_progress_table.setItem(i, 1, QTableWidgetItem(rover))
            self.in_progress_table.setItem(i, 2, QTableWidgetItem(sol))
            self.in_progress_table.setItem(i, 3, QTableWidgetItem(seq_id))
            self.in_progress_table.setItem(i, 4, QTableWidgetItem(obs))
            self.in_progress_table.setItem(i, 5, QTableWidgetItem(scene['my_role']))
            self.in_progress_table.setItem(i, 6, QTableWidgetItem(SceneStatus.LABELS[scene['status']]))
            self.in_progress_table.setItem(i, 7, QTableWidgetItem(scene['current_holder'] or '—'))
            self.in_progress_table.setItem(i, 8, QTableWidgetItem(scene['name']))
            self.in_progress_table.setItem(i, 9, QTableWidgetItem(str(scene['updated_at'] or '—')))
            viewed_at = scene_viewed.get(str(scene['id']), '')
            if (scene['updated_at'] or '') > viewed_at:
                for col in range(1, self.in_progress_table.columnCount()):
                    item = self.in_progress_table.item(i, col)
                    if item:
                        item.setBackground(_NEW)
        self._fill_table(self.in_progress_table, get_analyst_in_progress(self.conn, analyst_id), fill_in_progress)

        def fill_review(i, scene):
            rover, sol, seq_id, obs = parse_scene_key(scene['scene_key'])
            self.review_queue_table.setItem(i, 0, QTableWidgetItem(str(scene['id'])))
            self.review_queue_table.setItem(i, 1, QTableWidgetItem(rover))
            self.review_queue_table.setItem(i, 2, QTableWidgetItem(sol))
            self.review_queue_table.setItem(i, 3, QTableWidgetItem(seq_id))
            self.review_queue_table.setItem(i, 4, QTableWidgetItem(obs))
            self.review_queue_table.setItem(i, 5, make_flag_item(scene['flags']))
            self.review_queue_table.setItem(i, 6, QTableWidgetItem(scene['name']))
            self.review_queue_table.setItem(i, 7, QTableWidgetItem(scene['owner_username'] or '—'))
            self.review_queue_table.setItem(i, 8, QTableWidgetItem(str(scene['updated_at'] or '—')))
        ready_scenes = [s for s in get_ready_queue(self.conn) if s['owner_id'] != analyst_id]
        self._fill_table(self.review_queue_table, ready_scenes, fill_review)

        def fill_pool(i, scene):
            rover, sol, seq_id, obs = parse_scene_key(scene['scene_key'])
            self.scene_pool_table.setItem(i, 0, QTableWidgetItem(str(scene['id'])))
            self.scene_pool_table.setItem(i, 1, QTableWidgetItem(rover))
            self.scene_pool_table.setItem(i, 2, QTableWidgetItem(sol))
            self.scene_pool_table.setItem(i, 3, QTableWidgetItem(seq_id))
            self.scene_pool_table.setItem(i, 4, QTableWidgetItem(obs))
            self.scene_pool_table.setItem(i, 5, make_flag_item(scene['flags']))
            self.scene_pool_table.setItem(i, 6, QTableWidgetItem(scene['name']))
            self.scene_pool_table.setItem(i, 7, QTableWidgetItem(str(scene['updated_at'] or '—')))
        self._fill_table(self.scene_pool_table, get_scene_pool(self.conn), fill_pool)

        def fill_completed(i, scene):
            rover, sol, seq_id, obs = parse_scene_key(scene['scene_key'])
            self.completed_table.setItem(i, 0, QTableWidgetItem(str(scene['id'])))
            self.completed_table.setItem(i, 1, QTableWidgetItem(rover))
            self.completed_table.setItem(i, 2, QTableWidgetItem(sol))
            self.completed_table.setItem(i, 3, QTableWidgetItem(seq_id))
            self.completed_table.setItem(i, 4, QTableWidgetItem(obs))
            self.completed_table.setItem(i, 5, QTableWidgetItem(scene['my_role']))
            self.completed_table.setItem(i, 6, QTableWidgetItem(scene['name']))
            self.completed_table.setItem(i, 7, QTableWidgetItem(str(scene['updated_at'] or '—')))
        self._fill_table(self.completed_table, get_analyst_completed(self.conn, analyst_id), fill_completed)

        def fill_all_scenes(i, scene):
            rover, sol, seq_id, obs = parse_scene_key(scene['scene_key'])
            self.all_scenes_table.setItem(i, 0, QTableWidgetItem(str(scene['id'])))
            self.all_scenes_table.setItem(i, 1, QTableWidgetItem(rover))
            self.all_scenes_table.setItem(i, 2, QTableWidgetItem(sol))
            self.all_scenes_table.setItem(i, 3, QTableWidgetItem(seq_id))
            self.all_scenes_table.setItem(i, 4, QTableWidgetItem(SceneStatus.LABELS.get(scene['status'], str(scene['status']))))
            self.all_scenes_table.setItem(i, 5, QTableWidgetItem(obs))
            self.all_scenes_table.setItem(i, 6, QTableWidgetItem(scene['owner_username'] or '—'))
            self.all_scenes_table.setItem(i, 7, make_flag_item(scene['flags']))
            self.all_scenes_table.setItem(i, 8, QTableWidgetItem(scene['name']))
            self.all_scenes_table.setItem(i, 9, QTableWidgetItem(str(scene['updated_at'] or '—')))
        self._fill_table(self.all_scenes_table, get_all_scenes(self.conn), fill_all_scenes)

        self.update_shared_tray()

    # ── Button tray ─────────────────────────────────────────────────────

    def _my_queue_actions(self):
        """My Work Queue mixes statuses 1/3/4, so its buttons depend on which
        row is selected rather than on the table alone."""
        return _MY_QUEUE_BUTTONS.get(self.selected_status(self.my_queue_table), [])

    # ── My Queue handlers ───────────────────────────────────────────────

    def _review_callbacks(self, table, scene_id):
        """A peer reviewer holding a scene can act on it straight from the
        notes dialog. Only applies to their own queue, and only while the
        scene is actually in review."""
        if table is self.my_queue_table and self.selected_status(table) == SceneStatus.IN_REVIEW:
            return {
                'on_approve':   lambda comment: self._do_approve(scene_id, comment),
                'on_kick_back': lambda comment: self._do_kick_back(scene_id, comment),
            }
        return {}

    def _my_queue_scene_id(self):
        scene_id = self.selected_id(self.my_queue_table)
        if scene_id is None:
            QMessageBox.warning(self, "No Selection", "Select a scene first.")
        return scene_id

    def handle_submit(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        ok = self._run_db_action(
            lambda: submit_scene(self.conn, scene_id, self.user['id']), "Submit Failed"
        )
        self.refresh_task_list()
        if ok:
            QMessageBox.information(self, "Submitted", "Scene submitted.")

    def _do_approve(self, scene_id, comment=None):
        """Peer reviewer approves a status-3 scene → status 5."""
        ok = self._run_db_action(
            lambda: peer_review_scene(self.conn, scene_id, self.user['id'], Decision.APPROVE, comment),
            "Approve Failed"
        )
        self.refresh_task_list()
        return ok

    def _do_kick_back(self, scene_id, comment=None):
        """Peer reviewer kicks back a status-3 scene → status 4 with an optional comment."""
        ok = self._run_db_action(
            lambda: peer_review_scene(self.conn, scene_id, self.user['id'], Decision.REQUEST_REVISION, comment),
            "Kick Back Failed"
        )
        self.refresh_task_list()
        return ok

    def handle_approve(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        if self._do_approve(scene_id):
            QMessageBox.information(self, "Approved", "Scene approved and sent to supervisor.")

    def handle_kick_back(self):
        """Kick Back opens the notes dialog rather than acting immediately, so
        the reviewer can leave a comment first. _review_callbacks() supplies
        its Approve/Kick Back buttons."""
        if self._my_queue_scene_id() is None:
            return
        self.act_notes(self.my_queue_table)

    def handle_release(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        ok = self._run_db_action(
            lambda: release_scene_to_pool(self.conn, scene_id, self.user['id']), "Release Failed"
        )
        self.refresh_task_list()
        if ok:
            QMessageBox.information(self, "Released", "Scene released back to pool.")

    # ── Pool / review queue handlers ────────────────────────────────────

    def handle_claim_from_pool(self):
        scene_ids = self.selected_ids(self.scene_pool_table)
        if not scene_ids:
            return
        claimed, skipped = 0, 0
        def _claim_all():
            nonlocal claimed, skipped
            for scene_id in scene_ids:
                try:
                    if claim_from_pool(self.conn, scene_id, self.user['id']):
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

    def handle_claim_for_review(self):
        scene_ids = self.selected_ids(self.review_queue_table)
        if not scene_ids:
            return
        claimed, skipped = 0, 0
        def _claim_all():
            nonlocal claimed, skipped
            for scene_id in scene_ids:
                try:
                    if claim_scene_for_review(self.conn, scene_id, self.user['id']):
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
            QMessageBox.information(self, "Claimed", f"{claimed} scene(s) claimed for peer review.")
        elif claimed == 0:
            QMessageBox.warning(self, "Claim Failed", "None of the selected scenes are still available.")
        else:
            QMessageBox.information(self, "Partially Claimed", f"{claimed} scene(s) claimed; {skipped} were no longer available.")

