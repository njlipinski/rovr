#!/usr/bin/env python3
"""Scan R:\\Rice\\Pancam\\MERA\\iof\\sol#### directories for Pancam IOF .IMG files,
group them by (sol, seqID) to identify scenes, and import new ones as
unclaimed scenes (status 0).

Left and right eye images with the same sol and seqID are part of the same scene.
Owner and roi_filename are not set at import time — owner is assigned when
Analyst 1 claims the scene; roi_filename is set when the .sel file is saved.

Scenes are currently imported from MERA only for testing purposes.
TODO: import MERB

Usage (run from repo root):
    python setup/import_scenes.py
    python setup/import_scenes.py --path "R:\\Rice\\Pancam"
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

# MER Pancam filename stem is exactly 25 chars (plus 3-char extension):
# [scid(1)][inst(1)][sclk(9)][prod(3)][site(2)][pos(2)][seq(5)][eye(1)][filt(1)][who(1)][ver(1)]
# The seq field (chars 18-22) is always 'P####' — the 4 digits are the seqID.
_STEM_LEN = 25


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
    seq_field = stem[18:23].upper()   # e.g. 'P2210'
    if not seq_field.startswith('P') or not seq_field[1:].isdigit():
        return None
    return seq_field[1:]  # just the 4 digits


def find_scenes(pancam_root):
    """Walk MERA/iof/sol#### and return sorted list of unique scenes.

    Each scene dict has: name, scene_key, sol, image_count.

    scene_key format: MERA/sol####/seqID#### (e.g. MERA/sol0042/seqID2210)
    name format:      sol####seqID#### (e.g. sol0042seqID2210)
    """
    rover = 'MERA'
    iof_root = Path(pancam_root) / rover / 'iof'

    if not iof_root.exists():
        print(f"  Warning: {iof_root} does not exist, skipping {rover}.")
        return []

    seen = {}
    sol_dirs = sorted(
        (d for d in iof_root.iterdir() if d.is_dir() and _sol_num(d.name) is not None),
        key=lambda d: _sol_num(d.name),
    )

    for sol_dir in sol_dirs:
        sol = _sol_num(sol_dir.name)
        for f in sol_dir.iterdir():
            if not f.is_file():
                continue
            seq = _parse_img(f.name)
            if seq is None:
                continue
            key = f"{rover}/sol{sol:04d}/seqID{seq}"
            if key not in seen:
                seen[key] = {
                    'name': f"sol{sol:04d}seqID{seq}",
                    'scene_key': key,
                    'sol': sol,
                    'image_count': 0,
                }
            seen[key]['image_count'] += 1

    scenes = sorted(seen.values(), key=lambda s: (s['sol'], s['scene_key']))

    # Sanity check: warn on scenes with unexpected image counts (~13 expected)
    for s in scenes:
        if s['image_count'] != 13:
            print(f"  Note: {s['name']} has {s['image_count']} images (expected ~13)")

    return scenes


def import_scenes(conn, pancam_root, dry_run=False):
    root = Path(pancam_root)
    print(f"Scanning {root / 'MERA' / 'iof'} ...")

    scenes = find_scenes(root)
    if not scenes:
        print("No scenes found.")
        return

    existing = {row[0] for row in conn.execute("SELECT scene_key FROM scenes").fetchall()}

    added = skipped = 0
    for s in scenes:
        if s['scene_key'] in existing:
            skipped += 1
            continue
        if not dry_run:
            conn.execute(
                "INSERT INTO scenes (name, scene_key, status) VALUES (?, ?, 0)",
                (s['name'], s['scene_key']),
            )
        print(f"  {'[dry run] ' if dry_run else ''}+ {s['name']}  ({s['image_count']} images)")
        added += 1

    if not dry_run and added > 0:
        conn.commit()

    print(f"\n{added} scene(s) imported, {skipped} already in database.")


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
        import_scenes(conn, args.path, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
