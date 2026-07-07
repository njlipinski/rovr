"""analyst dashboard — work queue, peer review pool, and scene pool"""
from PyQt6.QtWidgets import QPushButton, QSplitter, QTabWidget, QMessageBox, QTableWidgetItem
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from app.ui.dashboard import (
    Dashboard, KickBackDialog,
    make_scene_table, make_button_tray, make_section, clear_tray,
    parse_scene_key, apply_flag_delegate, make_flag_item,
)
from app.local_settings import get_all_scene_viewed_times, set_scene_viewed_at, get_dark_mode
from app.ui.styles import NEW_ACTIVITY_LIGHT, NEW_ACTIVITY_DARK
from app.db import get_analyst_queue, get_ready_queue, get_scene_pool, get_analyst_in_progress, get_analyst_completed
from app.controller import (
    claim_from_pool, claim_scene_for_review, submit_scene,
    release_scene_to_pool, peer_review_scene
)
from app.models import SceneStatus, Decision

# Context-sensitive buttons for My Queue by status: (label, handler_name)
_MY_QUEUE_BUTTONS = {
    SceneStatus.CLAIMED: [
        ("Open in ROI Studio", "handle_open_roi"),
        ("Open in Notebook",   "handle_open_notebook"),
        ("Submit",             "handle_submit"),
        ("See Notes",          "handle_see_notes"),
        ("Science Notes",      "handle_see_science_notes"),
        ("Flag Scene",         "handle_flag_from_my_queue"),
        ("Release",            "handle_release"),
    ],
    SceneStatus.IN_REVIEW: [
        ("Open in ROI Studio", "handle_open_roi"),
        ("Open in Notebook",   "handle_open_notebook"),
        ("Approve",            "handle_approve"),
        ("Kick Back",          "handle_kick_back"),
        ("See Notes",          "handle_see_notes"),
        ("Science Notes",      "handle_see_science_notes"),
        ("Flag Scene",         "handle_flag_from_my_queue"),
        ("Release",            "handle_release"),
    ],
    SceneStatus.NEEDS_REVISION: [
        ("Open in ROI Studio", "handle_open_roi"),
        ("Open in Notebook",   "handle_open_notebook"),
        ("Submit",             "handle_submit"),
        ("See Notes",          "handle_see_notes"),
        ("Science Notes",      "handle_see_science_notes"),
        ("Flag Scene",         "handle_flag_from_my_queue"),
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
            ["ID", "Rover", "Sol", "SeqID", "Status", "Obs", "Analyst 1", "Flags", "Name"]
        )
        apply_flag_delegate(self.my_queue_table)
        self.my_queue_tray = make_button_tray()
        self.my_queue_table.itemSelectionChanged.connect(self._update_my_queue_tray)
        my_section = make_section("My Work Queue", self.my_queue_table, self.my_queue_tray)

        # In Progress — scenes I've contributed to that are still moving
        self.in_progress_table = make_scene_table(
            ["ID", "Rover", "Sol", "SeqID", "Obs", "My Role", "Status", "Current Holder", "Name"]
        )
        self.in_progress_tray = make_button_tray()
        self.in_progress_table.itemSelectionChanged.connect(self._update_in_progress_tray)
        in_progress_section = make_section("In Progress", self.in_progress_table, self.in_progress_tray)

        # Ready for Peer Review
        self.review_queue_table = make_scene_table(["ID", "Rover", "Sol", "SeqID", "Obs", "Flags", "Name"])
        apply_flag_delegate(self.review_queue_table)
        self.review_queue_tray = make_button_tray()
        self.review_queue_table.itemSelectionChanged.connect(self._update_review_tray)
        review_section = make_section("Ready for Peer Review", self.review_queue_table, self.review_queue_tray)

        # Unclaimed Scenes
        self.scene_pool_table = make_scene_table(["ID", "Rover", "Sol", "SeqID", "Obs", "Flags", "Name"])
        apply_flag_delegate(self.scene_pool_table)
        self.scene_pool_tray = make_button_tray()
        self.scene_pool_table.itemSelectionChanged.connect(self._update_pool_tray)
        pool_section = make_section("Unclaimed Scenes", self.scene_pool_table, self.scene_pool_tray)

        # My Completed Scenes
        self.completed_table = make_scene_table(["ID", "Rover", "Sol", "SeqID", "Obs", "My Role", "Name"])
        self.completed_tray = make_button_tray()
        self.completed_table.itemSelectionChanged.connect(self._update_completed_tray)
        completed_section = make_section("My Completed Scenes", self.completed_table, self.completed_tray)

        pool_tabs = QTabWidget()
        pool_tabs.addTab(pool_section, "Unclaimed Scenes")
        pool_tabs.addTab(completed_section, "My Completed Scenes")

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

        self.main_content_layout.addWidget(splitter)
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
        self._fill_table(self.completed_table, get_analyst_completed(self.conn, analyst_id), fill_completed)

        self._update_my_queue_tray()
        self._update_in_progress_tray()
        self._update_review_tray()
        self._update_pool_tray()
        self._update_completed_tray()

    # ── Button tray updaters ────────────────────────────────────────────

    def _update_my_queue_tray(self):
        clear_tray(self.my_queue_tray)
        status = self.selected_status(self.my_queue_table)
        if status is None:
            return
        layout = self.my_queue_tray.layout()
        assert layout is not None
        for label, handler in _MY_QUEUE_BUTTONS.get(status, []):
            btn = QPushButton(label)
            btn.clicked.connect(getattr(self, handler))
            layout.addWidget(btn)

    def _update_in_progress_tray(self):
        clear_tray(self.in_progress_tray)
        if self.selected_id(self.in_progress_table) is None:
            return
        layout = self.in_progress_tray.layout()
        assert layout is not None
        for label, slot in [("Open in ROI Studio", self.handle_in_progress_open_roi),
                             ("Open in Notebook",   self.handle_in_progress_open_notebook),
                             ("Open Notes",         self.handle_in_progress_notes),
                             ("Science Notes",      self.handle_in_progress_science_notes),
                             ("Flag Scene",         self.handle_flag_from_in_progress)]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            layout.addWidget(btn)

    def _update_review_tray(self):
        clear_tray(self.review_queue_tray)
        if not self.selected_ids(self.review_queue_table):
            return
        layout = self.review_queue_tray.layout()
        assert layout is not None
        for label, slot in [("Claim for Review", self.handle_claim_for_review),
                             ("Flag Scene",       self.handle_flag_from_review)]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            layout.addWidget(btn)

    def _update_pool_tray(self):
        clear_tray(self.scene_pool_tray)
        if not self.selected_ids(self.scene_pool_table):
            return
        layout = self.scene_pool_tray.layout()
        assert layout is not None
        for label, slot in [("Claim Scene", self.handle_claim_from_pool),
                             ("Flag Scene",  self.handle_flag_from_pool)]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            layout.addWidget(btn)

    def _update_completed_tray(self):
        clear_tray(self.completed_tray)
        if self.selected_id(self.completed_table) is None:
            return
        layout = self.completed_tray.layout()
        assert layout is not None
        for label, slot in [("Open in ROI Studio", self.handle_completed_open_roi),
                             ("Open in Notebook",   self.handle_completed_open_notebook),
                             ("See Notes",          self.handle_completed_notes),
                             ("Science Notes",      self.handle_completed_science_notes)]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            layout.addWidget(btn)

    # ── In Progress handlers ────────────────────────────────────────────

    def handle_in_progress_open_roi(self):
        scene_id = self.selected_id(self.in_progress_table)
        if scene_id is None:
            return
        super().handle_open_roi(scene_id)

    def handle_in_progress_open_notebook(self):
        scene_id = self.selected_id(self.in_progress_table)
        if scene_id is None:
            return
        super().handle_open_notebook(scene_id)

    def handle_in_progress_notes(self):
        scene_id = self.selected_id(self.in_progress_table)
        if scene_id is None:
            return
        row = self.in_progress_table.currentRow()
        cells = [self.in_progress_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        self._show_notes(scene_id, scene_name)
        set_scene_viewed_at(scene_id)
        self.refresh_task_list()

    def handle_in_progress_science_notes(self):
        scene_id = self.selected_id(self.in_progress_table)
        if scene_id is None:
            return
        row = self.in_progress_table.currentRow()
        cells = [self.in_progress_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        self._show_science_notes(scene_id, scene_name)
        set_scene_viewed_at(scene_id)
        self.refresh_task_list()

    def handle_flag_from_in_progress(self):
        scene_id = self.selected_id(self.in_progress_table)
        if scene_id is None:
            return
        row = self.in_progress_table.currentRow()
        cells = [self.in_progress_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        self.handle_flag_scene(scene_id, scene_name)

    # ── Completed scenes handlers ───────────────────────────────────────

    def handle_completed_open_roi(self):
        scene_id = self.selected_id(self.completed_table)
        if scene_id is None:
            return
        super().handle_open_roi(scene_id)

    def handle_completed_open_notebook(self):
        scene_id = self.selected_id(self.completed_table)
        if scene_id is None:
            return
        super().handle_open_notebook(scene_id)

    def handle_completed_notes(self):
        scene_id = self.selected_id(self.completed_table)
        if scene_id is None:
            return
        row = self.completed_table.currentRow()
        cells = [self.completed_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        self._show_notes(scene_id, scene_name)

    def handle_completed_science_notes(self):
        scene_id = self.selected_id(self.completed_table)
        if scene_id is None:
            return
        row = self.completed_table.currentRow()
        cells = [self.completed_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        self._show_science_notes(scene_id, scene_name)

    # ── My Queue handlers ───────────────────────────────────────────────

    def _my_queue_scene_id(self):
        scene_id = self.selected_id(self.my_queue_table)
        if scene_id is None:
            QMessageBox.warning(self, "No Selection", "Select a scene first.")
        return scene_id

    def handle_open_roi(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        super().handle_open_roi(scene_id)

    def handle_open_notebook(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        super().handle_open_notebook(scene_id)

    def handle_submit(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        try:
            submit_scene(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Submit Failed", str(e))
            self.refresh_task_list()
            return
        QMessageBox.information(self, "Submitted", "Scene submitted.")
        self.refresh_task_list()

    def handle_approve(self):
        """Peer reviewer approves a status-3 scene → status 5."""
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        try:
            peer_review_scene(self.conn, scene_id, self.user['id'], Decision.APPROVE, None)
        except ValueError as e:
            QMessageBox.warning(self, "Approve Failed", str(e))
            self.refresh_task_list()
            return
        QMessageBox.information(self, "Approved", "Scene approved and sent to supervisor.")
        self.refresh_task_list()

    def handle_kick_back(self):
        """Peer reviewer kicks back a status-3 scene → status 4 with optional notes."""
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        row = self.my_queue_table.currentRow()
        cells = [self.my_queue_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        dialog = KickBackDialog(self.conn, scene_id, scene_name, self)
        if dialog.exec() != KickBackDialog.DialogCode.Accepted:
            return
        comments = dialog.get_comments()
        try:
            peer_review_scene(self.conn, scene_id, self.user['id'], Decision.REQUEST_REVISION, comments)
        except ValueError as e:
            QMessageBox.warning(self, "Kick Back Failed", str(e))
            self.refresh_task_list()
            return
        QMessageBox.information(self, "Kicked Back", "Scene returned to analyst with notes.")
        self.refresh_task_list()

    def handle_see_notes(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        row = self.my_queue_table.currentRow()
        cells = [self.my_queue_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        self._show_notes(scene_id, scene_name)

    def handle_see_science_notes(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        row = self.my_queue_table.currentRow()
        cells = [self.my_queue_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        self._show_science_notes(scene_id, scene_name)

    def handle_flag_from_my_queue(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        row = self.my_queue_table.currentRow()
        cells = [self.my_queue_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        self.handle_flag_scene(scene_id, scene_name)

    def handle_release(self):
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        try:
            release_scene_to_pool(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Release Failed", str(e))
            self.refresh_task_list()
            return
        QMessageBox.information(self, "Released", "Scene released back to pool.")
        self.refresh_task_list()

    # ── Pool / review queue handlers ────────────────────────────────────

    def handle_claim_from_pool(self):
        scene_ids = self.selected_ids(self.scene_pool_table)
        if not scene_ids:
            return
        claimed, skipped = 0, 0
        for scene_id in scene_ids:
            try:
                if claim_from_pool(self.conn, scene_id, self.user['id']):
                    claimed += 1
                else:
                    skipped += 1
            except ValueError:
                skipped += 1
        self.refresh_task_list()
        if skipped == 0:
            QMessageBox.information(self, "Claimed", f"{claimed} scene(s) claimed and added to your work queue.")
        elif claimed == 0:
            QMessageBox.warning(self, "Claim Failed", "None of the selected scenes are still available.")
        else:
            QMessageBox.information(self, "Partially Claimed", f"{claimed} scene(s) claimed; {skipped} were no longer available.")

    def handle_flag_from_pool(self):
        scene_id = self.selected_id(self.scene_pool_table)
        if scene_id is None:
            return
        row = self.scene_pool_table.currentRow()
        cells = [self.scene_pool_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        self.handle_flag_scene(scene_id, scene_name)

    def handle_claim_for_review(self):
        scene_ids = self.selected_ids(self.review_queue_table)
        if not scene_ids:
            return
        claimed, skipped = 0, 0
        for scene_id in scene_ids:
            try:
                if claim_scene_for_review(self.conn, scene_id, self.user['id']):
                    claimed += 1
                else:
                    skipped += 1
            except ValueError:
                skipped += 1
        self.refresh_task_list()
        if skipped == 0:
            QMessageBox.information(self, "Claimed", f"{claimed} scene(s) claimed for peer review.")
        elif claimed == 0:
            QMessageBox.warning(self, "Claim Failed", "None of the selected scenes are still available.")
        else:
            QMessageBox.information(self, "Partially Claimed", f"{claimed} scene(s) claimed; {skipped} were no longer available.")

    def handle_flag_from_review(self):
        scene_id = self.selected_id(self.review_queue_table)
        if scene_id is None:
            return
        row = self.review_queue_table.currentRow()
        cells = [self.review_queue_table.item(row, c) for c in (1, 2, 3)]
        scene_name = " ".join(c.text() if c else '' for c in cells)
        self.handle_flag_scene(scene_id, scene_name)
