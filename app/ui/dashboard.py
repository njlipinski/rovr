"""base dashboard — shared layout, widgets, and utilities for all dashboard types"""
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter, QMenu, QGroupBox,
    QDialog, QTextEdit, QDialogButtonBox, QMessageBox, QFileDialog, QLineEdit,
    QCheckBox, QStyledItemDelegate, QStyle, QListWidget, QListWidgetItem, QInputDialog,
    QFrame,
    QApplication, QTabWidget, QSizePolicy,
)
from PyQt6.QtCore import Qt, QSize, QUrl, QTimer
from PyQt6.QtGui import (
    QPainter, QColor, QAction, QActionGroup, QTextCursor, QDesktopServices,
    QShortcut, QKeySequence,
)
from app.models import SceneStatus, SceneFlag, Role
from app.paths import (
    FolderKind, kind_path, find_sel_file, find_fits_file,
    find_scene_folder, summary_slide_paths,
)
from app.slides import build_summary_slide, slide_is_current
from app.local_settings import (
    get_roi_studio_path, set_roi_studio_path,
    get_roi_studio_python, set_roi_studio_python,
    get_column_widths, set_column_widths,
    get_dark_mode, set_dark_mode,
    get_confetti, set_confetti,
    get_ui_scale, set_ui_scale,
    get_dialog_size, set_dialog_size,
    get_splitter_sizes, set_splitter_sizes,
    get_note_height, set_note_height,
    set_scene_viewed_at,
)
from app.ui.styles import DARK_STYLESHEET, TRAY_HEIGHT, MIN_COL_WIDTH, apply_theme, color_button
from app.ui.confetti import ConfettiOverlay

# Preset options shown in the ☰ -> UI Scale menu. Applied via QT_SCALE_FACTOR
# at next launch (Qt reads it once, at startup)
UI_SCALE_PRESETS = (1.0, 1.25, 1.5, 1.75, 2.0)
from app.db import (
    get_scene_thread, add_note, update_note, delete_note, get_scene_by_id,
    get_science_notes, add_science_note, update_science_note, delete_science_note,
    update_username, update_user_password, get_user_by_username,
    update_scene_flags, get_user_stats, get_all_user_stats, get_all_supervisor_stats,
    get_supervisor_analyst_coverage, get_owned_activity_since, ConnectionLost,
)
from app.auth import verify_password, hash_password
from config import PANCAM_PATH
try:
    from app.version import __version__
except ImportError:
    __version__ = "dev"

# Parses 'MERB/sol0003/P2350/obs0' -> ('MERB', '0003', 'P2350', '0')
_KEY_RE = re.compile(r'^(MER[AB])/sol(\d{4})/([^/]+)/obs(\d+)$')


def connection_lost_message(exc):
    """(title, text) for a ConnectionLost, told apart by whether the connection
    came back. Neither case needs a restart, which is what users do now."""
    if exc.restored:
        return ("Connection Restored",
                f"The connection to {PANCAM_PATH} dropped and has been restored. "
                "Please try that again.")
    return ("Network Drive Unavailable",
            f"ROVR cannot reach {PANCAM_PATH}. Check your network or VPN connection "
            "and try again. You do not need to restart ROVR.")


def parse_scene_key(scene_key):
    """Return (rover, sol, seq_id, obs) from a scene_key, or ('','',scene_key,'0') if unparsable."""
    m = _KEY_RE.match(scene_key)
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(4)
    return '', '', scene_key, '0'


class WordSelectTextEdit(QTextEdit):
    """QTextEdit where double-click selects a whole word, treating apostrophes
    as part of the word (so "don't" or "can't" select as one unit instead of
    stopping at the punctuation, which is Qt's default).

    Pair it with a NoteResizeGrip to let the user set its height, and pass
    height_key to remember that height across restarts."""

    _WORD_RE = re.compile(r"[\w']+", re.UNICODE)
    _MIN_HEIGHT = 40

    def __init__(self, height_key=None, height=72, parent=None):
        super().__init__(parent)
        self._height_key = height_key
        self.setFixedHeight((height_key and get_note_height(height_key)) or height)

    def set_box_height(self, px):
        """Resize the box, floored. Not written to settings until save_height()."""
        self.setFixedHeight(max(self._MIN_HEIGHT, px))

    def save_height(self):
        """Remember the current height for next time, if this box is keyed."""
        if self._height_key:
            set_note_height(self._height_key, self.height())

    def mouseDoubleClickEvent(self, event):
        # Call super() first: besides doing Qt's default word selection (which we
        # override below), it arms Qt's internal triple-click timer as a side
        # effect. Returning early without calling it would silently break
        # triple-click-to-select-paragraph on the third click.
        super().mouseDoubleClickEvent(event)
        cursor = self.cursorForPosition(event.pos())
        block = cursor.block()
        pos_in_block = cursor.positionInBlock()
        for m in self._WORD_RE.finditer(block.text()):
            if m.start() <= pos_in_block <= m.end():
                cursor.setPosition(block.position() + m.start())
                cursor.setPosition(block.position() + m.end(), QTextCursor.MoveMode.KeepAnchor)
                self.setTextCursor(cursor)
                return


