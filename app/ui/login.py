# app/ui/login.py
"""Login window — first screen the user sees"""

import sqlite3

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QLineEdit,
    QPushButton, QMessageBox
)
from PyQt6.QtCore import Qt
from app.auth import authenticate_user
from app.db import ConnectionLost
from app.local_settings import get_last_login, set_last_login
from app.models import Role


# Usernames whose away window has already been consumed since this process
# started. Module level, not instance state: handle_logout() throws the old
# LoginUI away and builds a fresh one, so anything held on self would reset.
_away_consumed = set()


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

        # LoginUI isn't a Dashboard, so it can't reach _run_db_read — the same
        # handling is inlined here instead, as it is in the change-username and
        # change-password dialogs. The user lookup is a read, and a locked
        # database on it used to reach the crash dialog.
        try:
            user = authenticate_user(self.conn, username, password)
        except ConnectionLost as e:
            # Imported here, like the dashboards below: pulling dashboard.py in
            # at module level would drag its slide/plotting imports into launch.
            from app.ui.dashboard import connection_lost_message
            QMessageBox.warning(self, *connection_lost_message(e))
            return
        except sqlite3.OperationalError as e:
            if 'locked' not in str(e).lower():
                raise
            QMessageBox.warning(
                self, "Database Busy",
                "The shared database is busy and your login couldn't be checked. "
                "Please try again in a moment."
            )
            return

        if user:
            self._open_dashboard(user)
        else:
            QMessageBox.warning(self, "Login Failed", "Invalid username or password.")
            self.password_input.clear()
            self.password_input.setFocus()

    def _open_dashboard(self, user):
        from app.ui.analyst_dash import AnalystDashboard
        from app.ui.supervisor_dash import SupervisorDashboard

        away_since = self._consume_away_window(user['username'])
        if user['role'] == Role.ANALYST:
            self.dashboard = AnalystDashboard(self.conn, user, away_since=away_since)
        else:
            self.dashboard = SupervisorDashboard(self.conn, user, away_since=away_since)

        self.dashboard.show()
        self.close()

    @staticmethod
    def _consume_away_window(username):
        """Return the UTC stamp the While You Were Away summary should measure
        from, and move the stamp forward to now. None means show nothing."""
        if username in _away_consumed:
            return None
        _away_consumed.add(username)
        previous = get_last_login(username)
        set_last_login(username)
        return previous or None
