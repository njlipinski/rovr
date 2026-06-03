"""base dashboard UI elements and logic"""
# app/ui/dashboard.py
from PyQt6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton

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
        
        topbar_layout = QHBoxLayout()
        self.topbar.setLayout(topbar_layout)

        title_label = QLabel("ROVR")
        topbar_layout.addWidget(title_label)
        topbar_layout.addStretch()
        
        username_label = QLabel(self.user['username'])
        topbar_layout.addWidget(username_label)
        topbar_layout.addStretch()
        
        logout_button = QPushButton("Logout")
        topbar_layout.addWidget(logout_button)
        logout_button.clicked.connect(self.handle_logout)


        # bottom section: sidebar + content side by side
        bottom_layout = QHBoxLayout()
        outer_layout.addLayout(bottom_layout)

        # sidebar
        self.sidebar = QWidget()
        self.sidebar_layout = QVBoxLayout()
        self.sidebar.setLayout(self.sidebar_layout)
        bottom_layout.addWidget(self.sidebar)

        # main content
        self.main_content = QWidget()
        self.main_content_layout = QVBoxLayout()
        self.main_content.setLayout(self.main_content_layout)
        bottom_layout.addWidget(self.main_content)
        
    def handle_logout(self):
        from app.ui.login import LoginUI
        self.login = LoginUI(self.conn)
        self.login.show()
        self.close()
        