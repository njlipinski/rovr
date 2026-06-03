"""base dashboard UI elements and logic"""
# app/ui/dashboard.py
from PyQt6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout

class Dashboard(QMainWindow):
    def __init__(self, conn, user):
        super().__init__()
        self.conn = conn
        self.user = user
        self.setWindowTitle(f"ROVR — {user['username']}")
        self.setMinimumSize(800, 500)
                
        # central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # outer layout: topbar on top, everything else below
        outer_layout = QVBoxLayout()
        central_widget.setLayout(outer_layout)

        # topbar
        self.topbar = QWidget()
        outer_layout.addWidget(self.topbar)
        # contains username and logout button, and scene select dropdown for analysts

        # bottom section: sidebar + content side by side
        bottom_layout = QHBoxLayout()
        outer_layout.addLayout(bottom_layout)

        # sidebar
        self.sidebar = QWidget()
        bottom_layout.addWidget(self.sidebar)

        # main content
        self.main_content = QWidget()
        bottom_layout.addWidget(self.main_content)
        