# main.py
import sys
import os
import shutil
import subprocess

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


def _try_update():
    """Check R drive for a newer build; if found, replace local exe and relaunch.

    Only runs on Windows frozen builds. Silently skips on any error (R drive
    unavailable, no version file, permissions issue, etc.) so a failed update
    never prevents ROVR from starting.
    """
    if not getattr(sys, 'frozen', False) or sys.platform != 'win32':
        return
    try:
        from app.version import __version__ as current_ver
        from config import PANCAM_PATH

        source_exe   = os.path.join(PANCAM_PATH, 'rovr.exe')
        version_file = os.path.join(PANCAM_PATH, 'rovr-version.txt')
        local_exe    = sys.executable

        # Skip if we're already running from the R drive copy
        if os.path.normcase(os.path.abspath(local_exe)) == \
            os.path.normcase(os.path.abspath(source_exe)):
            return

        with open(version_file, encoding='utf-8-sig') as f:
            latest_ver = f.read().strip()

        def _ver(v):
            try:
                return tuple(int(x) for x in v.split('.'))
            except ValueError:
                return (0,)

        if _ver(latest_ver) <= _ver(current_ver):
            return  # already up to date

        # Rename local exe out of the way (allowed on local NTFS even for a
        # running exe), copy the new build in, then relaunch.
        backup = local_exe + '.bak'
        if os.path.exists(backup):
            os.remove(backup)
        os.rename(local_exe, backup)
        shutil.copy2(source_exe, local_exe)
        try:
            os.remove(backup)
        except OSError:
            pass
        subprocess.Popen([local_exe] + sys.argv[1:])
        os._exit(0)
    except Exception:
        pass  # silently continue with current version


_try_update()

try:
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from PyQt6.QtGui import QIcon
    from app.db import get_db_connection, initialize_db
    from app.ui.login import LoginUI
    from app.resources import ICON_PATH

    def _crash_log_path():
        log_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'rovr')
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, 'error.log')

    def _install_crash_handler():
        """Log and surface unhandled exceptions raised inside Qt slots (button
        clicks, etc). Without this, PyQt6's default behavior is to print to
        stderr (invisible in a windowed/console=False build) and then abort
        the whole process — the app just vanishes with no error shown."""
        import traceback
        from datetime import datetime

        def _handle_exception(exc_type, exc_value, exc_tb):
            log_path = _crash_log_path()
            try:
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f"\n--- {datetime.now().isoformat()} ---\n")
                    traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
            except OSError:
                pass
            try:
                QMessageBox.critical(
                    None, "ROVR Error",
                    f"An unexpected error occurred:\n\n{exc_value}\n\n"
                    f"Details were saved to:\n{log_path}\n\n"
                    "You can keep working. Please submit this log file "
                    "if the problem continues."
                )
            except Exception:
                pass

        sys.excepthook = _handle_exception

    def main():
        initialize_db()
        conn = get_db_connection()

        # Must be set before QApplication is constructed — Qt reads this env
        # var once, at platform-integration startup, to uniformly scale all
        # widget geometry and fonts (buttons, tables, dialogs included).
        from app.local_settings import get_ui_scale
        ui_scale = get_ui_scale()
        if ui_scale != 1.0:
            os.environ['QT_SCALE_FACTOR'] = str(ui_scale)

        app = QApplication(sys.argv)
        _install_crash_handler()
        app.setWindowIcon(QIcon(ICON_PATH))
        from app.local_settings import get_dark_mode
        from app.ui.styles import apply_theme
        apply_theme(get_dark_mode())
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
