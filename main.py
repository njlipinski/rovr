# main.py
import sys
import os

# When frozen, config.py lives beside the exe/app (not bundled).
# On macOS, sys.executable is deep inside rovr.app/Contents/MacOS/ — walk up
# past the .app bundle to find the folder that actually contains config.py.
if getattr(sys, 'frozen', False):
    if sys.platform == 'darwin':
        _p = sys.executable
        while _p and not _p.endswith('.app'):
            _p = os.path.dirname(_p)
        sys.path.insert(0, os.path.dirname(_p))
    else:
        sys.path.insert(0, os.path.dirname(sys.executable))

try:
    from PyQt6.QtWidgets import QApplication
    from app.db import get_db_connection, initialize_db
    from app.ui.login import LoginUI

    def main():
        initialize_db()
        conn = get_db_connection()
        app = QApplication(sys.argv)
        login_ui = LoginUI(conn)
        login_ui.show()
        sys.exit(app.exec())

    if __name__ == "__main__":
        main()

except Exception:
    import traceback
    log_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'rovr')
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, 'error.log')
    with open(log_path, 'w') as f:
        traceback.print_exc(file=f)
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        QApplication(sys.argv)
        QMessageBox.critical(None, "ROVR failed to start",
                             f"An error occurred. Details saved to:\n{log_path}")
    except Exception:
        pass
    sys.exit(1)
