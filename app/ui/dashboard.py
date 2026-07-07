"""base dashboard — shared layout, widgets, and utilities for all dashboard types"""
import os
import re
import subprocess
import sys
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter, QMenu, QGroupBox,
    QDialog, QTextEdit, QDialogButtonBox, QMessageBox, QFileDialog, QLineEdit,
    QCheckBox, QStyledItemDelegate, QStyle, QListWidget, QListWidgetItem, QInputDialog,
    QApplication,
)
from PyQt6.QtCore import Qt, QSize, QUrl
from PyQt6.QtGui import QPainter, QColor, QAction, QActionGroup, QTextCursor, QDesktopServices
from app.models import SceneStatus, SceneFlag, Role
from app.paths import FolderKind, kind_path
from app.local_settings import (
    get_roi_studio_path, set_roi_studio_path,
    get_column_widths, set_column_widths,
    get_dark_mode, set_dark_mode,
    get_ui_scale, set_ui_scale,
)
from app.ui.styles import DARK_STYLESHEET, TRAY_HEIGHT, MIN_COL_WIDTH, apply_theme

# Preset options shown in the ☰ → UI Scale menu. Applied via QT_SCALE_FACTOR
# at next launch (Qt reads it once, at startup) — not live.
UI_SCALE_PRESETS = (1.0, 1.25, 1.5, 1.75, 2.0)
from app.db import (
    get_scene_thread, add_note, update_note, delete_note, get_scene_by_id,
    get_science_notes, add_science_note, update_science_note, delete_science_note,
    update_username, update_user_password, get_user_by_username,
    update_scene_flags, get_user_stats, get_all_user_stats,
)
from app.auth import verify_password, hash_password
from config import PANCAM_PATH
try:
    from app.version import __version__
except ImportError:
    __version__ = "dev"

# Parses 'MERB/sol0003/P2350/obs0' → ('MERB', '0003', 'P2350', '0')
_KEY_RE = re.compile(r'^(MER[AB])/sol(\d{4})/([^/]+)/obs(\d+)$')



def parse_scene_key(scene_key):
    """Return (rover, sol, seq_id, obs) from a scene_key, or ('','',scene_key,'0') if unparseable."""
    m = _KEY_RE.match(scene_key)
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(4)
    return '', '', scene_key, '0'


class WordSelectTextEdit(QTextEdit):
    """QTextEdit where double-click selects a whole word, treating apostrophes
    as part of the word (so "don't" or "can't" select as one unit instead of
    stopping at the punctuation, which is Qt's default)."""

    _WORD_RE = re.compile(r"[\w']+", re.UNICODE)

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
            # No right neighbor — revert
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


def make_section(label_text, table, tray):
    """Wrap a label + table + button tray into a QSplitter-compatible widget."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 4, 0, 4)

    header_label = QLabel()

    def _update_count():
        n = table.rowCount()
        header_label.setText(f"{label_text} — {n} item{'s' if n != 1 else ''}")

    _update_count()

    model = table.model()
    assert model is not None
    model.rowsInserted.connect(lambda *_: _update_count())
    model.rowsRemoved.connect(lambda *_: _update_count())

    layout.addWidget(header_label)
    layout.addWidget(table)
    layout.addWidget(tray)
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


class FlagDialog(QDialog):
    """Check/uncheck scene flags and optionally add a note. Always saves a note on OK."""
    def __init__(self, conn, scene_id, scene_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Flags — {scene_name}")
        self.setMinimumWidth(360)
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

        layout.addWidget(QLabel("Note:"))
        self._note = WordSelectTextEdit()
        self._note.setFixedHeight(72)
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


class KickBackDialog(QDialog):
    """Shows scene history and optionally collects a kick-back note. Note is not required."""
    def __init__(self, conn, scene_id, scene_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Kick Back — {scene_name}")
        self.setMinimumSize(520, 480)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Scene history:"))
        self.display = WordSelectTextEdit()
        self.display.setReadOnly(True)
        layout.addWidget(self.display)

        layout.addWidget(QLabel("Add a note (optional):"))
        self.input = WordSelectTextEdit()
        self.input.setFixedHeight(80)
        self.input.setPlaceholderText("Describe what needs to be revised...")
        layout.addWidget(self.input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        thread = get_scene_thread(conn, scene_id)
        self.display.setPlainText(_format_thread(thread))

    def get_comments(self):
        text = self.input.toPlainText().strip()
        return text if text else None


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


def _format_thread_row(row):
    """Format one row from get_scene_thread (a note or a review entry)."""
    tag = 'Note' if row['type'] == 'note' else _DECISION_LABEL.get(row['decision'], row['decision'])
    header = f"[{row['timestamp']}]  {row['author_name']}  ({tag})"
    content = row['content']
    return f"{header}\n{content}" if content else header


def _format_thread(thread):
    """Format a scene thread (rows from get_scene_thread) into a plain-text string."""
    if not thread:
        return "No history yet."
    return "\n\n---\n\n".join(_format_thread_row(row) for row in thread)


def _format_science_note_row(row):
    """Format one row from get_science_notes."""
    header = f"[{row['timestamp']}]  {row['author_name']}"
    content = row['content']
    return f"{header}\n{content}" if content else header


def _format_science_notes_for_roi_studio(thread):
    """Concatenate a science notes thread (rows from get_science_notes) into the
    single metadata string ROI Studio's --notes argument expects."""
    return "\n\n---\n\n".join(_format_science_note_row(row) for row in thread)


