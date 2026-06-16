from PyQt6.QtWidgets import (
    QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox
)
from app.ui.dashboard import Dashboard
from app.db import get_analyst_queue, get_ready_queue
from app.controller import claim_scene_for_review
from app.models import SceneStatus


class AnalystDashboard(Dashboard):
    """analyst dashboard UI elements and logic"""

    def __init__(self, conn, user):
        super().__init__(conn, user)
        self._build_main_content()

    def _build_main_content(self):
        self.main_content_layout.addWidget(QLabel("My Work Queue"))

        self.my_queue_table = QTableWidget()
        self.my_queue_table.setColumnCount(3)
        self.my_queue_table.setHorizontalHeaderLabels(["ID", "Name", "Status"])
        self.my_queue_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.my_queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.my_queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.main_content_layout.addWidget(self.my_queue_table)

        self.main_content_layout.addWidget(QLabel("Available for Review"))

        self.review_queue_table = QTableWidget()
        self.review_queue_table.setColumnCount(3)
        self.review_queue_table.setHorizontalHeaderLabels(["ID", "Name", "Status"])
        self.review_queue_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.review_queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.review_queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.main_content_layout.addWidget(self.review_queue_table)

        claim_button = QPushButton("Claim")
        claim_button.clicked.connect(self.handle_claim)
        self.main_content_layout.addWidget(claim_button)

        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_task_list)
        self.main_content_layout.addWidget(refresh_button)

        self.refresh_task_list()

    def refresh_task_list(self):
        analyst_id = self.user['id']

        my_scenes = get_analyst_queue(self.conn, analyst_id)
        self.my_queue_table.setRowCount(len(my_scenes))
        for row, scene in enumerate(my_scenes):
            self.my_queue_table.setItem(row, 0, QTableWidgetItem(str(scene['id'])))
            self.my_queue_table.setItem(row, 1, QTableWidgetItem(scene['name']))
            self.my_queue_table.setItem(row, 2, QTableWidgetItem(SceneStatus.LABELS[scene['status']]))

        ready_scenes = [s for s in get_ready_queue(self.conn) if s['owner_id'] != analyst_id]
        self.review_queue_table.setRowCount(len(ready_scenes))
        for row, scene in enumerate(ready_scenes):
            self.review_queue_table.setItem(row, 0, QTableWidgetItem(str(scene['id'])))
            self.review_queue_table.setItem(row, 1, QTableWidgetItem(scene['name']))
            self.review_queue_table.setItem(row, 2, QTableWidgetItem(scene['status']))

    def handle_claim(self):
        if not self.review_queue_table.selectedItems():
            QMessageBox.warning(self, "No Selection", "Select a scene to claim.")
            return
        scene_id = int(self.review_queue_table.item(self.review_queue_table.currentRow(), 0).text())
        try:
            success = claim_scene_for_review(self.conn, scene_id, self.user['id'])
        except ValueError as e:
            QMessageBox.warning(self, "Claim Failed", str(e))
            self.refresh_task_list()
            return
        if success:
            QMessageBox.information(self, "Claimed", f"Scene {scene_id} claimed.")
        else:
            QMessageBox.warning(self, "Claim Failed", "Scene is no longer available.")
        self.refresh_task_list()
