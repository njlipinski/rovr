#!/usr/bin/env python3
"""Scan R:\\Rice\\Pancam\\MERA\\iof\\sol#### directories for Pancam IOF .IMG files,
group them by (sol, seqID) to identify scenes, and import new ones as
unclaimed scenes (status 0).

Scans and inserts sol-by-sol so progress is saved incrementally — a crash
loses at most one sol's worth of work. Re-running safely resumes from where
it left off (already-imported scenes are skipped).

Left and right eye images with the same sol and seqID are part of the same scene.
Owner and roi_filename are not set at import time — owner is assigned when
Analyst 1 claims the scene; roi_filename is set when the .sel file is saved.

Scenes are currently imported from MERA only. MERB support is planned.

Usage (run from repo root):
    python setup/import_scenes.py
    python setup/import_scenes.py --path "R:\\\\Rice\\\\Pancam"
    python setup/import_scenes.py --dry-run
"""

import re
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_db_connection, initialize_db

try:
    from config import PANCAM_PATH
except ImportError:
    PANCAM_PATH = None

# sol#### folder pattern
_SOL_RE = re.compile(r'^sol(\d{4})$', re.IGNORECASE)

# MER Pancam filename stem is exactly 27 chars (plus 3-char extension):
# [scid(1)][inst(1)][sclk(9)][prod(3)][site(4)][seq(5)][eye(1)][filt(1)][who(1)][ver(1)]
# The seq field (chars 17-21) is always 'P####' — the 4 digits are the seqID.
_STEM_LEN = 27


def _sol_num(dirname):
    m = _SOL_RE.match(dirname)
    return int(m.group(1)) if m else None


def _parse_img(filename):
    """Return seqID digits (e.g. '2210') if this is an IOF Pancam .IMG, else None.

    Filters:
    - extension must be .img (case-insensitive)
    - 'iof' must appear in the filename (case-insensitive) — skips IOT thumbnails
    - filename stem must be exactly 25 chars with 'P####' at positions 18-22
    """
    name_lower = filename.lower()
    if not name_lower.endswith('.img'):
        return None
    if 'iof' not in name_lower:
        return None
    dot = filename.rfind('.')
    stem = filename[:dot]
    if len(stem) != _STEM_LEN:
        return None
    seq_field = stem[18:23].upper()   # e.g. 'P2303'
    if not seq_field.startswith('P') or not seq_field[1:].isdigit():
        return None
    return seq_field[1:]  # just the 4 digits


def _scan_sol(sol_dir, rover):
    """Scan one sol directory and return a dict of scene_key -> scene dict."""
    sol = _sol_num(sol_dir.name)
    scenes = {}
    for f in sol_dir.iterdir():
        if not f.is_file():
            continue
        seq = _parse_img(f.name)
        if seq is None:
            continue
        key = f"{rover}/sol{sol:04d}/seqID{seq}"
        if key not in scenes:
            scenes[key] = {
                'name': f"{rover}sol{sol:04d}seqID{seq}",
                'scene_key': key,
                'sol': sol,
                'image_count': 0,
            }
        scenes[key]['image_count'] += 1
    return scenes


def import_scenes(conn, pancam_root, dry_run=False):
    rover = 'MERA'
    iof_root = Path(pancam_root) / rover / 'iof'

    if not iof_root.exists():
        print(f"Error: {iof_root} does not exist.")
        return

    # Collect and sort sol directories upfront (fast — just a listing)
    sol_dirs = sorted(
        (d for d in iof_root.iterdir() if d.is_dir() and _sol_num(d.name) is not None),
        key=lambda d: _sol_num(d.name),
    )
    total_sols = len(sol_dirs)
    if total_sols == 0:
        print("No sol directories found.")
        return

    # Load already-imported scene keys so we can skip them
    existing = {row[0] for row in conn.execute("SELECT scene_key FROM scenes").fetchall()}

    print(f"Found {total_sols} sol directories in {iof_root}")
    print(f"{len(existing)} scene(s) already in database — will be skipped\n")

    total_added = total_skipped = total_warnings = 0

    for i, sol_dir in enumerate(sol_dirs, 1):
        sol = _sol_num(sol_dir.name)
        pct = i / total_sols * 100
        prefix = f"[{i}/{total_sols}  {pct:5.1f}%]  sol{sol:04d}"

        scenes = _scan_sol(sol_dir, rover)

        if not scenes:
            print(f"{prefix}  (no IOF scenes)")
            continue

        # Check for image count anomalies
        warnings = [s for s in scenes.values() if s['image_count'] != 13]
        new_scenes = [s for s in scenes.values() if s['scene_key'] not in existing]
        skip_count = len(scenes) - len(new_scenes)

        # Insert new scenes for this sol and commit immediately
        if not dry_run:
            for s in new_scenes:
                conn.execute(
                    "INSERT INTO scenes (name, scene_key, status) VALUES (?, ?, 0)",
                    (s['name'], s['scene_key']),
                )
            if new_scenes:
                conn.commit()
                existing.update(s['scene_key'] for s in new_scenes)

        # Build status line
        parts = []
        if new_scenes:
            parts.append(f"+{len(new_scenes)} added")
        if skip_count:
            parts.append(f"{skip_count} skipped")
        if warnings:
            parts.append(f"{len(warnings)} image-count warning(s)")
            total_warnings += len(warnings)

        label = "  [dry run]" if dry_run else ""
        print(f"{prefix}{label}  {', '.join(parts) if parts else 'nothing new'}")

        for w in warnings:
            print(f"    Note: {w['name']} has {w['image_count']} images (expected ~13)")

        total_added += len(new_scenes)
        total_skipped += skip_count

    print(f"\nDone. {total_added} scene(s) imported, {total_skipped} already existed", end="")
    if total_warnings:
        print(f", {total_warnings} image-count warning(s)")
    else:
        print()


def wipe_scenes(conn):
    """Delete all rows from scenes and reviews, leaving users intact."""
    scene_count = conn.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
    review_count = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    conn.execute("DELETE FROM reviews")
    conn.execute("DELETE FROM scenes")
    conn.commit()
    print(f"Wiped {scene_count} scene(s) and {review_count} review(s). Users untouched.")


def main():
    parser = argparse.ArgumentParser(
        prog="import_scenes",
        description="Import Pancam scenes from the Rice drive into the ROVR database.",
    )
    parser.add_argument(
        "--path",
        default=PANCAM_PATH,
        help="Root Pancam folder containing MERA/ and MERB/ subdirectories "
             "(defaults to PANCAM_PATH in config.py)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without writing to the database",
    )
    parser.add_argument(
        "--reimport",
        action="store_true",
        help="Wipe all scenes and reviews, then re-import from scratch. Users are not affected.",
    )
    args = parser.parse_args()

    if not args.path:
        print("Error: no path specified and PANCAM_PATH is not set in config.py.")
        print("Usage: python setup/import_scenes.py --path <path to Pancam root>")
        sys.exit(1)

    if not Path(args.path).exists():
        print(f"Error: '{args.path}' does not exist.")
        sys.exit(1)

    initialize_db()
    conn = get_db_connection()
    try:
        if args.reimport:
            print("WARNING: --reimport will delete all scenes and reviews from the database.")
            print("Users will not be affected. This cannot be undone.")
            confirm = input("Type YES to continue: ").strip()
            if confirm != "YES":
                print("Cancelled.")
                sys.exit(0)
            wipe_scenes(conn)
            print()
        import_scenes(conn, args.path, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
