import re
from PyQt6.QtWidgets import (
    QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox
)
from app.ui.dashboard import Dashboard
from app.db import get_analyst_queue, get_ready_queue, get_scene_pool
from app.controller import claim_from_pool, claim_scene_for_review, submit_scene, release_scene_to_pool
from app.models import SceneStatus

# Parses 'MERAsol0042seqID2210' → ('MERA', '0042', '2210')
_NAME_RE = re.compile(r'^([A-Z]+)sol(\d{4})seqID(\d+)$')


def _parse_name(name):
    """Return (rover, sol, seqID) from a scene name, or ('', '', name) if unparseable."""
    m = _NAME_RE.match(name)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return '', '', name


def _make_scene_table(col_count=4):
    """Return a configured QTableWidget with Rover/Sol/SeqID/Status columns."""
    table = QTableWidget()
    table.setColumnCount(col_count)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    return table


def _populate_table(table, scenes, show_status=False):
    """Fill a scene table with Rover/Sol/SeqID columns, plus optional Status.
    Stores scene ID in column 0 (hidden via zero width isn't needed — ID is just col 0)."""
    table.setRowCount(len(scenes))
    for row, scene in enumerate(scenes):
        rover, sol, seq = _parse_name(scene['name'])
        table.setItem(row, 0, QTableWidgetItem(str(scene['id'])))
        table.setItem(row, 1, QTableWidgetItem(rover))
        table.setItem(row, 2, QTableWidgetItem(sol))
        table.setItem(row, 3, QTableWidgetItem(seq))
        if show_status:
            table.setItem(row, 4, QTableWidgetItem(SceneStatus.LABELS[scene['status']]))


class AnalystDashboard(Dashboard):
    """analyst dashboard UI elements and logic"""

    def __init__(self, conn, user):
        super().__init__(conn, user)
        self._build_main_content()

    def _build_main_content(self):
        # ── My Work Queue ──────────────────────────────────────────────
        self.main_content_layout.addWidget(QLabel("My Work Queue"))

        self.my_queue_table = _make_scene_table(col_count=5)
        self.my_queue_table.setHorizontalHeaderLabels(["ID","Rover", "Sol", "SeqID", "Status"])
        self.main_content_layout.addWidget(self.my_queue_table)
        
        submit_for_review_button = QPushButton("Submit")
        submit_for_review_button.clicked.connect(self.handle_submit_for_review)
        self.main_content_layout.addWidget(submit_for_review_button)
        remove_scene_button = QPushButton("Release Scene")
        remove_scene_button.clicked.connect(self.handle_remove_scene)
        self.main_content_layout.addWidget(remove_scene_button)

        # ── Ready for Peer Review ──────────────────────────────────────
        self.main_content_layout.addWidget(QLabel("Ready for Peer Review"))

        self.review_queue_table = _make_scene_table(col_count=4)
        self.review_queue_table.setHorizontalHeaderLabels(["ID","Rover", "Sol", "SeqID"])
        self.main_content_layout.addWidget(self.review_queue_table)

        claim_review_button = QPushButton("Claim for Review")
        claim_review_button.clicked.connect(self.handle_claim_for_review)
        self.main_content_layout.addWidget(claim_review_button)

        # ── Unclaimed Scenes ───────────────────────────────────────────
        self.main_content_layout.addWidget(QLabel("Unclaimed Scenes"))

        self.scene_pool_table = _make_scene_table(col_count=4)
        self.scene_pool_table.setHorizontalHeaderLabels(["ID","Rover", "Sol", "SeqID"])
        self.main_content_layout.addWidget(self.scene_pool_table)

        claim_pool_button = QPushButton("Claim Scene")
        claim_pool_button.clicked.connect(self.handle_claim_from_pool)
        self.main_content_layout.addWidget(claim_pool_button)

        # ── Refresh ────────────────────────────────────────────────────
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_task_list)
        self.main_content_layout.addWidget(refresh_button)

        self.refresh_task_list()

    def refresh_task_list(self):
        analyst_id = self.user['id']

        my_scenes = get_analyst_queue(self.conn, analyst_id)
        _populate_table(self.my_queue_table, my_scenes, show_status=True)

        ready_scenes = [s for s in get_ready_queue(self.conn) if s['owner_id'] != analyst_id]
        _populate_table(self.review_queue_table, ready_scenes)

        pool_scenes = get_scene_pool(self.conn)
        _populate_table(self.scene_pool_table, pool_scenes)

    def handle_claim_from_pool(self):
        if not self.scene_pool_table.selectedItems():
            QMessageBox.warning(self, "No Selection", "Select a scene to claim.")
            return
        scene_id = int(self.scene_pool_table.item(self.scene_pool_table.currentRow(), 0).text())
        try:
            success = claim_from_pool(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Claim Failed", str(e))
            self.refresh_task_list()
            return
        if success:
            QMessageBox.information(self, "Claimed", f"Scene {scene_id} claimed and added to your work queue.")
        else:
            QMessageBox.warning(self, "Claim Failed", "Scene is no longer available.")
        self.refresh_task_list()

    def handle_claim_for_review(self):
        if not self.review_queue_table.selectedItems():
            QMessageBox.warning(self, "No Selection", "Select a scene to claim for review.")
            return
        scene_id = int(self.review_queue_table.item(self.review_queue_table.currentRow(), 0).text())
        try:
            success = claim_scene_for_review(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Claim Failed", str(e))
            self.refresh_task_list()
            return
        if success:
            QMessageBox.information(self, "Claimed", f"Scene {scene_id} claimed for peer review.")
        else:
            QMessageBox.warning(self, "Claim Failed", "Scene is no longer available.")
        self.refresh_task_list()
        
    def handle_submit_for_review(self):
        if not self.my_queue_table.selectedItems():
            QMessageBox.warning(self, "No Selection", "Select a scene to submit.")
            return
        scene_id = int(self.my_queue_table.item(self.my_queue_table.currentRow(), 0).text())
        try:
            success = submit_scene(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Submission Failed", str(e))
            self.refresh_task_list()
            return
        if success:
            QMessageBox.information(self, "Submitted", f"Scene {scene_id} submitted and added to peer review pool.")
        else:
            QMessageBox.warning(self, "Submission failed", "Please try again.")
        self.refresh_task_list()
        
        
    def handle_remove_scene(self):
        if not self.my_queue_table.selectedItems():
            QMessageBox.warning(self, "No Selection", "Select a scene to release.")
            return
        scene_id = int(self.my_queue_table.item(self.my_queue_table.currentRow(), 0).text())
        try:
            success = release_scene_to_pool(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Release Failed", str(e))
            self.refresh_task_list()
            return
        if success:
            QMessageBox.information(self, "Released", f"Scene {scene_id} released and returned to shared pool.")
        else:
            QMessageBox.warning(self, "Claim Failed", "Scene is no longer available.")
        self.refresh_task_list()
        