class NotesDialog(QDialog):
    """Read-write dialog showing a note thread for a scene, allowing new notes,
    and letting the author (or a supervisor) edit/delete past notes.

    Parameterized by get_thread/format_row/add_note_fn/update_note_fn/delete_note_fn
    so the same dialog backs both the regular Notes thread (manual notes + review
    housekeeping, via get_scene_thread/add_note/update_note/delete_note) and the
    Science Notes thread (manual, scientifically-relevant notes only, via
    get_science_notes/add_science_note/update_science_note/delete_science_note).
    Review/decision entries are never editable — reviews are an append-only
    audit log — so edit/delete are only enabled for rows where type == 'note'.
    """
    def __init__(self, conn, scene_id, scene_name, author_id, parent=None,
                 title="Notes", get_thread=get_scene_thread,
                 format_row=_format_thread_row, add_note_fn=add_note,
                 update_note_fn=update_note, delete_note_fn=delete_note,
                 is_supervisor=False):
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
        self.setWindowTitle(f"{title} — {scene_name}")
        self.setMinimumSize(520, 440)
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

        layout.addWidget(QLabel("Add a note:"))
        self.input = WordSelectTextEdit()
        self.input.setFixedHeight(80)
        self.input.setPlaceholderText("Type a note...")
        layout.addWidget(self.input)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        add_btn = QPushButton("Add Note")
        add_btn.clicked.connect(self._on_add)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(close_btn)
        layout.addWidget(btn_row)

        self._refresh()

    def _can_edit(self, row):
        return row['type'] == 'note' and (self._is_supervisor or row['author_id'] == self.author_id)

    def _refresh(self):
        self.list.clear()
        thread = self._get_thread(self.conn, self.scene_id)
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
        self._update_note_fn(self.conn, row['id'], new_body)
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
        self._delete_note_fn(self.conn, row['id'])
        self._refresh()

    def _on_add(self):
        body = self.input.toPlainText().strip()
        if not body:
            QMessageBox.warning(self, "Empty Note", "Please enter some text before adding a note.")
            return
        self._add_note_fn(self.conn, self.scene_id, self.author_id, body)
        self.input.clear()
        self._refresh()


class ChangeUsernameDialog(QDialog):
    """Let the logged-in user pick a new username."""
    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self._conn = conn
        self._user = user
        self.setWindowTitle("Change Username")
        self.setMinimumWidth(340)
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
        update_username(self._conn, self._user['id'], new_name)
        self.accept()

    def new_username(self):
        return self._field.text().strip()


class ChangePasswordDialog(QDialog):
    """Let the logged-in user set a new password after confirming their current one."""
    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self._conn = conn
        self._user = user
        self.setWindowTitle("Change Password")
        self.setMinimumWidth(340)
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
        if len(new_pw) < 6:
            QMessageBox.warning(self, "Too Short", "New password must be at least 6 characters.")
            return
        if new_pw != self._confirm.text():
            QMessageBox.warning(self, "Mismatch", "New passwords do not match.")
            return
        update_user_password(self._conn, self._user['id'], hash_password(new_pw))
        self.accept()


class FilterDialog(QDialog):
    """Filter scenes by rover, sol range, and sequence ID range across all tables."""

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
    """QTableWidgetItem that sorts by integer value instead of string."""
    def __lt__(self, other):
        try:
            return int(self.text()) < int(other.text())
        except ValueError:
            return super().__lt__(other)