class NoteResizeGrip(QFrame):
    """Grey line above a note box. Drag it up to make the box taller."""

    _HEIGHT = 7  # thin line, but a band tall enough to grab

    def __init__(self, target):
        super().__init__()
        self._target = target
        self._drag_from = None
        self.setFrameShape(QFrame.Shape.HLine)
        self.setFrameShadow(QFrame.Shadow.Plain)
        self.setStyleSheet("color: #808080;")  # reads as grey on either theme
        self.setFixedHeight(self._HEIGHT)
        self.setCursor(Qt.CursorShape.SizeVerCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = (event.globalPosition().toPoint().y(), self._target.height())

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            start_y, start_h = self._drag_from
            self._target.set_box_height(
                start_h + (start_y - event.globalPosition().toPoint().y()))

    def mouseReleaseEvent(self, event):
        if self._drag_from is not None:
            self._drag_from = None
            self._target.save_height()


class SceneTable(QTableWidget):
    """QTableWidget whose columns fill available width proportionally and rescale on window resize."""

    def __init__(self, headers):
        super().__init__()
        self._key = "|".join(headers)
        self._in_resize = False

        self.setColumnCount(len(headers))
        self.setHorizontalHeaderLabels(headers)

        header = self.horizontalHeader()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setSortIndicatorShown(True)

        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setColumnHidden(0, True)
        self.setSortingEnabled(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._percentages = get_column_widths(self._key)
        header.sectionResized.connect(self._on_user_resize)

    def _visible_cols(self):
        return [i for i in range(self.columnCount()) if not self.isColumnHidden(i)]

    def _on_user_resize(self, logical, old_size, new_size):
        if self._in_resize:
            return
        vis = self._visible_cols()
        if logical not in vis:
            return
        delta = new_size - old_size
        if delta == 0:
            return
        idx = vis.index(logical)
        if idx >= len(vis) - 1:
            # No right neighbor -- revert
            self._in_resize = True
            try:
                self.setColumnWidth(logical, old_size)
            finally:
                self._in_resize = False
            return
        neighbor = vis[idx + 1]
        new_neighbor = self.columnWidth(neighbor) - delta
        if new_neighbor < MIN_COL_WIDTH:
            delta = self.columnWidth(neighbor) - MIN_COL_WIDTH
            new_neighbor = MIN_COL_WIDTH
        self._in_resize = True
        try:
            self.setColumnWidth(logical, old_size + delta)
            self.setColumnWidth(neighbor, new_neighbor)
        finally:
            self._in_resize = False
        vis_widths = [self.columnWidth(i) for i in vis]
        total_w = sum(vis_widths)
        if total_w > 0:
            self._percentages = [w / total_w for w in vis_widths]
            set_column_widths(self._key, self._percentages)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_percentages()

    def _apply_percentages(self):
        viewport = self.viewport()
        assert viewport is not None
        total = viewport.width()
        if total <= 0:
            return
        vis = self._visible_cols()
        n = len(vis)
        if n == 0:
            return
        proportions = (
            self._percentages
            if (self._percentages and len(self._percentages) == n)
            else [1.0 / n] * n
        )
        self._in_resize = True
        try:
            widths = [max(1, int(p * total)) for p in proportions]
            widths[-1] += total - sum(widths)  # absorb rounding error in last column
            for i, w in zip(vis, widths):
                self.setColumnWidth(i, w)
        finally:
            self._in_resize = False


def make_scene_table(headers):
    """Return a SceneTable with proportional, user-resizable columns; col 0 (ID) is always hidden."""
    return SceneTable(headers)


class PersistentSplitter(QSplitter):
    """QSplitter that remembers where the user dragged its handles, keyed by
    `key` in local settings. Pane sizes are stored as fractions of the total
    (like SceneTable's column widths) so a saved layout still applies when the
    window opens at a different size or UI scale."""

    # A drag emits splitterMoved on every mouse-move pixel and each save
    # rewrites local.json, so coalesce a whole drag into one write.
    _SAVE_DELAY_MS = 300

    def __init__(self, orientation, key):
        super().__init__(orientation)
        self._key = key
        self._fractions = get_splitter_sizes(key)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(self._SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._save)
        self.splitterMoved.connect(lambda *_: self._save_timer.start())

    def _save(self):
        sizes = self.sizes()
        total = sum(sizes)
        if total <= 0:
            return
        self._fractions = [s / total for s in sizes]
        set_splitter_sizes(self._key, self._fractions)

    def _pinned_extent(self, i):
        """Pane i's fixed size along this splitter's orientation, or None if it
        is free to resize. The shared button tray is pinned this way, and a band
        that must stay the same height cannot be sized from a fraction of a
        window that changes."""
        w = self.widget(i)
        if w is None:
            return None
        if self.orientation() == Qt.Orientation.Vertical:
            lo, hi = w.minimumHeight(), w.maximumHeight()
        else:
            lo, hi = w.minimumWidth(), w.maximumWidth()
        return lo if lo == hi else None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Reapplied on every resize, not just the first: the first one arrives
        # before the window has its real geometry, and stretch factors would
        # then pull the restored split back toward an even one as the window
        # grew to its final size. Once saved, the fraction is what this splitter
        # goes by, and only a drag changes it. Same rule as SceneTable's
        # column widths.
        if not self._fractions:
            return
        if len(self._fractions) != self.count():
            self._fractions = None  # pane count changed since this was saved
            return
        total = sum(self.sizes())
        if total <= 0:
            return

        # Pinned panes keep their own size and the free panes divide what is
        # left, their saved fractions renormalized over just that remainder.
        # Handing a pinned pane its fraction instead would leave Qt to clamp it
        # and hand the difference back on some rule of its own, drifting the
        # free panes off the split the user actually dragged.
        pinned = {i: e for i in range(self.count())
                    if (e := self._pinned_extent(i)) is not None}
        free = [i for i in range(self.count()) if i not in pinned]
        if not free:
            return
        free_total = total - sum(pinned.values())
        free_sum = sum(self._fractions[i] for i in free)
        if free_total <= 0 or free_sum <= 0:
            return

        sizes = [0] * self.count()
        for i, extent in pinned.items():
            sizes[i] = extent
        for i in free:
            sizes[i] = max(1, int(free_total * self._fractions[i] / free_sum))
        sizes[free[-1]] += total - sum(sizes)  # absorb rounding in the last free pane
        self.setSizes(sizes)


def make_button_tray():
    """Return a fixed-height QWidget containing an HBoxLayout for context-sensitive buttons."""
    tray = QWidget()
    tray.setFixedHeight(TRAY_HEIGHT)
    tray.setLayout(QHBoxLayout())
    tray.layout().setContentsMargins(0, 0, 0, 0)
    return tray


def clear_tray(tray):
    """Remove all buttons from a tray."""
    layout = tray.layout()
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()


# Scene actions that work the same way on any scene table: keyed by a short
# name, each mapping to (button label, Dashboard method). Dashboard.build_tray()
# binds these to whichever table the tray belongs to, so no dashboard needs a
# per-(table, action) wrapper method.
SCENE_ACTIONS = {
    'open_roi':      ("Open in ROI Studio", 'act_open_roi'),
    'open_notebook': ("Open in Notebook",   'act_open_notebook'),
    'open_folder':   ("Open File Location", 'act_open_folder'),
    # Opens the Review dialog. _review_callbacks() decides from the table and
    # status whether it can also approve or kick back, so one key serves every tray.
    'notes':         ("Review",             'act_notes'),
    'science_notes': ("Science Notes",      'act_science_notes'),
    'flag':          ("Flag Scene",         'act_flag'),
    'summary_slide': ("Summary Slide",      'act_summary_slide'),
}

# The full set, in the order it should appear in a tray.
SCENE_BUTTONS = ('notes', 'open_roi', 'open_notebook', 'open_folder', 'science_notes', 'flag')

# Keyboard shortcuts: key sequence -> the SCENE_ACTIONS it runs, in order.
# Adding one is one line here.
# Keyed by sequence rather than by action so one key can run several actions.
# Bare letters rather than Ctrl combinations, which the dashboard can afford
# because it has no text entry of its own - every input it opens is a modal
# dialog in a window of its own, where these do not fire. The cost is that Qt
# hands a shortcut the keystroke before the focused widget sees it, so these
# letters no longer reach a table's type-to-jump-to-row search.
SCENE_SHORTCUTS = {
    'R': ('open_roi',),
    'N': ('notes',),
    'S': ('summary_slide',),
    'E': ('summary_slide', 'notes'),
}


def shortcut_keys_for(action_key):
    """Every key that runs `action_key`, for labelling its button."""
    return [seq for seq, actions in SCENE_SHORTCUTS.items() if action_key in actions]


def make_section(label_text, table, tray=None, count_fn=None):
    """Wrap a label + table (+ optional button tray) into a QSplitter-compatible
    widget. Both dashboards now share one tray at the window bottom, so `tray`
    is normally omitted; it stays supported for a section that wants its own.

    If count_fn is given, it's called with the table and should return a
    string breakdown (e.g. "12 Unclaimed, 8 Claimed") appended after the
    item count; it's recomputed alongside the count on every row change."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 4, 0, 4)

    header_label = QLabel()

    def _update_count():
        n = table.rowCount()
        text = f"{label_text} — {n} item{'s' if n != 1 else ''}"
        if count_fn is not None:
            breakdown = count_fn(table)
            if breakdown:
                text += f"  ({breakdown})"
        header_label.setText(text)

    _update_count()

    model = table.model()
    assert model is not None
    model.rowsInserted.connect(lambda *_: _update_count())
    model.rowsRemoved.connect(lambda *_: _update_count())

    layout.addWidget(header_label)
    layout.addWidget(table)
    if tray is not None:
        layout.addWidget(tray)
    # rowsInserted fires from setRowCount(), before _fill_table's loop populates
    # cells -- so a count_fn reading cell contents needs an explicit recount
    # once filling actually finishes. Exposed here for callers to invoke.
    widget.refresh_count = _update_count
    return widget


class FlagDelegate(QStyledItemDelegate):
    """Renders colored flag squares in a table cell; stores raw flags string for sorting."""
    _SQ  = 10
    _GAP = 3
    _PAD = 4

    def paint(self, painter, option, index):
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        else:
            painter.fillRect(option.rect, option.palette.base())
        flags_str = index.data(Qt.ItemDataRole.DisplayRole) or '{}'
        flags_set = SceneFlag.parse(flags_str)
        x = option.rect.x() + self._PAD
        y = option.rect.y() + (option.rect.height() - self._SQ) // 2
        for flag_id in sorted(SceneFlag.LABELS.keys()):
            if flag_id in flags_set:
                painter.fillRect(x, y, self._SQ, self._SQ, QColor(SceneFlag.COLORS[flag_id]))
            x += self._SQ + self._GAP
        painter.restore()

    def sizeHint(self, option, index):
        n = len(SceneFlag.LABELS)
        return QSize(n * (self._SQ + self._GAP) + self._PAD * 2, 20)


def apply_flag_delegate(table):
    """Apply FlagDelegate to the 'Flags' column of a table, if present."""
    for col in range(table.columnCount()):
        item = table.horizontalHeaderItem(col)
        if item and item.text() == "Flags":
            table.setItemDelegateForColumn(col, FlagDelegate(table))
            return


def make_flag_item(flags_str):
    """Create a QTableWidgetItem for a flags cell (rendered by FlagDelegate)."""
    val = flags_str or '{}'
    item = QTableWidgetItem(val)
    flags_set = SceneFlag.parse(val)
    active = [SceneFlag.LABELS[f] for f in sorted(flags_set) if f in SceneFlag.LABELS]
    item.setToolTip("Flags: " + ", ".join(active) if active else "No flags")
    return item


class SizePersistentDialog(QDialog):
    """QDialog subclass that remembers its size (in local settings) across app
    restarts, keyed by _size_key. Subclasses must set self._size_key and call
    self._restore_size() once their minimum size is set in __init__."""
    _size_key = None

    def _restore_size(self):
        saved = get_dialog_size(self._size_key)
        if saved:
            self.resize(*saved)

    def done(self, result):
        if self._size_key:
            set_dialog_size(self._size_key, self.width(), self.height())
        super().done(result)


class FlagDialog(SizePersistentDialog):
    """Check/uncheck scene flags and optionally add a note. Always saves a note on OK."""
    _size_key = 'flags'

    def __init__(self, conn, scene_id, scene_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Flags — {scene_name}")
        self.setMinimumWidth(360)
        self._restore_size()
        layout = QVBoxLayout(self)

        scene = get_scene_by_id(conn, scene_id)
        self.old_flags = SceneFlag.parse(scene['flags'] if scene else '{}')

        layout.addWidget(QLabel("Flags:"))
        self._checks: dict[int, QCheckBox] = {}
        for flag_id, label in sorted(SceneFlag.LABELS.items()):
            cb = QCheckBox(label)
            cb.setChecked(flag_id in self.old_flags)
            color = SceneFlag.COLORS[flag_id]
            cb.setStyleSheet(
                f"QCheckBox::indicator:checked {{ background-color: {color}; border: 1px solid {color}; }}"
            )
            layout.addWidget(cb)
            self._checks[flag_id] = cb

        self._note = WordSelectTextEdit(height_key='flags')
        layout.addWidget(NoteResizeGrip(self._note))
        layout.addWidget(QLabel("Note:"))
        self._note.setPlaceholderText("Optional additional context...")
        layout.addWidget(self._note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_flags(self):
        return {fid for fid, cb in self._checks.items() if cb.isChecked()}

    def get_note_text(self):
        return self._note.toPlainText().strip()


_DECISION_LABEL = {
    'request_revision': 'Kick Back',
    'needs_revision':   'Kicked Back',
    'status_override':  'Status Override',
    'submitted':        'Submitted',
    'reset':            'Reset',
    'approve':          'Approve',
    'approved':         'Approved',
    'force_released':   'Force Released',
    'flag_updated':     'Flag Updated',
    'scene_edited':     'Scene Edited',
    'marked_issues':    'Marked Issues',
}


# How SQLite's datetime() renders a timestamp, and how ROVR displays one.
_TS_FORMAT = '%Y-%m-%d %H:%M:%S'


def local_ts(value):
    """Render a stored timestamp in the viewer's local time.
    Timestamps are stored UTC so that rows from users in different zones sort in
    real order."""
    if not value:
        return value
    try:
        stamp = datetime.strptime(str(value), _TS_FORMAT)
    except ValueError:
        return value
    # astimezone() with no argument uses the system zone, so this needs no
    # timezone database of its own.
    return stamp.replace(tzinfo=timezone.utc).astimezone().strftime(_TS_FORMAT)


def _format_thread_row(row):
    """Format one row from get_scene_thread (a note or a review entry)."""
    tag = 'Note' if row['type'] == 'note' else _DECISION_LABEL.get(row['decision'], row['decision'])
    header = f"[{local_ts(row['timestamp'])}]  {row['author_name']}  ({tag})"
    content = row['content']
    return f"{header}\n{content}" if content else header


def _format_science_note_row(row):
    """Format one row from get_science_notes."""
    header = f"[{local_ts(row['timestamp'])}]  {row['author_name']}"
    content = row['content']
    return f"{header}\n{content}" if content else header


def _format_science_notes_for_roi_studio(thread):
    """Concatenate a science notes thread (rows from get_science_notes) into the
    single metadata string ROI Studio's --notes argument expects."""
    return "\n\n---\n\n".join(_format_science_note_row(row) for row in thread)


# Virtualenv layouts checked beside a ROI Studio checkout's entry point, in the
# order they are tried. Windows puts the interpreter in Scripts/, everyone else
# in bin/.
_VENV_DIRS = ('.venv', 'venv', 'env')
_VENV_PYTHON = (('Scripts', 'python.exe'),) if sys.platform == 'win32' else \
                (('bin', 'python3'), ('bin', 'python'))


def find_venv_python(script_path):
    """Return the interpreter of a virtualenv sitting beside a ROI Studio
    checkout's entry point, or None. Looks in the script's own directory and its
    parent, so both repo/main.py and repo/src/main.py find repo/.venv."""
    first = os.path.dirname(os.path.abspath(script_path))
    roots = [first, os.path.dirname(first)]
    for root in roots:
        for venv in _VENV_DIRS:
            for parts in _VENV_PYTHON:
                candidate = os.path.join(root, venv, *parts)
                if os.path.exists(candidate):
                    return candidate
    return None


def roi_studio_command(path, args, interpreter=None):
    """Build the (command, cwd) that launches ROI Studio for a stored path.

    Three shapes are supported. A source checkout's .py entry point runs under
    the interpreter that has ROI Studio's dependencies, from the repo root so
    its relative imports and asset paths resolve. A macOS .app is a directory,
    not a binary, so it needs `open` the way Finder does. Anything else is run
    directly. cwd is None when the launch does not need one.
    """
    if path.endswith('.py'):
        return [interpreter, path] + args, os.path.dirname(os.path.abspath(path))
    if sys.platform == 'darwin' and path.rstrip('/').endswith('.app'):
        return ['open', '-n', '-a', path, '--args'] + args, None
    return [path] + args, None


class NotesDialog(SizePersistentDialog):
    """Read-write dialog showing a note thread for a scene, allowing new notes,
    and letting the author (or a supervisor) edit/delete past notes.

    Parameterized by get_thread/format_row/add_note_fn/update_note_fn/delete_note_fn
    so the same dialog backs both the regular Notes thread (manual notes + review
    housekeeping, via get_scene_thread/add_note/update_note/delete_note) and the
    Science Notes thread (manual, scientifically-relevant notes only, via
    get_science_notes/add_science_note/update_science_note/delete_science_note).
    Review/decision entries are never editable. Reviews are an append-only
    audit log, so edit/delete are only enabled for rows where type == 'note'.
    """
    _size_key = 'notes'

    def __init__(self, conn, scene_id, scene_name, author_id, parent=None,
                title="Notes", get_thread=get_scene_thread,
                format_row=_format_thread_row, add_note_fn=add_note,
                update_note_fn=update_note, delete_note_fn=delete_note,
                is_supervisor=False, on_approve=None, on_kick_back=None,
                on_submit=None, on_open_roi=None, on_summary_slide=None):
        """on_approve/on_kick_back/on_submit, if given, are callables
        (comment: str | None) -> bool (True on success). When set, the dialog shows the
        matching button, which sends the current note-box text as the comment and closes
        the dialog on success, so a user can leave a note and act on it without a second
        dialog. on_submit is the owner's side of that: an analyst reading why their scene
        was kicked back can resubmit from the same window once they have fixed it.

        on_open_roi, if given, is a callable () -> None that launches ROI Studio for
        this scene, so a reviewer can jump to ROI Studio without closing the note thread.

        on_summary_slide, if given, is a callable () -> None that opens this scene's
        summary slide -- the fast way to see the ROIs, spectra and metadata without
        waiting for ROI Studio to start."""
        super().__init__(parent)
        self.conn = conn
        self.scene_id = scene_id
        self.author_id = author_id
        self._get_thread = get_thread
        self._format_row = format_row
        self._add_note_fn = add_note_fn
        self._update_note_fn = update_note_fn
        self._delete_note_fn = delete_note_fn
        self._is_supervisor = is_supervisor
        self._on_approve = on_approve
        self._on_kick_back = on_kick_back
        self._on_submit = on_submit
        self._on_open_roi = on_open_roi
        self._on_summary_slide = on_summary_slide
        self.setWindowTitle(f"{title} — {scene_name}")
        self.setMinimumSize(520, 440)
        self._restore_size()
        layout = QVBoxLayout(self)

        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.itemSelectionChanged.connect(self._update_edit_buttons)
        layout.addWidget(self.list)

        edit_row = QWidget()
        edit_layout = QHBoxLayout(edit_row)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        self.edit_btn = QPushButton("Edit")
        self.edit_btn.clicked.connect(self._on_edit)
        self.edit_btn.setEnabled(False)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(self._on_delete)
        self.delete_btn.setEnabled(False)
        edit_layout.addWidget(self.edit_btn)
        edit_layout.addWidget(self.delete_btn)
        edit_layout.addStretch()
        layout.addWidget(edit_row)

        self.input = WordSelectTextEdit(height_key='notes', height=80)
        layout.addWidget(NoteResizeGrip(self.input))
        layout.addWidget(QLabel("Add a note:"))
        self.input.setPlaceholderText("Type a note...")
        layout.addWidget(self.input)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        if self._on_open_roi is not None:
            open_roi_btn = QPushButton("Open in ROI Studio")
            open_roi_btn.clicked.connect(self._on_open_roi)
            btn_layout.addWidget(open_roi_btn)
        if self._on_summary_slide is not None:
            slide_btn = QPushButton("Summary Slide")
            slide_btn.clicked.connect(self._on_summary_slide)
            btn_layout.addWidget(slide_btn)
        copy_id_btn = QPushButton("Copy Scene ID")
        copy_id_btn.clicked.connect(self._on_copy_scene_id)
        btn_layout.addWidget(copy_id_btn)
        add_btn = QPushButton("Add Note")
        add_btn.clicked.connect(self._on_add)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_layout.addStretch()
        if self._on_kick_back is not None:
            kick_back_btn = QPushButton("Kick Back")
            kick_back_btn.clicked.connect(self._on_kick_back_clicked)
            color_button(kick_back_btn)
            btn_layout.addWidget(kick_back_btn)
        if self._on_approve is not None:
            approve_btn = QPushButton("Approve")
            approve_btn.clicked.connect(self._on_approve_clicked)
            color_button(approve_btn)
            btn_layout.addWidget(approve_btn)
        if self._on_submit is not None:
            submit_btn = QPushButton("Submit")
            submit_btn.clicked.connect(self._on_submit_clicked)
            color_button(submit_btn)
            btn_layout.addWidget(submit_btn)
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(close_btn)
        layout.addWidget(btn_row)

        self._refresh()

    def _on_copy_scene_id(self):
        """Copy this scene's rover/sol/seqID/obsIX key to the clipboard, e.g.
        'MERA/sol0055/P2583/obs0', so it can be pasted into Slack or a note
        elsewhere without retyping it by hand."""
        scene = get_scene_by_id(self.conn, self.scene_id)
        if not scene:
            return
        QApplication.clipboard().setText(scene['scene_key'])

    def _can_edit(self, row):
        return row['type'] == 'note' and (self._is_supervisor or row['author_id'] == self.author_id)

    def _refresh(self):
        self.list.clear()
        try:
            thread = self._get_thread(self.conn, self.scene_id)
        except Exception as e:
            QMessageBox.warning(self, "Load Failed", f"Could not load notes: {e}")
            self._update_edit_buttons()
            return
        if not thread:
            item = QListWidgetItem("No notes yet.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)
        for row in thread:
            item = QListWidgetItem(self._format_row(row))
            item.setData(Qt.ItemDataRole.UserRole, dict(row))
            self.list.addItem(item)
        self._update_edit_buttons()

    def _selected_row(self):
        items = self.list.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.ItemDataRole.UserRole)

    def _update_edit_buttons(self):
        row = self._selected_row()
        editable = row is not None and self._can_edit(row)
        self.edit_btn.setEnabled(editable)
        self.delete_btn.setEnabled(editable)

    def _on_edit(self):
        row = self._selected_row()
        if row is None or not self._can_edit(row):
            return
        new_body, ok = QInputDialog.getMultiLineText(
            self, "Edit Note", "Note:", row['content'] or ''
        )
        if not ok:
            return
        new_body = new_body.strip()
        if not new_body:
            QMessageBox.warning(self, "Empty Note", "Note text cannot be empty.")
            return
        try:
            self._update_note_fn(self.conn, row['id'], new_body)
        except Exception as e:
            QMessageBox.warning(self, "Save Failed", f"Could not save note: {e}")
            return
        self._refresh()

    def _on_delete(self):
        row = self._selected_row()
        if row is None or not self._can_edit(row):
            return
        confirm = QMessageBox.question(
            self, "Delete Note", "Delete this note?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._delete_note_fn(self.conn, row['id'])
        except Exception as e:
            QMessageBox.warning(self, "Delete Failed", f"Could not delete note: {e}")
            return
        self._refresh()

    def _on_add(self):
        body = self.input.toPlainText().strip()
        if not body:
            QMessageBox.warning(self, "Empty Note", "Please enter some text before adding a note.")
            return
        try:
            self._add_note_fn(self.conn, self.scene_id, self.author_id, body)
        except Exception as e:
            QMessageBox.warning(self, "Save Failed", f"Could not save note: {e}")
            return
        self.input.clear()
        self._refresh()

    def _on_kick_back_clicked(self):
        comment = self.input.toPlainText().strip() or None
        if self._on_kick_back(comment):
            self.accept()

    def _on_approve_clicked(self):
        comment = self.input.toPlainText().strip() or None
        if self._on_approve(comment):
            self.accept()

    def _on_submit_clicked(self):
        comment = self.input.toPlainText().strip() or None
        if self._on_submit(comment):
            self.accept()


class ChangeUsernameDialog(SizePersistentDialog):
    """Let the logged-in user pick a new username."""
    _size_key = 'change_username'

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self._conn = conn
        self._user = user
        self.setWindowTitle("Change Username")
        self.setMinimumWidth(340)
        self._restore_size()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Current username:  {user['username']}"))
        layout.addWidget(QLabel("New username:"))
        self._field = QLineEdit()
        layout.addWidget(self._field)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self):
        new_name = self._field.text().strip()
        if not new_name:
            QMessageBox.warning(self, "Invalid", "Username cannot be empty.")
            return
        if new_name == self._user['username']:
            self.reject()
            return
        if get_user_by_username(self._conn, new_name):
            QMessageBox.warning(self, "Taken", f'"{new_name}" is already in use.')
            return
        try:
            update_username(self._conn, self._user['id'], new_name)
        except ConnectionLost as e:
            QMessageBox.warning(self, *connection_lost_message(e))
            return
        except sqlite3.OperationalError as e:
            if 'locked' not in str(e).lower():
                raise
            QMessageBox.warning(
                self, "Database Busy",
                "The shared database is busy and this couldn't be saved, even after "
                "retrying. Please try again in a moment."
            )
            return
        self.accept()

    def new_username(self):
        return self._field.text().strip()


class ChangePasswordDialog(SizePersistentDialog):
    """Let the logged-in user set a new password after confirming their current one."""
    _size_key = 'change_password'

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self._conn = conn
        self._user = user
        self.setWindowTitle("Change Password")
        self.setMinimumWidth(340)
        self._restore_size()
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Current password:"))
        self._current = QLineEdit()
        self._current.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self._current)

        layout.addWidget(QLabel("New password:"))
        self._new = QLineEdit()
        self._new.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self._new)

        layout.addWidget(QLabel("Confirm new password:"))
        self._confirm = QLineEdit()
        self._confirm.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self._confirm)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self):
        if not verify_password(self._current.text(), self._user['password_hash']):
            QMessageBox.warning(self, "Incorrect", "Current password is incorrect.")
            return
        new_pw = self._new.text()
        if len(new_pw) < 4:
            QMessageBox.warning(self, "Too Short", "New password must be at least 4 characters.")
            return
        if new_pw != self._confirm.text():
            QMessageBox.warning(self, "Mismatch", "New passwords do not match.")
            return
        try:
            update_user_password(self._conn, self._user['id'], hash_password(new_pw))
        except ConnectionLost as e:
            QMessageBox.warning(self, *connection_lost_message(e))
            return
        except sqlite3.OperationalError as e:
            if 'locked' not in str(e).lower():
                raise
            QMessageBox.warning(
                self, "Database Busy",
                "The shared database is busy and this couldn't be saved, even after "
                "retrying. Please try again in a moment."
            )
            return
        self.accept()


