"""base dashboard UI elements and logic"""

# app/ui/dashboard.py
from PyQt6.QtWidgets import QMainWindow

class Dashboard(QMainWindow):
    def __init__(self, conn, user):
        super().__init__()
        self.conn = conn
        self.user = user
        self.setWindowTitle(f"ROVR — {user['username']}")
        self.setMinimumSize(800, 500)
        