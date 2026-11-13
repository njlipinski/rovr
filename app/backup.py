"""Backup of the database and working folders into R:\\Rice\\Backup.

Shared by `setup/util.py backup` and the supervisor dashboard's automatic
weekly run.
"""
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from app.models import Rover
from app.paths import FolderKind

try:
    from config import BACKUP_INTERVAL_DAYS
except ImportError:
    BACKUP_INTERVAL_DAYS = 7  # config.py predates this setting

_SOL_RE = re.compile(r'^\d{4}$')


def backup_root(pancam_root):
    return Path(pancam_root).parent / "Backup"


def last_backup_time(pancam_root):
    """Epoch mtime of the newest DB backup, 0.0 if there are none yet, or
    None if this machine can't reach the backup folder."""
    root = backup_root(pancam_root)
    try:
        if not root.is_dir() or not os.access(root, os.W_OK):
            return None
        db_dir = root / "db"
        if not db_dir.is_dir():
            return 0.0
        return max((f.stat().st_mtime for f in db_dir.iterdir() if f.is_file()),
                   default=0.0)
    except OSError:
        return None


def backup_scenes(pancam_root, db_path, log=print):
    """Back up the database and both rovers' 'working' directories
    into R:\\Rice\\Backup. Returns True on success.

    The DB is snapshotted through SQLite's online backup API (safe against
    concurrent writers on the shared drive) into a timestamped file, so
    every run keeps its own dated copy rather than overwriting the last one.
    DB_PATH sits on the same shared network drive as the live app -- the
    backup API's page-by-page read can intermittently surface as a generic
    SQLite "disk I/O error" rather than a clean lock error on a flaky
    network filesystem, especially while another user's ROVR instance has
    the file open. The whole backup is retried a few times before giving up,
    same spirit as db.py's _with_lock_retry for ordinary writes.

    Working-directory files (.sel/.fits) are mirrored into a single
    persistent backup/working tree, preserving the rover/####/working
    layout. Existing files there are never touched or re-copied -- .sel and
    .fits files don't change once written, so only new files since the last
    run are copied.
    """
    pancam_path = Path(pancam_root)
    root = backup_root(pancam_path)
    db_backup_dir = root / "db"
    db_backup_dir.mkdir(parents=True, exist_ok=True)

    db_path = Path(db_path)
    timestamp = datetime.now().strftime("%Y_%m_%d")
    db_backup_path = db_backup_dir / f"{db_path.stem}_{timestamp}{db_path.suffix}"

    retries = 5
    last_error = None
    for attempt in range(retries):
        if db_backup_path.exists():
            db_backup_path.unlink()  # drop any partial file from a failed attempt
        try:
            src_conn = sqlite3.connect(db_path, timeout=1.0)
            try:
                dst_conn = sqlite3.connect(db_backup_path)
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
            last_error = None
            break
        except sqlite3.OperationalError as e:
            last_error = e
        if attempt < retries - 1:
            time.sleep(1.0)

    if last_error is not None:
        if db_backup_path.exists():
            db_backup_path.unlink()
        log(f"Error: database backup failed after {retries} attempts: {last_error}")
        log("The network drive may be busy or briefly unreachable -- try again in a moment.")
        return False

    log(f"Database backed up to {db_backup_path}")

    copied = skipped = 0
    for rover in Rover.ALL:
        rover_root = pancam_path / rover
        if not rover_root.exists():
            log(f"Skipping {rover}: {rover_root} does not exist.")
            continue

        sol_dirs = sorted(
            d for d in rover_root.iterdir() if d.is_dir() and _SOL_RE.match(d.name)
        )
        log(f"{rover}: {len(sol_dirs)} sol directories")
        for sol_dir in sol_dirs:
            working_dir = sol_dir / FolderKind.WORKING
            if not working_dir.exists():
                continue
            sol_copied = 0
            for item in working_dir.rglob("*"):
                if item.is_dir():
                    continue
                dest = root / item.relative_to(pancam_path)
                if dest.exists():
                    skipped += 1
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Interrupted copies stay .part, so the exists() skip never keeps a truncated file
                part = dest.with_name(dest.name + ".part")
                shutil.copy2(item, part)
                os.replace(part, dest)
                copied += 1
                sol_copied += 1
            # Only sols with new files report, so a re-run stays quiet
            if sol_copied:
                rel = working_dir.relative_to(pancam_path)
                log(f"  {rel}: {sol_copied} copied")

    log(f"Working files: {copied} copied, {skipped} already backed up (skipped)")
    return True
