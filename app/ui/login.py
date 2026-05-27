# app/ui/login.py
"""Login UI elements and logic"""

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox
from app.auth import authenticate_user

class LoginUI(QWidget):
    """Login UI elements and logic"""
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.setWindowTitle("Rovr Login")
        self.setGeometry(100, 100, 300, 150)
        layout = QVBoxLayout()
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Username")
        layout.addWidget(self.username_input)
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.password_input)
        login_button = QPushButton("Login")
        login_button.clicked.connect(self.handle_login)
        layout.addWidget(login_button)
        self.setLayout(layout)

    def handle_login(self):
        """Handle login button click"""
        username = self.username_input.text()
        password = self.password_input.text()
        user = authenticate_user(self.conn, username, password)
        if user:
            QMessageBox.information(self, "Login Successful", f"Welcome, {user['username']}!")

#TODO finish login logic to open appropriate dashboard based on user role
