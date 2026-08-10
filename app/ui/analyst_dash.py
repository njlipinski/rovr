"""analyst dashboard — work queue, peer review pool, and scene pool"""
from PyQt6.QtWidgets import QTabWidget, QMessageBox, QTableWidgetItem
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from app.ui.dashboard import (
    Dashboard, SCENE_BUTTONS, PersistentSplitter,
    make_scene_table, make_section,
    parse_scene_key, apply_flag_delegate, make_flag_item,
)
from app.local_settings import get_all_scene_viewed_times, get_dark_mode
from app.ui.styles import NEW_ACTIVITY_LIGHT, NEW_ACTIVITY_DARK
from app.db import (
    get_analyst_queue, get_ready_queue, get_scene_pool, get_analyst_in_progress,
    get_analyst_completed, get_all_scenes, get_scene_by_id,
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
        ("Submit", "handle_submit"),
        'notes', 'open_roi', 'open_notebook', 'open_folder',
        'science_notes', 'flag',
        ("Release", "handle_release"),
    ],
    SceneStatus.IN_REVIEW: [
        ("Approve", "handle_approve"),
        'notes', 'open_roi', 'open_notebook', 'open_folder',
        'science_notes', 'flag',
        ("Release", "handle_release"),
    ],
    SceneStatus.NEEDS_REVISION: [
        ("Submit", "handle_submit"),
        'notes', 
        'open_roi', 'open_notebook', 'open_folder',
        'science_notes', 'flag',
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

        # Top row: the analyst's own work. Bottom row: what they can pick up.
        top_row = PersistentSplitter(Qt.Orientation.Horizontal, 'analyst.top')
        top_row.addWidget(my_section)
        top_row.addWidget(in_progress_section)
        top_row.setStretchFactor(0, 1)
        top_row.setStretchFactor(1, 1)

        bottom_row = PersistentSplitter(Qt.Orientation.Horizontal, 'analyst.bottom')
        bottom_row.addWidget(review_section)
        bottom_row.addWidget(pool_tabs)
        bottom_row.setStretchFactor(0, 1)
        bottom_row.setStretchFactor(1, 1)

        tray_bar = self.make_tray_bar([
            (self.my_queue_table,     "My Work Queue",         self._my_queue_actions),
            (self.in_progress_table,  "In Progress",           SCENE_BUTTONS),
            (self.review_queue_table, "Ready for Peer Review",
             [("Claim for Review", "handle_claim_for_review"), 'flag']),
            (self.scene_pool_table,   "Unclaimed Scenes",
             [("Claim Scene", "handle_claim_from_pool"), 'flag']),
            (self.completed_table,    "My Completed Scenes",
             ['open_roi', 'open_notebook', 'open_folder', 'notes', 'science_notes']),
            (self.all_scenes_table,   "All Scenes",            SCENE_BUTTONS),
        ])

        # stretch=1: all surplus window height goes to the tables, none of it to
        # the pinned tray band in the middle.
        self.main_content_layout.addWidget(
            self.make_rows_splitter('analyst.rows', top_row, tray_bar, bottom_row),
            stretch=1,
        )

        self.refresh_task_list()

    # ── Populate tables ─────────────────────────────────────────────────

    def _refresh_tables(self):
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
        """Submit a scene the analyst owns. A submission from status 4 lands in
        supervisor review directly (status 6 if a supervisor is already
        attached, else the status-5 pool), so the summary slide is built here
        too — see _build_slide_if_supervisor_bound."""
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        ok = self._run_db_action(
            lambda: submit_scene(self.conn, scene_id, self.user['id']), "Submit Failed"
        )
        problem = self._build_slide_if_supervisor_bound(scene_id) if ok else None
        self.refresh_task_list()
        if ok:
            QMessageBox.information(self, "Submitted", "Scene submitted.")
        if problem:
            QMessageBox.warning(self, "Summary Slide Not Built", problem)

    def _build_slide_if_supervisor_bound(self, scene_id):
        """Build the summary slide if this scene has just moved into supervisor
        review, either into the pool (5) or straight into a supervisor's queue
        (4 -> 6 on resubmission, ADR-015).

        Generating on the way in rather than when a supervisor claims the scene
        is deliberate: the cost lands on the analyst who is already waiting on
        a dialog, and the supervisor's Summary Slide button opens a file that
        is already there. Its freshness check covers anything that changed in
        between."""
        scene = get_scene_by_id(self.conn, scene_id)
        if scene is None or scene['status'] not in SceneStatus.SUPERVISOR_BOUND:
            return None
        return self.generate_summary_slide(scene_id)

    def _do_approve(self, scene_id, comment=None):
        """Peer reviewer approves a status-3 scene -> status 5."""
        ok = self._run_db_action(
            lambda: peer_review_scene(self.conn, scene_id, self.user['id'], Decision.APPROVE, comment),
            "Approve Failed"
        )
        problem = self._build_slide_if_supervisor_bound(scene_id) if ok else None
        self.refresh_task_list()
        if problem:
            QMessageBox.warning(self, "Summary Slide Not Built", problem)
        return ok

    def _do_kick_back(self, scene_id, comment=None):
        """Peer reviewer kicks back a status-3 scene -> status 4 with an optional comment."""
        ok = self._run_db_action(
            lambda: peer_review_scene(self.conn, scene_id, self.user['id'], Decision.REQUEST_REVISION, comment),
            "Kick Back Failed"
        )
        self.refresh_task_list()
        return ok

    def handle_approve(self):
        """Approve every selected status-3 scene. My Work Queue mixes statuses
        1/3/4, so anything not in peer review is reported as skipped rather
        than blocking the rest of the batch.

        Slide generation runs per scene but its failures are pooled into one
        dialog afterwards — approving a batch should not mean dismissing a
        warning per scene."""
        slide_problems = []

        def _approve(scene_id):
            peer_review_scene(self.conn, scene_id, self.user['id'], Decision.APPROVE, None)
            problem = self._build_slide_if_supervisor_bound(scene_id)
            if problem:
                slide_problems.append(problem)

        self.run_bulk_action(
            self.my_queue_table, _approve, "Approve",
            done_msg="{done} scene(s) approved and sent to supervisor.",
            none_msg="None of the selected scenes could be approved.",
            partial_msg="{done} scene(s) approved; {skipped} were no longer eligible.",
            confirm_msg="Approve {n} scenes?\n\nThis cannot be undone.",
        )
        if slide_problems:
            QMessageBox.warning(self, "Summary Slides Not Built", "\n\n".join(slide_problems))

    def handle_release(self):
        self.run_bulk_action(
            self.my_queue_table,
            lambda sid: release_scene_to_pool(self.conn, sid, self.user['id']),
            "Release",
            done_msg="{done} scene(s) released back to the pool.",
            none_msg="None of the selected scenes could be released.",
            partial_msg="{done} scene(s) released; {skipped} were no longer eligible.",
        )

    # ── Pool / review queue handlers ────────────────────────────────────

    def handle_claim_from_pool(self):
        self.run_bulk_action(
            self.scene_pool_table,
            lambda sid: claim_from_pool(self.conn, sid, self.user['id']),
            "Claim",
            done_msg="{done} scene(s) claimed and added to your work queue.",
            none_msg="None of the selected scenes are still available.",
            partial_msg="{done} scene(s) claimed; {skipped} were no longer available.",
        )

    def handle_claim_for_review(self):
        self.run_bulk_action(
            self.review_queue_table,
            lambda sid: claim_scene_for_review(self.conn, sid, self.user['id']),
            "Claim",
            done_msg="{done} scene(s) claimed for peer review.",
            none_msg="None of the selected scenes are still available.",
            partial_msg="{done} scene(s) claimed; {skipped} were no longer available.",
        )

