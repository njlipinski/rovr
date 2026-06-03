# app/ui/login.py
"""Login window — first screen the user sees"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QLineEdit,
    QPushButton, QMessageBox
)
from PyQt6.QtCore import Qt
from app.auth import authenticate_user


class LoginUI(QWidget):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.dashboard = None
        self._build_ui()

    def _build_ui(self):
        self.setWindowTitle("ROVR")
        self.setFixedSize(300, 180)

        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(30, 30, 30, 30)

        title = QLabel("ROVR")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username")
        layout.addWidget(self.username_input)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.returnPressed.connect(self.handle_login)
        layout.addWidget(self.password_input)

        self.login_button = QPushButton("Login")
        self.login_button.clicked.connect(self.handle_login)
        layout.addWidget(self.login_button)

        self.setLayout(layout)

    def handle_login(self):
        username = self.username_input.text().strip()
        password = self.password_input.text()

        if not username or not password:
            QMessageBox.warning(self, "Error", "Please enter a username and password.")
            return

        user = authenticate_user(self.conn, username, password)

        if user:
            self._open_dashboard(user)
        else:
            QMessageBox.warning(self, "Login Failed", "Invalid username or password.")
            self.password_input.clear()
            self.password_input.setFocus()

    def _open_dashboard(self, user):
        from app.ui.analyst_dash import AnalystDashboard
        from app.ui.supervisor_dash import SupervisorDashboard

        if user['role'] == 'analyst':
            self.dashboard = AnalystDashboard(self.conn, user)
        else:
            self.dashboard = SupervisorDashboard(self.conn, user)

        self.dashboard.show()
        self.close()
