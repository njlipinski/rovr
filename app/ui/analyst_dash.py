"""analyst dashboard — work queue, peer review pool, and scene pool"""
from PyQt6.QtWidgets import QPushButton, QSplitter, QMessageBox, QTableWidgetItem
from PyQt6.QtCore import Qt
from app.ui.dashboard import (
    Dashboard, KickBackDialog, NotesDialog,
    make_scene_table, make_button_tray, make_section, clear_tray,
    parse_scene_name, TRAY_HEIGHT
)
from app.db import get_analyst_queue, get_ready_queue, get_scene_pool, get_scene_history
from app.controller import (
    claim_from_pool, claim_scene_for_review, submit_scene,
    release_scene_to_pool, peer_review_scene
)
from app.models import SceneStatus, Decision

# Context-sensitive buttons for My Queue by status: (label, handler_name)
_MY_QUEUE_BUTTONS = {
    SceneStatus.CLAIMED: [
        ("Open in ROI Studio", "handle_open_roi"),
        ("Submit",             "handle_submit"),
        ("See Notes",          "handle_see_notes"),
        ("Release",            "handle_release"),
    ],
    SceneStatus.IN_REVIEW: [
        ("Open in ROI Studio", "handle_open_roi"),
        ("Approve",            "handle_approve"),
        ("Kick Back",          "handle_kick_back"),
        ("See Notes",          "handle_see_notes"),
        ("Release",            "handle_release"),
    ],
    SceneStatus.NEEDS_REVISION: [
        ("Open in ROI Studio", "handle_open_roi"),
        ("Submit",             "handle_submit"),
        ("See Notes",          "handle_see_notes"),
        ("Release",            "handle_release"),
    ],
}