class StatsDialog(QDialog):
    """Show productivity stats. Analysts see their own; supervisors see all users."""

    _ROWS = [
        ("Scenes submitted",       'submitted_total',    'submitted_today'),
        ("Peer reviews completed", 'peer_reviewed_total','peer_reviewed_today'),
        ("My scenes approved",     'approved_total',     'approved_today'),
        ("Kicked back to me",      'kicked_back_total',  'kicked_back_today'),
    ]

    def __init__(self, conn, user, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Statistics")
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)

        if user['role'] == Role.SUPERVISOR:
            self._build_all_users(layout, conn)
        else:
            self._build_own(layout, conn, user['id'])

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

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
            today = QTableWidgetItem(str(stats[today_key]))
            today.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tbl.setItem(i, 2, today)

        tbl.setSortingEnabled(True)

        tbl.resizeRowsToContents()
        self.setMinimumWidth(380)
        layout.addWidget(tbl)

    def _build_all_users(self, layout, conn):
        all_stats = [
            (u, s) for u, s in get_all_user_stats(conn)
            if u['role'] != Role.SUPERVISOR and 'test' not in u['username'].lower()
        ]
        layout.addWidget(QLabel("<b>All User Statistics</b>"))

        headers = [
            "Username",
            "Submitted", "Sub Today",
            "Peer Rev'd", "PR Today",
            "Approved",   "Appr Today",
            "Kicked Back","KB Today",
        ]
        tbl = QTableWidget(len(all_stats), len(headers))
        tbl.setHorizontalHeaderLabels(headers)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tbl.horizontalHeader().setStretchLastSection(False)

        R = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

        def num(val):
            item = _NumericItem(str(val))
            item.setTextAlignment(R)
            return item

        for i, (user, stats) in enumerate(all_stats):
            tbl.setItem(i, 0, QTableWidgetItem(user['username']))
            tbl.setItem(i, 1, num(stats['submitted_total']))
            tbl.setItem(i, 2, num(stats['submitted_today']))
            tbl.setItem(i, 3, num(stats['peer_reviewed_total']))
            tbl.setItem(i, 4, num(stats['peer_reviewed_today']))
            tbl.setItem(i, 5, num(stats['approved_total']))
            tbl.setItem(i, 6, num(stats['approved_today']))
            tbl.setItem(i, 7, num(stats['kicked_back_total']))
            tbl.setItem(i, 8, num(stats['kicked_back_today']))

        tbl.setSortingEnabled(True)

        tbl.resizeColumnsToContents()
        tbl.resizeRowsToContents()
        self.setMinimumWidth(680)
        layout.addWidget(tbl)

        chart_btn = QPushButton("View Chart")
        chart_btn.clicked.connect(lambda: StatsChartDialog(all_stats, self).exec())
        layout.addWidget(chart_btn)


