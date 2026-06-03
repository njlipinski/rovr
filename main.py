# main.py
import sys
from PyQt6.QtWidgets import QApplication
from app.db import get_db_connection, initialize_db
from app.ui.login import LoginUI



def main():
    """Main entry point for the application."""
    # initialize database
    initialize_db()
    conn = get_db_connection()
    # create pyqt6 application object
    app = QApplication(sys.argv)
    # show login screen
    login_ui = LoginUI(conn)
    login_ui.show()
    # start event loop
    sys.exit(app.exec())
    

if __name__ == "__main__":
    main()
