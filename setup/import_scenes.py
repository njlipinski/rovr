#!/usr/bin/env python3
"""Scan R:\Rice\Pancam\MERA\iof\sol#### directories for Pancam .IMG files,
group images by (site, pos, seq) to identify scenes, and import new ones as
unclaimed scenes (status 0).

Both left and right eye images for the same pointing are part of the same scene.
Owner is not set at import time — it is assigned when analyst 1 claims the scene.

Usage (run from repo root):
    python setup/import_scenes.py
    python setup/import_scenes.py --path "R:\\Rice\\Pancam\\MERA\\iof"
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

# MER EDR/RDR filename: 27 chars before the dot, then 3-char extension.
# Layout: [scid(1)][inst(1)][sclk(9)][prod(3)][site(2)][pos(2)][seq(5)][eye(1)][filt(1)][who(1)][ver(1)].[ext(3)]
_IMG_RE = re.compile(
    r'^([12])'          # scid: 1=MER-B, 2=MER-A
    r'([A-Z])'          # inst: P=Pancam, N=Navcam, F/R=Hazcam, etc.
    r'\d{9}'            # sclk
    r'[A-Z0-9]{3}'      # product type (e.g. IOF, EFF)
    r'([A-Z0-9]{2})'    # site
    r'([A-Z0-9]{2})'    # pos
    r'([A-Z0-9]{5})'    # seq (e.g. P2210)
    r'[LRM]'            # eye — L and R are the same scene
    r'[0-9A-Z]'         # filter
    r'[A-Z][0-9]'       # who + ver
    r'\.[A-Z]{3}$',
    re.IGNORECASE,
)


def _parse_pancam(filename):
    """Return (site, pos, seq) if filename is a Pancam .IMG, else None."""
    m = _IMG_RE.match(filename)
    if not m:
        return None
    inst, site, pos, seq = m.group(2).upper(), m.group(3).upper(), m.group(4).upper(), m.group(5).upper()
    if inst != 'P':
        return None
    return site, pos, seq


def _sol_num(dirname):
    m = re.match(r'^sol(\d{4})$', dirname, re.IGNORECASE)
    return int(m.group(1)) if m else None


def find_scenes(root):
    """Walk sol#### subdirectories and return sorted list of unique Pancam scenes.

    Each scene dict has: name, scene_key, sol, image_count.
    scene_key doubles as roi_filename — a stable unique identifier of the form
    'MERA/sol####/SSPPQQQQQ' (site, pos, seq).
    """
    seen = {}
    sol_dirs = sorted(
        (d for d in Path(root).iterdir() if d.is_dir() and _sol_num(d.name) is not None),
        key=lambda d: _sol_num(d.name),
    )
    for sol_dir in sol_dirs:
        sol = _sol_num(sol_dir.name)
        for f in sol_dir.iterdir():
            if not f.is_file():
                continue
            parsed = _parse_pancam(f.name)
            if parsed is None:
                continue
            site, pos, seq = parsed
            key = f"MERA/sol{sol:04d}/{site}{pos}{seq}"
            if key not in seen:
                seen[key] = {
                    'name': f"Sol {sol:04d} — {site}{pos}{seq}",
                    'scene_key': key,
                    'sol': sol,
                    'image_count': 0,
                }
            seen[key]['image_count'] += 1

    return sorted(seen.values(), key=lambda s: (s['sol'], s['scene_key']))


def import_scenes(conn, scan_path, dry_run=False):
    root = Path(scan_path)
    sol_count = sum(1 for d in root.iterdir() if d.is_dir() and _sol_num(d.name) is not None)
    print(f"Scanning {root}  ({sol_count} sol directories) ...")

    scenes = find_scenes(root)
    if not scenes:
        print("No Pancam scenes found.")
        return

    existing = {row[0] for row in conn.execute("SELECT roi_filename FROM scenes").fetchall()}

    added = skipped = 0
    for s in scenes:
        if s['scene_key'] in existing:
            skipped += 1
            continue
        if not dry_run:
            conn.execute(
                "INSERT INTO scenes (name, roi_filename, status) VALUES (?, ?, 0)",
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
        help="Root folder containing sol#### subdirectories (defaults to PANCAM_PATH in config.py)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without writing to the database",
    )
    args = parser.parse_args()

    if not args.path:
        print("Error: no path specified and PANCAM_PATH is not set in config.py.")
        print("Usage: python setup/import_scenes.py --path <path>")
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