class StatsChartDialog(QDialog):
    """Bar chart of analyst activity stats with an All Time / Today toggle."""

    _METRICS = [
        ("Submitted",    'submitted_total',    'submitted_today'),
        ("Peer Rev'd",   'peer_reviewed_total','peer_reviewed_today'),
        ("Approved",     'approved_total',     'approved_today'),
        ("Kicked Back",  'kicked_back_total',  'kicked_back_today'),
    ]

    def __init__(self, all_stats, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Analyst Activity Chart")
        self.setSizeGripEnabled(True)
        self.resize(820, 480)
        self._all_stats = all_stats

        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        import matplotlib.pyplot as plt
        self._plt = plt

        self._fig, self._ax = plt.subplots()
        self._canvas = FigureCanvasQTAgg(self._fig)

        toggle_row = QHBoxLayout()
        self._btn_total = QPushButton("All Time")
        self._btn_today = QPushButton("Today")
        self._btn_total.setCheckable(True)
        self._btn_today.setCheckable(True)
        self._btn_total.setChecked(True)
        self._btn_total.clicked.connect(lambda: self._switch(False))
        self._btn_today.clicked.connect(lambda: self._switch(True))
        toggle_row.addWidget(self._btn_total)
        toggle_row.addWidget(self._btn_today)
        toggle_row.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(toggle_row)
        layout.addWidget(self._canvas, stretch=1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._today = False
        self._draw()

    def _switch(self, today):
        self._today = today
        self._btn_total.setChecked(not today)
        self._btn_today.setChecked(today)
        self._draw()

    def _metrics_keyed(self):
        """Return [(display_label, stats_dict_key)] for the current toggle mode."""
        i = 2 if self._today else 1
        return [(m[0], m[i]) for m in self._METRICS]

    def _draw(self):
        import numpy as np

        ax = self._ax
        ax.clear()

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
            ax.bar_label(bars, padding=2, fontsize=8)

        ax.set_xticks(x)
        ax.set_xticklabels(usernames, rotation=20, ha='right')
        ax.set_ylabel("Count")
        ax.set_title("Today's Activity" if self._today else "All-Time Activity")
        ax.legend(loc='upper right')
        ax.margins(y=0.15)

        dark = get_dark_mode()
        bg = '#2b2b2b' if dark else '#ffffff'
        fg = '#dddddd' if dark else '#000000'
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
    def __init__(self, conn, user):
        super().__init__()
        self.conn = conn
        self.user = user
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

    # ── Shared helpers available to all subclasses ──────────────────────

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
            self.refresh_task_list()

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

    def _prompt_for_roi_studio_path(self):
        """Ask the user to locate ROI Studio and save the path. Returns the path, or None if cancelled."""
        if sys.platform == 'darwin':
            file_filter = "Applications (*.app);;All Files (*)"
        elif sys.platform == 'win32':
            file_filter = "Executables (*.exe)"
        else:
            file_filter = "All Files (*)"
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate ROI Studio", "", file_filter
        )
        if not path:
            return None
        set_roi_studio_path(path)
        return path

    def handle_open_roi(self, scene_id):
        """Launch ROI Studio and open the given scene."""
        path = get_roi_studio_path()
        if not path or not os.path.exists(path):
            path = self._prompt_for_roi_studio_path()
            if not path:
                return

        args = [path]
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
            sel = self._find_sel_file(scene)
            if sel:
                args.append(sel)
            fits = self._find_fits_file(scene)  # noqa: F841 — not yet sent, see TODO below
            # TODO: current ROI Studio build doesn't accept a FITS path yet. Once it
            # does, send it here (exact position/flag TBD — update alongside --notes):
            # if fits:
            #     args.append(fits)
            notes_thread = get_science_notes(self.conn, scene_id)
            if notes_thread:
                args += ['--notes', _format_science_notes_for_roi_studio(notes_thread)]

        try:
            if sys.platform == 'darwin':
                # .app bundles are directories — must launch via `open -a` like Finder does
                subprocess.Popen(['open', '-a', path, '--args'] + args[1:])
            else:
                subprocess.Popen(args)
        except OSError as e:
            QMessageBox.warning(self, "Launch Failed", f"Could not open ROI Studio:\n{e}")

    def handle_open_notebook(self, scene_id):
        """Open the MER Analyst's Notebook to this scene's Sol Summary, and copy
        the sol number to the clipboard. The Notebook's own left-hand Sol
        navigator can't be set via URL — it's driven by a stateful ASP.NET
        postback, not a link — so pasting the sol there is the fastest way to
        reach its other panels (Data Products, Mosaics, Targets, etc.)."""
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

    def _find_scene_file(self, scene, ext):
        """Return the path to the most recent file with the given extension (e.g.
        '.sel', '.fits') for this scene under working/, or None."""
        rover  = scene['rover']
        sol    = scene['sol']
        seq_id = scene['seq_id']
        pma    = scene['pma']
        if None in (rover, sol, seq_id, pma):
            return None

        base_name = f"Sol{sol:04d}_{seq_id.lower()}_PMA{pma}"
        sol_dir   = kind_path(PANCAM_PATH, rover, sol, FolderKind.WORKING)
        if not os.path.isdir(sol_dir):
            return None

        candidates = []
        for entry in os.scandir(sol_dir):
            if not entry.is_dir():
                continue
            n = entry.name
            is_base      = n == base_name
            is_versioned = n.startswith(base_name + '_v') and n[len(base_name) + 2:].isdigit()
            if not (is_base or is_versioned):
                continue
            file_path = os.path.join(entry.path, base_name + ext)
            if os.path.isfile(file_path):
                candidates.append(file_path)

        if not candidates:
            return None
        return max(candidates, key=os.path.getmtime)

    def _find_sel_file(self, scene):
        """Return the path to the most recent .sel file for this scene under working/, or None."""
        return self._find_scene_file(scene, '.sel')

    def _find_fits_file(self, scene):
        """Return the path to the most recent .fits file for this scene under working/, or None."""
        return self._find_scene_file(scene, '.fits')

    def _show_notes(self, scene_id, scene_name):
        NotesDialog(
            self.conn, scene_id, scene_name, self.user['id'], self,
            is_supervisor=(self.user['role'] == Role.SUPERVISOR),
        ).exec()

    def _show_science_notes(self, scene_id, scene_name):
        NotesDialog(
            self.conn, scene_id, scene_name, self.user['id'], self,
            title="Science Notes", get_thread=get_science_notes,
            format_row=_format_science_note_row, add_note_fn=add_science_note,
            update_note_fn=update_science_note, delete_note_fn=delete_science_note,
            is_supervisor=(self.user['role'] == Role.SUPERVISOR),
        ).exec()

    def handle_flag_scene(self, scene_id, scene_name=""):
        """Open FlagDialog, persist flag changes, and always save an auto-note."""
        dialog = FlagDialog(self.conn, scene_id, scene_name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_flags = dialog.get_flags()
        old_flags = dialog.old_flags
        update_scene_flags(self.conn, scene_id, SceneFlag.serialize(new_flags))
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
        add_note(self.conn, scene_id, self.user['id'], note_body)
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
        StatsDialog(self.conn, self.user, self).exec()

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

    def refresh_task_list(self):
        raise NotImplementedError

    def handle_logout(self):
        from app.ui.login import LoginUI
        self.login = LoginUI(self.conn)
        self.login.show()
        self.close()