class FilterDialog(SizePersistentDialog):
    """Filter scenes by rover, sol range, and sequence ID range across all tables."""

    _size_key = 'filters'
    _DEFAULTS = {
        'rovers':    {'MERA', 'MERB'},
        'sol_min':   None,
        'sol_max':   None,
        'seqid_min': None,
        'seqid_max': None,
    }

    def __init__(self, filters, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filters")
        self.setMinimumWidth(320)
        self._restore_size()
        self._result = dict(filters)
        layout = QVBoxLayout(self)

        rover_box = QGroupBox("Rover")
        rover_lay = QHBoxLayout(rover_box)
        self._mera = QCheckBox("MERA")
        self._mera.setChecked('MERA' in filters['rovers'])
        self._merb = QCheckBox("MERB")
        self._merb.setChecked('MERB' in filters['rovers'])
        rover_lay.addWidget(self._mera)
        rover_lay.addWidget(self._merb)
        rover_lay.addStretch()
        layout.addWidget(rover_box)

        sol_box = QGroupBox("Sol Range")
        sol_lay = QHBoxLayout(sol_box)
        sol_lay.addWidget(QLabel("From"))
        self._sol_min = QLineEdit()
        self._sol_min.setPlaceholderText("0")
        self._sol_min.setFixedWidth(70)
        if filters['sol_min'] is not None:
            self._sol_min.setText(str(filters['sol_min']))
        sol_lay.addWidget(self._sol_min)
        sol_lay.addWidget(QLabel("to"))
        self._sol_max = QLineEdit()
        self._sol_max.setPlaceholderText("end")
        self._sol_max.setFixedWidth(70)
        if filters['sol_max'] is not None:
            self._sol_max.setText(str(filters['sol_max']))
        sol_lay.addWidget(self._sol_max)
        sol_lay.addStretch()
        layout.addWidget(sol_box)

        seq_box = QGroupBox("Sequence ID Range")
        seq_lay = QHBoxLayout(seq_box)
        seq_lay.addWidget(QLabel("From"))
        self._seqid_min = QLineEdit()
        self._seqid_min.setPlaceholderText("0")
        self._seqid_min.setFixedWidth(70)
        if filters['seqid_min'] is not None:
            self._seqid_min.setText(str(filters['seqid_min']))
        seq_lay.addWidget(self._seqid_min)
        seq_lay.addWidget(QLabel("to"))
        self._seqid_max = QLineEdit()
        self._seqid_max.setPlaceholderText("end")
        self._seqid_max.setFixedWidth(70)
        if filters['seqid_max'] is not None:
            self._seqid_max.setText(str(filters['seqid_max']))
        seq_lay.addWidget(self._seqid_max)
        seq_lay.addStretch()
        layout.addWidget(seq_box)

        btn_row = QHBoxLayout()
        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch()
        apply_btn = QPushButton("Apply")
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self._apply)
        btn_row.addWidget(apply_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    @staticmethod
    def _parse_int(text):
        try:
            return int(text.strip()) if text.strip() else None
        except ValueError:
            return None

    def _reset(self):
        self._mera.setChecked(True)
        self._merb.setChecked(True)
        self._sol_min.clear()
        self._sol_max.clear()
        self._seqid_min.clear()
        self._seqid_max.clear()

    def _apply(self):
        rovers = set()
        if self._mera.isChecked():
            rovers.add('MERA')
        if self._merb.isChecked():
            rovers.add('MERB')
        self._result = {
            'rovers':    rovers,
            'sol_min':   self._parse_int(self._sol_min.text()),
            'sol_max':   self._parse_int(self._sol_max.text()),
            'seqid_min': self._parse_int(self._seqid_min.text()),
            'seqid_max': self._parse_int(self._seqid_max.text()),
        }
        self.accept()

    def result_filters(self):
        return self._result


class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts by numeric value instead of string."""
    def __lt__(self, other):
        try:
            return float(self.text()) < float(other.text())
        except ValueError:
            return super().__lt__(other)


class StatsDialog(SizePersistentDialog):
    """Show productivity stats. Analysts see their own; supervisors see all users."""

    _size_key = 'stats'
    _ROWS = [
        ("Scenes submitted",       'submitted_total',    'submitted_today'),
        ("Peer reviews completed", 'peer_reviewed_total','peer_reviewed_today'),
        ("My scenes approved",     'approved_total',     'approved_today'),
        ("Scenes reworked",        'multi_kickback_scenes_total', 'multi_kickback_scenes_today'),
        ("Rework rate (2+ supervisor kicks / completed)", 'multi_kickback_rate_total', None),
    ]

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Statistics")
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)

        if user['role'] == Role.SUPERVISOR:
            self._build_tabs(layout, conn, user)
        else:
            self._build_own(layout, conn, user['id'])

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        # After the tab/own builders set their own default size, apply any
        # saved size on top so it isn't clobbered by those hardcoded defaults.
        self._restore_size()

    def _build_own(self, layout, conn, user_id):
        stats = get_user_stats(conn, user_id)
        layout.addWidget(QLabel("<b>My Statistics</b>"))

        tbl = QTableWidget(len(self._ROWS), 3)
        tbl.setHorizontalHeaderLabels(["", "Total", "Today"])
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tbl.horizontalHeader().setStretchLastSection(False)
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        for i, (label, total_key, today_key) in enumerate(self._ROWS):
            tbl.setItem(i, 0, QTableWidgetItem(label))
            total = QTableWidgetItem(str(stats[total_key]))
            total.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tbl.setItem(i, 1, total)
            today_text = str(stats[today_key]) if today_key else "—"
            today = QTableWidgetItem(today_text)
            today.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tbl.setItem(i, 2, today)

        tbl.setSortingEnabled(True)

        tbl.resizeRowsToContents()
        self.setMinimumWidth(380)
        layout.addWidget(tbl)

    _ANALYST_METRICS = [
        ("Submitted",           'submitted'),
        ("Peer Rev'd",          'peer_reviewed'),
        ("Approved",            'approved'),
        ("2+ Super. Kicks",     'multi_kickback_scenes'),
    ]

    # Total-only metrics: no meaningful week/today cut (the two counts span
    # different points in a scene's lifecycle), so these only appear on the
    # "Analyst Total" tab, not the Daily/Weekly tabs.
    _ANALYST_TOTAL_ONLY_METRICS = [
        ("Completed",           'completed_scenes'),
        ("Rework Rate",         'multi_kickback_rate'),
    ]

    # "2+ Kicks" counts scenes this supervisor kicked back twice or more
    _SUPERVISOR_METRICS = [
        ("Approved",    'approved'),
        ("Kicked Back", 'kicked_back'),
        ("2+ Kicks",    'multi_kickback_scenes'),
    ]

    @staticmethod
    def _make_table(row_count, headers):
        """Sortable, read-only table shell shared by every stats tab."""
        tbl = QTableWidget(row_count, len(headers))
        tbl.setHorizontalHeaderLabels(headers)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tbl.horizontalHeader().setStretchLastSection(False)
        return tbl

    @staticmethod
    def _num_item(val):
        text = f"{val:.2f}" if isinstance(val, float) else str(val)
        item = _NumericItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _build_tabs(self, layout, conn, user):
        all_analyst_stats = [
            (u, s) for u, s in get_all_user_stats(conn)
            if u['role'] != Role.SUPERVISOR and 'test' not in u['username'].lower()
        ]
        all_supervisor_stats = get_all_supervisor_stats(conn)
        coverage = [
            row for row in get_supervisor_analyst_coverage(conn, user['id'])
            if 'test' not in row['username'].lower()
        ]

        tabs = QTabWidget()
        tabs.addTab(self._build_analyst_period_tab(all_analyst_stats, 'today', "Today's"), "Analyst Daily")
        tabs.addTab(self._build_analyst_period_tab(all_analyst_stats, 'last', "Last Week's"), "Analyst Last Week")
        tabs.addTab(self._build_analyst_period_tab(all_analyst_stats, 'week', "This Week's"), "Analyst Weekly")
        tabs.addTab(self._build_analyst_period_tab(all_analyst_stats, 'total', "All-Time"), "Analyst Total")
        tabs.addTab(self._build_supervisor_tab(all_supervisor_stats), "All Supervisor Stats")
        tabs.addTab(self._build_coverage_tab(coverage), "My Analyst Coverage")
        layout.addWidget(tabs)
        self.setMinimumWidth(680)
        self.resize(720, 480)

        chart_btn = QPushButton("View Chart")
        chart_btn.clicked.connect(lambda: StatsChartDialog(all_analyst_stats, self).exec())
        layout.addWidget(chart_btn)

    def _build_analyst_period_tab(self, all_stats, period, label_prefix):
        container = QWidget()
        v = QVBoxLayout(container)
        v.addWidget(QLabel(f"<b>{label_prefix} Analyst Statistics</b>"))

        extra_metrics = self._ANALYST_TOTAL_ONLY_METRICS if period == 'total' else []
        headers = (["Username"] + [name for name, _ in self._ANALYST_METRICS]
                    + [name for name, _ in extra_metrics])
        tbl = self._make_table(len(all_stats), headers)

        for i, (user, stats) in enumerate(all_stats):
            tbl.setItem(i, 0, QTableWidgetItem(user['username']))
            for col, (_, key) in enumerate(self._ANALYST_METRICS, start=1):
                tbl.setItem(i, col, self._num_item(stats[f'{key}_{period}']))
            for col, (_, key) in enumerate(extra_metrics, start=1 + len(self._ANALYST_METRICS)):
                tbl.setItem(i, col, self._num_item(stats[f'{key}_total']))

        tbl.setSortingEnabled(True)
        tbl.resizeColumnsToContents()
        tbl.resizeRowsToContents()
        v.addWidget(tbl)
        return container

    _COVERAGE_COLUMNS = [
        ("In Progress",     'in_progress'),
        ("Approved",        'approved_mine'),
        ("Approved (all)",  'approved_any'),
    ]

    def _build_coverage_tab(self, coverage):
        container = QWidget()
        v = QVBoxLayout(container)
        headers = ["Analyst"] + [name for name, _ in self._COVERAGE_COLUMNS]
        tbl = self._make_table(len(coverage), headers)

        for i, row in enumerate(coverage):
            tbl.setItem(i, 0, QTableWidgetItem(row['username']))
            for col, (_, key) in enumerate(self._COVERAGE_COLUMNS, start=1):
                tbl.setItem(i, col, self._num_item(row[key]))

        tbl.setSortingEnabled(True)
        tbl.resizeColumnsToContents()
        tbl.resizeRowsToContents()
        v.addWidget(tbl)
        return container

    def _build_supervisor_tab(self, all_stats):
        container = QWidget()
        v = QVBoxLayout(container)
        v.addWidget(QLabel("<b>All Supervisor Statistics</b>"))

        headers = ["Username"]
        for name, _ in self._SUPERVISOR_METRICS:
            headers += [f"{name} Today", f"{name} Week", f"{name} Total"]
        tbl = self._make_table(len(all_stats), headers)

        for i, (user, stats) in enumerate(all_stats):
            tbl.setItem(i, 0, QTableWidgetItem(user['username']))
            col = 1
            for _, key in self._SUPERVISOR_METRICS:
                tbl.setItem(i, col,     self._num_item(stats[f'{key}_today']))
                tbl.setItem(i, col + 1, self._num_item(stats[f'{key}_week']))
                tbl.setItem(i, col + 2, self._num_item(stats[f'{key}_total']))
                col += 3

        tbl.setSortingEnabled(True)
        tbl.resizeColumnsToContents()
        tbl.resizeRowsToContents()
        v.addWidget(tbl)
        return container


class StatsChartDialog(SizePersistentDialog):
    """Bar chart of analyst activity stats with an All Time / Weekly / Today toggle."""

    _size_key = 'stats_chart'
    _METRICS = [
        ("Submitted",    'submitted_total',    'submitted_last',    'submitted_week',    'submitted_today'),
        ("Peer Rev'd",   'peer_reviewed_total','peer_reviewed_last','peer_reviewed_week','peer_reviewed_today'),
        ("Approved",     'approved_total',     'approved_last',     'approved_week',     'approved_today'),
        ("2+ Super. Kicks", 'multi_kickback_scenes_total', 'multi_kickback_scenes_last',
         'multi_kickback_scenes_week', 'multi_kickback_scenes_today'),
    ]

    _MODES = [
        ('total', "All Time",   "All-Time Activity"),
        ('last',  "Last Week",  "Last Week's Activity"),
        ('week',  "Weekly",     "This Week's Activity"),
        ('today', "Today",      "Today's Activity"),
    ]

    def __init__(self, all_stats, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Analyst Activity Chart")
        self.setSizeGripEnabled(True)
        self.resize(820, 480)
        self._restore_size()
        self._all_stats = all_stats

        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        import matplotlib.pyplot as plt
        self._plt = plt

        self._fig, self._ax = plt.subplots()
        self._canvas = FigureCanvasQTAgg(self._fig)

        toggle_row = QHBoxLayout()
        self._buttons = {}
        for mode, label, _ in self._MODES:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked, m=mode: self._switch(m))
            toggle_row.addWidget(btn)
            self._buttons[mode] = btn
        toggle_row.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(toggle_row)
        layout.addWidget(self._canvas, stretch=1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._mode = 'total'
        self._buttons['total'].setChecked(True)
        self._draw()

    def _switch(self, mode):
        self._mode = mode
        for m, btn in self._buttons.items():
            btn.setChecked(m == mode)
        self._draw()

    def _metrics_keyed(self):
        """Return [(display_label, stats_dict_key)] for the current toggle mode."""
        i = {'total': 1, 'last': 2, 'week': 3, 'today': 4}[self._mode]
        return [(m[0], m[i]) for m in self._METRICS]

    def _title(self):
        return next(title for mode, _, title in self._MODES if mode == self._mode)

    def _draw(self):
        import numpy as np

        ax = self._ax
        ax.clear()

        # Resolved before anything is drawn: the bar value labels take their
        # color at creation time, unlike the axis text recolored further down.
        dark = get_dark_mode()
        bg = '#2b2b2b' if dark else '#ffffff'
        fg = '#dddddd' if dark else '#000000'

        metrics = self._metrics_keyed()
        usernames = [u['username'] for u, _ in self._all_stats]
        n_users = len(usernames)
        n_metrics = len(metrics)
        x = np.arange(n_users)
        width = 0.8 / n_metrics

        for m_idx, (label, key) in enumerate(metrics):
            vals = [s[key] for _, s in self._all_stats]
            offset = (m_idx - n_metrics / 2 + 0.5) * width
            bars = ax.bar(x + offset, vals, width, label=label)
            ax.bar_label(bars, padding=2, fontsize=8, color=fg)

        ax.set_xticks(x)
        ax.set_xticklabels(usernames, rotation=20, ha='right')
        ax.set_ylabel("Count")
        ax.set_title(self._title())
        ax.legend(loc='upper right')
        ax.margins(y=0.15)

        self._fig.patch.set_facecolor(bg)
        ax.set_facecolor('#3c3f41' if dark else '#f8f8f8')
        ax.tick_params(colors=fg)
        ax.xaxis.label.set_color(fg)
        ax.yaxis.label.set_color(fg)
        ax.title.set_color(fg)
        for spine in ax.spines.values():
            spine.set_edgecolor(fg)
        legend = ax.get_legend()
        if legend:
            legend.get_frame().set_facecolor(bg)
            for text in legend.get_texts():
                text.set_color(fg)

        self._fig.tight_layout()
        self._canvas.draw()


class Dashboard(QMainWindow):
    # Built by each subclass's _build_main_content(). Declared here because
    # refresh_task_list() watches it for the queue-cleared celebration.
    my_queue_table: QTableWidget

    def __init__(self, conn, user, away_since=None):
        super().__init__()
        self.conn = conn
        self.user = user
        # UTC stamp this user was last here on this machine, or None to skip the
        # While You Were Away summary entirely (first run, or already shown this
        # session). Set by LoginUI, which owns the stamp.
        self._away_since = away_since
        self._away_summary_done = False
        self._filters = dict(FilterDialog._DEFAULTS)
        self._filters['rovers'] = set(FilterDialog._DEFAULTS['rovers'])
        self.setWindowTitle(f"ROVR {__version__} — {user['username']}")
        self.setMinimumSize(800, 500)
        apply_theme(get_dark_mode())

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        outer_layout = QVBoxLayout(central_widget)
        outer_layout.setContentsMargins(6, 4, 6, 4)
        outer_layout.setSpacing(4)

        # Topbar
        topbar = QWidget()
        topbar.setFixedHeight(32)
        topbar_layout = QHBoxLayout(topbar)
        topbar_layout.setContentsMargins(2, 0, 2, 0)
        topbar_layout.addWidget(QLabel("ROVR"))
        topbar_layout.addStretch()
        self._username_label = QLabel(self.user['username'])
        topbar_layout.addWidget(self._username_label)
        topbar_layout.addStretch()
        self._filter_btn = QPushButton("Filters")
        self._filter_btn.clicked.connect(self._open_filter_dialog)
        topbar_layout.addWidget(self._filter_btn)
        menu_btn = QPushButton("☰")
        menu_btn.setFixedWidth(34)
        menu_btn.setToolTip("Menu")
        menu_btn.clicked.connect(lambda: self._show_menu(menu_btn))
        topbar_layout.addWidget(menu_btn)
        logout_button = QPushButton("Logout")
        logout_button.clicked.connect(self.handle_logout)
        topbar_layout.addWidget(logout_button)
        outer_layout.addWidget(topbar)

        # Bottom: sidebar + main content
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(4)
        outer_layout.addLayout(bottom_layout)

        self.sidebar = QWidget()
        self.sidebar_layout = QVBoxLayout(self.sidebar)
        self.sidebar_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(self.sidebar, stretch=0)

        self.main_content = QWidget()
        self.main_content_layout = QVBoxLayout(self.main_content)
        self.main_content_layout.setContentsMargins(0, 0, 0, 0)
        self.main_content_layout.setSpacing(4)
        bottom_layout.addWidget(self.main_content, stretch=1)

    # ── While You Were Away ─────────────────────────────────────────────

    def showEvent(self, event):
        """Fire the away summary once, on the first show."""
        super().showEvent(event)
        if not self._away_summary_done:
            self._away_summary_done = True
            QTimer.singleShot(0, self._show_away_summary)

    def _away_summary_items(self):
        """Lines for the While You Were Away dialog, as
        [(count, singular_text, plural_text, celebrate)]. `celebrate` marks a
        line as good news: any of those with a nonzero count earns confetti."""
        return []

    def _show_away_summary(self):
        """Report what happened to this user's scenes since they were last here.
        Silent when there is no window to report on (first run on this machine)
        or when nothing in it moved."""
        if not self._away_since:
            return
        items = [item for item in self._away_summary_items() if item[0]]
        if not items:
            return
        lines = "\n".join(f"    {n} {one if n == 1 else many}"
                        for n, one, many, _ in items)
        # Started before the dialog, which blocks: the confetti is already
        # falling behind it by the time the user reads the good news.
        if get_confetti() and any(celebrate for _, _, _, celebrate in items):
            ConfettiOverlay(self)
        QMessageBox.information(
            self, "While You Were Away",
            f"Since your last visit ({local_ts(self._away_since)}):\n\n{lines}"
        )

    # ── Shared helpers available to all subclasses ──────────────────────

    def _run_db_action(self, fn, error_title="Action Failed"):
        """Run fn() (typically a lambda calling a controller function that
        writes to the DB), showing a message box instead of crashing for
        either a business-rule violation (ValueError, raised by controller.py)
        or database lock contention that outlasted every retry.
        Returns True on success, False if fn() raised."""
        try:
            fn()
            return True
        except ValueError as e:
            QMessageBox.warning(self, error_title, str(e))
        except ConnectionLost as e:
            QMessageBox.warning(self, *connection_lost_message(e))
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower():
                QMessageBox.warning(
                    self, "Database Busy",
                    "The shared database is busy and this action couldn't be saved, "
                    "even after retrying. Please try again in a moment."
                )
            else:
                raise
        return False

    def _run_db_read(self, fn, default=None):
        """Run fn() (a DB read), returning `default` instead of crashing if the
        shared database stayed locked for the whole read budget (see
        db._with_read_retry)."""
        try:
            return fn()
        except ConnectionLost as e:
            QMessageBox.warning(self, *connection_lost_message(e))
            return default
        except sqlite3.OperationalError as e:
            if 'locked' in str(e).lower():
                QMessageBox.warning(
                    self, "Database Busy",
                    "The shared database is busy, so this view may be out of date. "
                    "Press Refresh in a moment to try again."
                )
                return default
            raise

    def run_bulk_action(self, table, action, title, *,
                        done_msg, none_msg, partial_msg, confirm_msg=None,
                        celebrate_empty=True):
        """Apply 'action(scene_id)' to every selected row in 'table'.

        Each scene is written independently, so one that is no longer eligible 
        raises ValueError from 'controller.py' and is counted as
        skipped instead of aborting the batch. Lock contention is different: 
        it means nothing further can be written, so `_run_db_action` stops the 
        run and shows its own dialog.

        Returns the number of scenes acted on.
        """
        scene_ids = self.selected_ids(table)
        if not scene_ids:
            return 0
        if confirm_msg and len(scene_ids) > 1:
            answer = QMessageBox.question(
                self, title, confirm_msg.format(n=len(scene_ids)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return 0

        done = skipped = 0

        def _run_all():
            nonlocal done, skipped
            for scene_id in scene_ids:
                try:
                    if action(scene_id) is False:
                        skipped += 1
                    else:
                        done += 1
                except ValueError:
                    skipped += 1

        ok = self._run_db_action(_run_all, f"{title} Failed")
        self.refresh_task_list(celebrate_empty=celebrate_empty)
        if not ok:
            return done
        if skipped == 0:
            QMessageBox.information(self, title, done_msg.format(done=done, skipped=skipped))
        elif done == 0:
            QMessageBox.warning(self, f"{title} Failed", none_msg.format(done=done, skipped=skipped))
        else:
            QMessageBox.information(self, f"Partially {title}d",
                                    partial_msg.format(done=done, skipped=skipped))
        return done

    def _fill_table(self, table, rows, fill_fn):
        """Populate a table safely: disables sorting during insert to prevent mid-fill reorders."""
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            fill_fn(i, row)
            id_item = table.item(i, 0)
            if id_item is not None:
                try:
                    id_item.setData(Qt.ItemDataRole.UserRole, row['scene_key'])
                except (IndexError, KeyError):
                    pass
        table.setSortingEnabled(True)
        self._apply_filters(table)

    def _apply_filters(self, table):
        f = self._filters
        for row in range(table.rowCount()):
            id_item = table.item(row, 0)
            if id_item is None:
                continue
            scene_key = id_item.data(Qt.ItemDataRole.UserRole)
            if not scene_key:
                table.setRowHidden(row, False)
                continue
            rover, sol_str, seq_id, _ = parse_scene_key(scene_key)
            hide = False
            if rover and rover not in f['rovers']:
                hide = True
            if not hide:
                try:
                    sol = int(sol_str)
                    if f['sol_min'] is not None and sol < f['sol_min']:
                        hide = True
                    elif f['sol_max'] is not None and sol > f['sol_max']:
                        hide = True
                except (ValueError, TypeError):
                    pass
            if not hide:
                m = re.search(r'\d+', seq_id or '')
                if m:
                    seq_num = int(m.group())
                    if f['seqid_min'] is not None and seq_num < f['seqid_min']:
                        hide = True
                    elif f['seqid_max'] is not None and seq_num > f['seqid_max']:
                        hide = True
            table.setRowHidden(row, hide)

    def _open_filter_dialog(self):
        dlg = FilterDialog(self._filters, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._filters = dlg.result_filters()
            self._update_filter_btn()
            self.refresh_task_list(celebrate_empty=False)

    def _update_filter_btn(self):
        f = self._filters
        active = (
            f['rovers'] != {'MERA', 'MERB'} or
            any(f[k] is not None for k in ('sol_min', 'sol_max', 'seqid_min', 'seqid_max'))
        )
        self._filter_btn.setText("Filters ●" if active else "Filters")

    def selected_id(self, table):
        """Return scene ID from hidden col 0 of the selected row, or None."""
        row = table.currentRow()
        if row < 0 or not table.selectedItems():
            return None
        return int(table.item(row, 0).text())

    def selected_ids(self, table) -> list[int]:
        """Return scene IDs from col 0 of all selected rows."""
        return [
            int(table.item(idx.row(), 0).text())
            for idx in table.selectionModel().selectedRows()
        ]

    def selected_status(self, table):
        """Return status int from col 4 of the selected row, or None."""
        row = table.currentRow()
        if row < 0 or not table.selectedItems():
            return None
        item = table.item(row, 4)
        if not item:
            return None
        label = item.text()
        for status, name in SceneStatus.LABELS.items():
            if name == label:
                return status
        return None

    def _scene_name_from(self, table):
        """Return 'Rover Sol SeqID' from cols 1-3 of the selected row."""
        row = table.currentRow()
        cells = [table.item(row, c) for c in (1, 2, 3)]
        return " ".join(c.text() if c else '' for c in cells)

    def _prompt_for_roi_studio_path(self):
        """Ask the user to locate ROI Studio and save the path. Returns the path, or None if cancelled.

        The target may be a packaged build or a source checkout's main.py - the
        filters offer both, since not everyone has a packaged ROI Studio."""
        if sys.platform == 'darwin':
            file_filter = "ROI Studio (*.app *.py);;All Files (*)"
        elif sys.platform == 'win32':
            file_filter = "ROI Studio (*.exe *.py);;All Files (*)"
        else:
            file_filter = "ROI Studio (*.py);;All Files (*)"
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate ROI Studio", "", file_filter
        )
        if not path:
            return None
        # Resolve the interpreter before storing the path, so backing out of the
        # interpreter step does not leave a checkout configured with no way to
        # run it.
        if path.endswith('.py') and not self._resolve_roi_studio_python(path):
            return None
        set_roi_studio_path(path)
        return path

    def _resolve_roi_studio_python(self, script_path):
        """Find and save the interpreter that runs a ROI Studio checkout. Uses a
        venv beside the script if there is one, otherwise asks. Returns the
        path, or None if cancelled."""
        found = find_venv_python(script_path)
        if found:
            set_roi_studio_python(found)
            return found
        QMessageBox.information(
            self, "Python Interpreter Needed",
            "That is a ROI Studio source checkout, so ROVR needs the Python "
            "interpreter that has its dependencies installed - usually the one "
            "inside the checkout's virtual environment.\n\n"
            "Locate it on the next screen."
        )
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate Python for ROI Studio",
            os.path.dirname(os.path.abspath(script_path)), "All Files (*)"
        )
        if not path:
            return None
        set_roi_studio_python(path)
        return path

    def handle_open_roi(self, scene_id):
        """Launch ROI Studio and open the given scene."""
        path = get_roi_studio_path()
        if not path or not os.path.exists(path):
            path = self._prompt_for_roi_studio_path()
            if not path:
                return

        interpreter = None
        if path.endswith('.py'):
            interpreter = get_roi_studio_python()
            if not interpreter or not os.path.exists(interpreter):
                interpreter = self._resolve_roi_studio_python(path)
                if not interpreter:
                    return

        args = []
        try:
            scene = get_scene_by_id(self.conn, scene_id)
        except Exception as e:
            QMessageBox.warning(self, "Database Error", f"Could not load scene: {e}")
            return
        if scene:
            rover, sol, seq_id, _ = parse_scene_key(scene['scene_key'])
            obs_ix = scene['obs_ix'] if scene['obs_ix'] is not None else 0
            folder_path = kind_path(PANCAM_PATH, rover, sol, FolderKind.IOF)
            args += [folder_path, seq_id, str(obs_ix), 'PCAM']
            roi_file = find_fits_file(PANCAM_PATH, scene) or find_sel_file(PANCAM_PATH, scene)
            if roi_file:
                args.append(roi_file)
            notes_thread = get_science_notes(self.conn, scene_id)
            if notes_thread:
                args += ['--notes', _format_science_notes_for_roi_studio(notes_thread)]

        cmd, cwd = roi_studio_command(path, args, interpreter)
        try:
            subprocess.Popen(cmd, cwd=cwd)
        except OSError as e:
            QMessageBox.warning(self, "Launch Failed", f"Could not open ROI Studio:\n{e}")

    def handle_open_notebook(self, scene_id):
        """Open the MER Analyst's Notebook to this scene's Sol Summary, and copy
        the sol number to the clipboard. The Notebook's own left-hand Sol
        navigator can't be set via URL."""
        try:
            scene = get_scene_by_id(self.conn, scene_id)
        except Exception as e:
            QMessageBox.warning(self, "Database Error", f"Could not load scene: {e}")
            return
        if not scene:
            return
        rover, sol, _, _ = parse_scene_key(scene['scene_key'])
        if not rover or not sol:
            QMessageBox.warning(self, "Missing Data", "Could not determine rover/sol for this scene.")
            return
        sol_num = int(sol)
        url = f"https://an.rsl.wustl.edu/{rover.lower()}/AN/an3.aspx?it=SS&ii={sol_num}"
        QApplication.clipboard().setText(str(sol_num))
        QDesktopServices.openUrl(QUrl(url))

    def handle_open_folder(self, scene_id):
        """Open this scene's saved ROI Studio folder in the system file browser
        (Explorer on Windows, Finder on macOS).

        Handed to the OS via QDesktopServices so there is no platform branch to
        keep in sync. Scenes with no findable folder are a known state on the
        drive, so rather than dead-ending, fall back to the sol's working/
        folder and say why."""
        try:
            scene = get_scene_by_id(self.conn, scene_id)
        except Exception as e:
            QMessageBox.warning(self, "Database Error", f"Could not load scene: {e}")
            return
        if not scene:
            return

        folder = find_scene_folder(PANCAM_PATH, scene)
        if not folder:
            rover, sol, _, _ = parse_scene_key(scene['scene_key'])
            fallback = (kind_path(PANCAM_PATH, rover, sol, FolderKind.WORKING)
                        if rover and sol else None)
            if not fallback or not os.path.isdir(fallback):
                QMessageBox.warning(
                    self, "Folder Not Found",
                    f"No saved ROI Studio folder for '{scene['name']}', and its sol "
                    f"folder could not be reached on the drive."
                )
                return
            QMessageBox.information(
                self, "No Saved Folder",
                f"No saved ROI Studio folder for '{scene['name']}'.\n\n"
                f"Opening the sol's working folder instead:\n{fallback}"
            )
            folder = fallback
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # ── Generic per-table scene actions ─────────────────────────────────
    # Each takes the table whose selection it acts on. Trays bind them to a
    # specific table via build_tray(), so a dashboard never defines its own
    # per-table wrapper, which is what made these actions easy to shadow.

    def _bind(self, attr, table):
        """Bind a per-table action to a slot safe for QPushButton.clicked.

        clicked emits a 'checked' bool. Swallowing it here keeps the action
        signatures clean and stops the extra argument from reaching them."""
        action = getattr(self, attr)
        return lambda *_: action(table)

    def build_tray(self, tray, table, actions, enabled=True):
        """Rebuild `tray`'s buttons for the current selection in `table`.

        Each entry in `actions` is either a SCENE_ACTIONS key (bound to
        `table`) or an explicit (label, method name) pair for a handler that
        is specific to this dashboard, like Submit or Claim."""
        clear_tray(tray)
        if not enabled:
            return
        layout = tray.layout()
        assert layout is not None
        for entry in actions:
            keys = []
            if isinstance(entry, str):
                label, attr = SCENE_ACTIONS[entry]
                slot = self._bind(attr, table)
                keys = shortcut_keys_for(entry)
            else:
                label, handler = entry
                slot = getattr(self, handler)
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            if keys:
                btn.setToolTip(f"{label}  ({', '.join(keys)})")
            color_button(btn)
            layout.addWidget(btn)

    # ── The shared tray ─────────────────────────────────────────────────
    # Both dashboards show several scene tables at once. Rather than give each
    # one its own tray, all buttons live in a single tray that acts on
    # whichever table currently holds the selection. Selecting in one table
    # clears the others, so "the selected scene" is never ambiguous.

    def make_tray_bar(self, sections):
        """Build the shared tray and wire up single-table selection."""
        self._sections = list(sections)
        self._active_table = None
        self._syncing = False
        self._install_scene_shortcuts()

        self.shared_tray = make_button_tray()
        self.tray_label = QLabel()
        self.tray_label.setObjectName("trayLabel")

        for table, _, _ in self._sections:
            table.itemSelectionChanged.connect(
                lambda t=table: self._on_table_selection_changed(t)
            )

        self.refresh_button = QPushButton("Refresh")
        # Explicit lambda, not a direct connect: clicked() emits a bool that
        # would otherwise land in celebrate_empty.
        self.refresh_button.clicked.connect(
            lambda: self.refresh_task_list(celebrate_empty=False))

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self.shared_tray, stretch=1)
        row_layout.addWidget(self.refresh_button)

        bar = QWidget()
        # A plain QWidget is vertically Preferred, which lets it share surplus
        # window height with the tables around it. Fixed pins the bar to its hint 
        # (label + tray), so every extra pixel goes to the tables. The dashboard 
        # must also give its table splitter stretch=1; a horizontal QSplitter is only
        # vertically Preferred and won't claim that space on its own.
        bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.tray_label)
        layout.addWidget(row)
        bar.setFixedHeight(max(bar.sizeHint().height(), bar.minimumSizeHint().height()))
        self.update_shared_tray()
        self.tray_bar = bar
        return bar

    def make_rows_splitter(self, key, top, tray_bar, bottom, weights=(1, 1)):
        """Stack a row of tables, the shared tray, and a second row of tables in
        one vertical splitter, saved under `key`. `weights` is the (top, bottom)
        stretch used until the user drags a handle."""
        rows = PersistentSplitter(Qt.Orientation.Vertical, key)
        rows.addWidget(top)
        rows.addWidget(tray_bar)
        rows.addWidget(bottom)
        rows.setStretchFactor(0, weights[0])
        rows.setStretchFactor(1, 0)
        rows.setStretchFactor(2, weights[1])
        rows.setCollapsible(1, False)
        return rows

    def _on_table_selection_changed(self, table):
        """Make `table` the active one and clear every other table's selection.

        clearSelection() re-emits itemSelectionChanged on the tables it
        touches, so the _syncing guard is what stops this from recursing."""
        if self._syncing:
            return
        if not table.selectedItems():
            # Selection was cleared (often by a refresh repopulating rows).
            # Only the active table losing its selection empties the tray.
            if table is self._active_table:
                self._active_table = None
                self.update_shared_tray()
            return
        self._syncing = True
        try:
            for other, _, _ in self._sections:
                if other is not table:
                    other.clearSelection()
        finally:
            self._syncing = False
        self._active_table = table
        self.update_shared_tray()

    def _actions_for(self, table):
        """The tray entries for `table`, resolving the callable form used by
        tables whose buttons depend on the selected row's status."""
        actions = next((a for tbl, _, a in getattr(self, '_sections', []) if tbl is table), [])
        return actions() if callable(actions) else actions

    def _install_scene_shortcuts(self):
        for sequence in SCENE_SHORTCUTS:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(
                lambda s=sequence: self._run_scene_shortcut(s)
            )

    def _run_scene_shortcut(self, sequence):
        table = self._active_table
        if table is None or not table.selectedItems():
            return
        offered = [a for a in self._actions_for(table) if isinstance(a, str)]
        for action_key in SCENE_SHORTCUTS[sequence]:
            if action_key in offered:
                getattr(self, SCENE_ACTIONS[action_key][1])(table)

    def update_shared_tray(self):
        """Rebuild the shared tray for the active table's current selection."""
        table = self._active_table
        # Also treat a stale active table as empty: a refresh can repopulate
        # rows without the selection-changed signal reaching us.
        if table is None or not table.selectedItems():
            self._active_table = None
            self.build_tray(self.shared_tray, None, [], enabled=False)
            self.tray_label.setText("No scene selected")
            return
        title = next(t for tbl, t, _ in self._sections if tbl is table)
        actions = self._actions_for(table)
        rows = len(self.selected_ids(table))
        name = self._scene_name_from(table) if rows == 1 else f"{rows} scenes"
        # The label names the table as well as the scene: the tray serves tables
        # on both sides of it, and some sit behind an unselected tab, so the
        # highlighted row itself may not be visible.
        self.tray_label.setText(f"Selected: {name}  —  {title}")
        self.build_tray(self.shared_tray, table, actions, enabled=True)

    def _review_callbacks(self, table, scene_id):
        """Approve/Kick Back kwargs for the notes dialog when the selected row
        is one this user may act on. No review buttons by default; the
        dashboards override this for their own work queue."""
        return {}

    def act_open_roi(self, table):
        scene_id = self.selected_id(table)
        if scene_id is not None:
            self.handle_open_roi(scene_id)

    def act_open_notebook(self, table):
        scene_id = self.selected_id(table)
        if scene_id is not None:
            self.handle_open_notebook(scene_id)

    def act_open_folder(self, table):
        scene_id = self.selected_id(table)
        if scene_id is not None:
            self.handle_open_folder(scene_id)

    def act_notes(self, table):
        scene_id = self.selected_id(table)
        if scene_id is None:
            return
        self._show_notes(scene_id, self._scene_name_from(table),
                         **self._review_callbacks(table, scene_id))
        self._mark_viewed(scene_id)

    def act_science_notes(self, table):
        scene_id = self.selected_id(table)
        if scene_id is None:
            return
        self._show_science_notes(scene_id, self._scene_name_from(table))
        self._mark_viewed(scene_id)

    def act_flag(self, table):
        scene_id = self.selected_id(table)
        if scene_id is not None:
            self.handle_flag_scene(scene_id, self._scene_name_from(table))

    def act_summary_slide(self, table):
        scene_id = self.selected_id(table)
        if scene_id is not None:
            self.handle_open_summary_slide(scene_id)

    # ── Summary slides ──────────────────────────────────────────────────

    def generate_summary_slide(self, scene_id, force=False):
        """Build a scene's summary slide, skipping the work if the one on disk
        is already newer than everything it was built from."""
        try:
            scene = get_scene_by_id(self.conn, scene_id)
        except sqlite3.OperationalError:
            return None      # the workflow write already succeeded; not worth a second dialog
        if scene is None:
            return None
        try:
            if not force and slide_is_current(PANCAM_PATH, scene):
                return None
            build_summary_slide(PANCAM_PATH, scene)
        except FileNotFoundError as e:
            return f"'{scene['name']}': {e}"
        except (OSError, ValueError) as e:
            return f"'{scene['name']}': summary slide could not be written:\n{e}"
        return None

    def handle_open_summary_slide(self, scene_id):
        """Open a scene's summary slide in the system PDF viewer, rebuilding it
        first if it is missing or older than the images it summarizes."""
        problem = self.generate_summary_slide(scene_id)
        if problem:
            QMessageBox.warning(self, "Summary Slide Unavailable", problem)
            return
        try:
            scene = get_scene_by_id(self.conn, scene_id)
        except sqlite3.OperationalError:
            scene = None
        if scene is None:
            return
        folder = find_scene_folder(PANCAM_PATH, scene)
        if not folder:
            QMessageBox.warning(self, "Summary Slide Unavailable",
                                f"No saved ROI Studio folder for '{scene['name']}'.")
            return
        slide, _master = summary_slide_paths(PANCAM_PATH, scene, folder)
        QDesktopServices.openUrl(QUrl.fromLocalFile(slide))

    def _mark_viewed(self, scene_id):
        """Clear the new-activity highlight after notes have been read, from
        whichever table they were opened from."""
        set_scene_viewed_at(scene_id)
        self.refresh_task_list()

    def _show_notes(self, scene_id, scene_name, on_approve=None, on_kick_back=None,
                    on_submit=None):
        NotesDialog(
            self.conn, scene_id, scene_name, self.user['id'], self,
            is_supervisor=(self.user['role'] == Role.SUPERVISOR),
            on_approve=on_approve, on_kick_back=on_kick_back, on_submit=on_submit,
            on_open_roi=(lambda: self.handle_open_roi(scene_id)),
            on_summary_slide=(lambda: self.handle_open_summary_slide(scene_id)),
        ).exec()

    def _show_science_notes(self, scene_id, scene_name):
        NotesDialog(
            self.conn, scene_id, scene_name, self.user['id'], self,
            title="Science Notes", get_thread=get_science_notes,
            format_row=_format_science_note_row, add_note_fn=add_science_note,
            update_note_fn=update_science_note, delete_note_fn=delete_science_note,
            is_supervisor=(self.user['role'] == Role.SUPERVISOR),
            on_open_roi=(lambda: self.handle_open_roi(scene_id)),
        ).exec()

    def handle_flag_scene(self, scene_id, scene_name=""):
        """Open FlagDialog, persist flag changes, and always save an auto-note."""
        dialog = FlagDialog(self.conn, scene_id, scene_name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_flags = dialog.get_flags()
        old_flags = dialog.old_flags
        added   = new_flags - old_flags
        removed = old_flags - new_flags
        parts = []
        if added:
            parts.append("Added: "   + ", ".join(SceneFlag.LABELS[f] for f in sorted(added)))
        if removed:
            parts.append("Removed: " + ", ".join(SceneFlag.LABELS[f] for f in sorted(removed)))
        if not parts:
            parts.append("No changes")
        note_body = "Flags updated — " + "; ".join(parts)
        user_text = dialog.get_note_text()
        if user_text:
            note_body += f"\n{user_text}"

        def _save():
            update_scene_flags(self.conn, scene_id, SceneFlag.serialize(new_flags))
            add_note(self.conn, scene_id, self.user['id'], note_body)
        self._run_db_action(_save, "Flag Update Failed")
        self.refresh_task_list()

    def _show_menu(self, btn):
        menu = QMenu(self)

        act_stats = QAction("See Stats", self)
        act_stats.triggered.connect(self._handle_see_stats)
        menu.addAction(act_stats)

        menu.addSeparator()

        act_user = QAction("Change Username", self)
        act_user.triggered.connect(self._handle_change_username)
        menu.addAction(act_user)

        act_pass = QAction("Change Password", self)
        act_pass.triggered.connect(self._handle_change_password)
        menu.addAction(act_pass)

        menu.addSeparator()

        act_roi_path = QAction("Reset ROI Studio Path", self)
        act_roi_path.triggered.connect(self._prompt_for_roi_studio_path)
        menu.addAction(act_roi_path)

        menu.addSeparator()

        act_dark = QAction("Dark Mode", self)
        act_dark.setCheckable(True)
        act_dark.setChecked(get_dark_mode())
        act_dark.toggled.connect(self._toggle_dark_mode)
        menu.addAction(act_dark)

        act_confetti = QAction("Confetti", self)
        act_confetti.setCheckable(True)
        act_confetti.setChecked(get_confetti())
        act_confetti.toggled.connect(set_confetti)
        menu.addAction(act_confetti)

        scale_menu = menu.addMenu("UI Scale (restart required)")
        assert scale_menu is not None
        current_scale = get_ui_scale()
        scale_group = QActionGroup(scale_menu)
        scale_group.setExclusive(True)
        for scale in UI_SCALE_PRESETS:
            act_scale = QAction(f"{int(scale * 100)}%", scale_menu)
            act_scale.setCheckable(True)
            act_scale.setChecked(abs(scale - current_scale) < 0.001)
            act_scale.triggered.connect(lambda checked, s=scale: self._set_ui_scale(s))
            scale_group.addAction(act_scale)
            scale_menu.addAction(act_scale)

        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _handle_see_stats(self):
        # StatsDialog reads the whole stats set in its constructor, so the
        # guard has to wrap construction, not exec().
        dialog = self._run_db_read(lambda: StatsDialog(self.conn, self.user, self))
        if dialog is not None:
            dialog.exec()

    def _toggle_dark_mode(self, enabled):
        set_dark_mode(enabled)
        apply_theme(enabled)

    def _set_ui_scale(self, scale):
        set_ui_scale(scale)
        QMessageBox.information(
            self, "Restart Required",
            "Quit and reopen ROVR for the new UI scale to take effect."
        )

    def _handle_change_username(self):
        dlg = ChangeUsernameDialog(self.conn, self.user, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.user = dict(self.user)
            self.user['username'] = dlg.new_username()
            self._username_label.setText(self.user['username'])

    def _handle_change_password(self):
        ChangePasswordDialog(self.conn, self.user, self).exec()

    def refresh_task_list(self, celebrate_empty=True):
        """Repopulate every table on this dashboard.

        Guarded rather than left to each subclass: this runs automatically
        after every action, so a locked database here would take the window
        down for something the user never asked for. Subclasses implement
        _refresh_tables() and get the handling for free. Clearing My Work 
        Queue earns confetti"""
        
        before = self.my_queue_table.rowCount()
        self._run_db_read(self._refresh_tables)
        if (celebrate_empty and before and not self.my_queue_table.rowCount()
                and get_confetti()):
            ConfettiOverlay(self)

    def _refresh_tables(self):
        raise NotImplementedError

    def handle_logout(self):
        from app.ui.login import LoginUI
        self.login = LoginUI(self.conn)
        self.login.show()
        self.close()