class AnalystDashboard(Dashboard):

    def __init__(self, conn, user):
        super().__init__(conn, user)
        self._build_main_content()

    # ── Build UI ────────────────────────────────────────────────────────

    def _build_main_content(self):
        # My Work Queue
        self.my_queue_table = make_scene_table(
            ["ID", "Rover", "Sol", "SeqID", "Status", "Analyst 1"]
        )
        self.my_queue_tray = make_button_tray()
        self.my_queue_table.itemSelectionChanged.connect(self._update_my_queue_tray)
        my_section = make_section("My Work Queue", self.my_queue_table, self.my_queue_tray)

        # Ready for Peer Review
        self.review_queue_table = make_scene_table(["ID", "Rover", "Sol", "SeqID"])
        self.review_queue_tray = make_button_tray()
        self.review_queue_table.itemSelectionChanged.connect(self._update_review_tray)
        review_section = make_section("Ready for Peer Review", self.review_queue_table, self.review_queue_tray)

        # Unclaimed Scenes
        self.scene_pool_table = make_scene_table(["ID", "Rover", "Sol", "SeqID"])
        self.scene_pool_tray = make_button_tray()
        self.scene_pool_table.itemSelectionChanged.connect(self._update_pool_tray)
        pool_section = make_section("Unclaimed Scenes", self.scene_pool_table, self.scene_pool_tray)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(my_section)
        splitter.addWidget(review_section)
        splitter.addWidget(pool_section)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 2)

        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_task_list)

        self.main_content_layout.addWidget(splitter)
        self.main_content_layout.addWidget(refresh_button)

        self.refresh_task_list()

    # ── Populate tables ─────────────────────────────────────────────────

    def refresh_task_list(self):
        analyst_id = self.user['id']

        my_scenes = get_analyst_queue(self.conn, analyst_id)
        self.my_queue_table.setRowCount(len(my_scenes))
        for row, scene in enumerate(my_scenes):
            rover, sol, seq = parse_scene_name(scene['name'])
            self.my_queue_table.setItem(row, 0, QTableWidgetItem(str(scene['id'])))
            self.my_queue_table.setItem(row, 1, QTableWidgetItem(rover))
            self.my_queue_table.setItem(row, 2, QTableWidgetItem(sol))
            self.my_queue_table.setItem(row, 3, QTableWidgetItem(seq))
            self.my_queue_table.setItem(row, 4, QTableWidgetItem(SceneStatus.LABELS[scene['status']]))
            self.my_queue_table.setItem(row, 5, QTableWidgetItem(scene['owner_username'] or '—'))

        ready_scenes = [s for s in get_ready_queue(self.conn) if s['owner_id'] != analyst_id]
        self.review_queue_table.setRowCount(len(ready_scenes))
        for row, scene in enumerate(ready_scenes):
            rover, sol, seq = parse_scene_name(scene['name'])
            self.review_queue_table.setItem(row, 0, QTableWidgetItem(str(scene['id'])))
            self.review_queue_table.setItem(row, 1, QTableWidgetItem(rover))
            self.review_queue_table.setItem(row, 2, QTableWidgetItem(sol))
            self.review_queue_table.setItem(row, 3, QTableWidgetItem(seq))

        pool_scenes = get_scene_pool(self.conn)
        self.scene_pool_table.setRowCount(len(pool_scenes))
        for row, scene in enumerate(pool_scenes):
            rover, sol, seq = parse_scene_name(scene['name'])
            self.scene_pool_table.setItem(row, 0, QTableWidgetItem(str(scene['id'])))
            self.scene_pool_table.setItem(row, 1, QTableWidgetItem(rover))
            self.scene_pool_table.setItem(row, 2, QTableWidgetItem(sol))
            self.scene_pool_table.setItem(row, 3, QTableWidgetItem(seq))

        self._update_my_queue_tray()
        self._update_review_tray()
        self._update_pool_tray()

    # ── Button tray updaters ────────────────────────────────────────────

    def _update_my_queue_tray(self):
        clear_tray(self.my_queue_tray)
        status = self.selected_status(self.my_queue_table)
        if status is None:
            return
        for label, handler in _MY_QUEUE_BUTTONS.get(status, []):
            btn = QPushButton(label)
            btn.clicked.connect(getattr(self, handler))
            self.my_queue_tray.layout().addWidget(btn)

    def _update_review_tray(self):
        clear_tray(self.review_queue_tray)
        if self.selected_id(self.review_queue_table) is None:
            return
        btn = QPushButton("Claim for Review")
        btn.clicked.connect(self.handle_claim_for_review)
        self.review_queue_tray.layout().addWidget(btn)

    def _update_pool_tray(self):
        clear_tray(self.scene_pool_tray)
        if self.selected_id(self.scene_pool_table) is None:
            return
        btn = QPushButton("Claim Scene")
        btn.clicked.connect(self.handle_claim_from_pool)
        self.scene_pool_tray.layout().addWidget(btn)

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
        pass  # TODO: launch ROI Studio

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
        """Peer reviewer kicks back a status-3 scene → status 4 with notes."""
        scene_id = self._my_queue_scene_id()
        if scene_id is None:
            return
        dialog = KickBackDialog(self)
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
        scene_name = " ".join([
            self.my_queue_table.item(row, c).text() for c in (1, 2, 3)
        ])
        history = get_scene_history(self.conn, scene_id)
        NotesDialog(scene_name, history, self).exec()

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
        scene_id = self.selected_id(self.scene_pool_table)
        if scene_id is None:
            return
        try:
            success = claim_from_pool(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Claim Failed", str(e))
            self.refresh_task_list()
            return
        if success:
            QMessageBox.information(self, "Claimed", "Scene claimed and added to your work queue.")
        else:
            QMessageBox.warning(self, "Claim Failed", "Scene is no longer available.")
        self.refresh_task_list()

    def handle_claim_for_review(self):
        scene_id = self.selected_id(self.review_queue_table)
        if scene_id is None:
            return
        try:
            success = claim_scene_for_review(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Claim Failed", str(e))
            self.refresh_task_list()
            return
        if success:
            QMessageBox.information(self, "Claimed", "Scene claimed for peer review.")
        else:
            QMessageBox.warning(self, "Claim Failed", "Scene is no longer available.")
        self.refresh_task_list()
