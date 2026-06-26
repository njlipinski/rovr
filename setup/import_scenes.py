#!/usr/bin/env python3
"""Import Pancam scenes into the ROVR database.

Primary method: CSV observation table (--csv path/to/obs_table.csv)
  Groups rows by (ROVER, SOL, SEQ_ID, obs_ix) to form unique scenes.
  All 33 CSV columns are stored in the DB; re-running is incremental (skips
  existing scene_keys).

Fallback method: folder scan (default, no flag needed)
  Walks MERA/iof/sol#### and MERB/iof/sol#### directories looking for
  Pancam IOF .IMG files. obs_ix defaults to 0 (single-pointing assumption).

Additional utility: --build-folders
  Mirrors the iof/sol#### tree into any named subfolder (default: working).

Usage:
    python setup/import_scenes.py --csv obs_table.csv
    python setup/import_scenes.py --csv obs_table.csv --dry-run

    python setup/import_scenes.py
    python setup/import_scenes.py --path "R:\\Rice\\Pancam"
    python setup/import_scenes.py --dry-run

    python setup/import_scenes.py --build-folders
    python setup/import_scenes.py --build-folders --subfolder edr

    python setup/import_scenes.py --wipe
"""

import csv
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

# sol#### folder pattern used by both the folder scanner and build_folders
_SOL_RE = re.compile(r'^sol(\d{4})$', re.IGNORECASE)

# MER Pancam filename stem is exactly 27 chars (plus 3-char extension):
# [scid(1)][inst(1)][sclk(9)][prod(3)][site(4)][seq(5)][eye(1)][filt(1)][who(1)][ver(1)]
# The seq field (chars 17-21) is always 'P####' — the 4 digits are the seqID.
_STEM_LEN = 27


# ── Conversion helpers ────────────────────────────────────────────────────────

def _to_int(val):
    """Convert a CSV string to int, returning None for blank/unparseable values."""
    if val is None:
        return None
    v = str(val).strip()
    if not v:
        return None
    try:
        return int(float(v))
    except (ValueError, OverflowError):
        return None


def _to_float(val):
    """Convert a CSV string to float, returning None for blank/unparseable values."""
    if val is None:
        return None
    v = str(val).strip()
    if not v:
        return None
    try:
        return float(v)
    except (ValueError, OverflowError):
        return None


# ── Folder scanner (fallback) ─────────────────────────────────────────────────

def _sol_num(dirname):
    m = _SOL_RE.match(dirname)
    return int(m.group(1)) if m else None


def _parse_img(filename):
    """Return seqID (e.g. 'P2210') if this is an IOF Pancam .IMG, else None."""
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
    return seq_field   # full 'P####' token


def _scan_sol(sol_dir, rover):
    """Scan one sol directory and return a dict of scene_key -> scene dict."""
    sol = _sol_num(sol_dir.name)
    scenes = {}
    for f in sol_dir.iterdir():
        if not f.is_file():
            continue
        seq_id = _parse_img(f.name)
        if seq_id is None:
            continue
        # obs_ix=0: folder scan has no obs index info; single-pointing assumption
        key = f"{rover}/sol{sol:04d}/{seq_id}/obs0"
        if key not in scenes:
            scenes[key] = {
                'name': f"{rover}sol{sol:04d}{seq_id}obs0",
                'scene_key': key,
                'rover': rover,
                'sol': sol,
                'seq_id': seq_id,
                'obs_ix': 0,
                'image_count': 0,
            }
        scenes[key]['image_count'] += 1
    return scenes


def import_scenes_from_folders(conn, pancam_root, dry_run=False):
    """Walk iof directories for both rovers and import scenes by folder scan."""
    pancam_path = Path(pancam_root)
    rovers = ["MERA", "MERB"]

    for rover in rovers:
        rover_root = pancam_path / rover / "iof"
        if not rover_root.exists():
            print(f"Skipping {rover}: {rover_root} does not exist.")
            continue

        sol_dirs = sorted(
            (d for d in rover_root.iterdir() if d.is_dir() and _sol_num(d.name) is not None),
            key=lambda d: _sol_num(d.name) or -1,
        )
        total_sols = len(sol_dirs)
        if total_sols == 0:
            print(f"{rover}: no sol directories found.")
            continue

        existing = {row[0] for row in conn.execute("SELECT scene_key FROM scenes").fetchall()}
        print(f"{rover}: {total_sols} sol directories, {len(existing)} scenes already in DB\n")

        total_added = total_skipped = total_warnings = 0

        for i, sol_dir in enumerate(sol_dirs, 1):
            sol = _sol_num(sol_dir.name)
            pct = i / total_sols * 100
            prefix = f"[{i}/{total_sols}  {pct:5.1f}%]  {rover} sol{sol:04d}"

            scenes = _scan_sol(sol_dir, rover)
            if not scenes:
                print(f"{prefix}  (no IOF scenes)")
                continue

            warnings = [s for s in scenes.values() if s['image_count'] != 13]
            new_scenes = [s for s in scenes.values() if s['scene_key'] not in existing]
            skip_count = len(scenes) - len(new_scenes)

            if not dry_run:
                for s in new_scenes:
                    conn.execute(
                        """INSERT INTO scenes
                           (name, scene_key, status, rover, sol, seq_id, obs_ix)
                           VALUES (?, ?, 0, ?, ?, ?, ?)""",
                        (s['name'], s['scene_key'], s['rover'], s['sol'], s['seq_id'], s['obs_ix']),
                    )
                if new_scenes:
                    conn.commit()
                    existing.update(s['scene_key'] for s in new_scenes)

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
                print(f"  {w['name']} has {w['image_count']} images")

            total_added += len(new_scenes)
            total_skipped += skip_count

        print(f"\n{rover}: {total_added} imported, {total_skipped} already existed", end="")
        if total_warnings:
            print(f", {total_warnings} image-count warning(s)\n")
        else:
            print("\n")


# ── CSV importer ──────────────────────────────────────────────────────────────

# Maps (ROVER, SOL, SEQ_ID, obs_ix) → first-row representative dict.
# We only need one row per unique scene; subsequent rows for the same scene
# (same composite key, different filter/eye) are counted but not stored.

def import_scenes_from_csv(conn, csv_path, dry_run=False):
    """Read a CSV observation table and import one scene per unique
    (ROVER, SOL, SEQ_ID, obs_ix) group. All 33 CSV columns are stored
    from the first representative row of each group."""

    path = Path(csv_path)
    if not path.exists():
        print(f"Error: CSV file '{csv_path}' does not exist.")
        sys.exit(1)

    existing = {row[0] for row in conn.execute("SELECT scene_key FROM scenes").fetchall()}

    # First pass — group rows
    groups = {}   # scene_key → representative row dict
    row_counts = {}
    total_rows = 0

    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_rows += 1
            rover   = row.get('ROVER', '').strip().upper()
            # CSV stores single letter ('A'/'B'); normalize to full rover name
            if rover in ('A', 'B'):
                rover = 'MER' + rover
            sol_str = row.get('SOL', '').strip()
            seq_id  = row.get('SEQ_ID', '').strip().upper()
            obs_ix  = _to_int(row.get('obs_ix', '0'))
            name    = row.get('NAME', '').strip()

            if not rover or not sol_str or not seq_id:
                print(f"  Warning: skipping row {total_rows} — missing ROVER/SOL/SEQ_ID")
                continue

            sol = _to_int(sol_str)
            if sol is None:
                print(f"  Warning: skipping row {total_rows} — unparseable SOL '{sol_str}'")
                continue
            if obs_ix is None:
                obs_ix = 0

            key = f"{rover}/sol{sol:04d}/{seq_id}/obs{obs_ix}"
            row_counts[key] = row_counts.get(key, 0) + 1
            if key not in groups:
                groups[key] = {
                    'scene_key':              key,
                    'name':                   name or f"{rover}sol{sol:04d}{seq_id}obs{obs_ix}",
                    'fn':                     row.get('fn', '').strip() or None,
                    'rover':                  rover,
                    'sclk':                   _to_int(row.get('SCLK')),
                    'product_type':           row.get('PRODUCT_TYPE', '').strip() or None,
                    'site':                   _to_int(row.get('SITE')),
                    'pos':                    _to_int(row.get('POS')),
                    'seq_id':                 seq_id,
                    'filter':                 row.get('FILTER', '').strip() or None,
                    'version':                _to_int(row.get('VERSION')),
                    'sol':                    sol,
                    'seq_ver':                _to_int(row.get('SEQ_VER')),
                    'lines':                  _to_int(row.get('LINES')),
                    'pma':                    _to_int(row.get('PMA')),
                    'obs_ix':                 obs_ix,
                    'frame_type':             row.get('FRAME_TYPE', '').strip() or None,
                    'ltst':                   row.get('LTST', '').strip() or None,
                    'product_creation_time':  row.get('PRODUCT_CREATION_TIME', '').strip() or None,
                    'compression':            row.get('COMPRESSION', '').strip() or None,
                    'first_line':             _to_int(row.get('FIRST_LINE')),
                    'first_sample':           _to_int(row.get('FIRST_SAMPLE')),
                    'samples':                _to_int(row.get('SAMPLES')),
                    'solar_elevation':        _to_float(row.get('SOLAR_ELEVATION')),
                    'instrument_elevation':   _to_float(row.get('INSTRUMENT_ELEVATION')),
                    'instrument_azimuth':     _to_float(row.get('INSTRUMENT_AZIMUTH')),
                    'solar_azimuth':          _to_float(row.get('SOLAR_AZIMUTH')),
                    'incidence_angle':        _to_float(row.get('INCIDENCE_ANGLE')),
                    'emission_angle':         _to_float(row.get('EMISSION_ANGLE')),
                    'phase_angle':            _to_float(row.get('PHASE_ANGLE')),
                    'tau':                    _to_float(row.get('TAU')),
                    'rover_elevation':        _to_float(row.get('ROVER_ELEVATION')),
                    'lon':                    _to_float(row.get('LON')),
                    'lat':                    _to_float(row.get('LAT')),
                }

    print(f"CSV: {total_rows} rows → {len(groups)} unique scenes")
    print(f"Already in DB: {len(existing)}\n")

    new_scenes = [s for s in groups.values() if s['scene_key'] not in existing]
    skip_count = len(groups) - len(new_scenes)

    if not dry_run:
        for s in new_scenes:
            conn.execute("""
                INSERT INTO scenes (
                    scene_key, name, status,
                    fn, rover, sclk, product_type, site, pos, seq_id, filter,
                    version, sol, seq_ver, lines, pma, obs_ix, frame_type, ltst,
                    product_creation_time, compression, first_line, first_sample,
                    samples, solar_elevation, instrument_elevation, instrument_azimuth,
                    solar_azimuth, incidence_angle, emission_angle, phase_angle,
                    tau, rover_elevation, lon, lat
                ) VALUES (
                    :scene_key, :name, 0,
                    :fn, :rover, :sclk, :product_type, :site, :pos, :seq_id, :filter,
                    :version, :sol, :seq_ver, :lines, :pma, :obs_ix, :frame_type, :ltst,
                    :product_creation_time, :compression, :first_line, :first_sample,
                    :samples, :solar_elevation, :instrument_elevation, :instrument_azimuth,
                    :solar_azimuth, :incidence_angle, :emission_angle, :phase_angle,
                    :tau, :rover_elevation, :lon, :lat
                )
            """, s)
        if new_scenes:
            conn.commit()

    label = "  [dry run]" if dry_run else ""
    print(f"{label}+{len(new_scenes)} imported, {skip_count} already existed")


# ── Utilities ─────────────────────────────────────────────────────────────────

def wipe_scenes(conn):
    """Delete all rows from scenes and reviews, leaving users intact."""
    scene_count = conn.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
    review_count = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    conn.execute("DELETE FROM reviews")
    conn.execute("DELETE FROM scenes")
    conn.commit()
    print(f"Wiped {scene_count} scene(s) and {review_count} review(s). Users untouched.")


def build_folders(pancam_root, subfolder_name):
    """Mirror each rover's iof/sol#### tree into rover/<subfolder_name>/sol####.

    Creates missing directories; skips any that already exist. No-op safe.
    """
    pancam_path = Path(pancam_root)
    rovers = ["MERA", "MERB"]
    for rover in rovers:
        iof_root = pancam_path / rover / "iof"
        if not iof_root.exists():
            print(f"Skipping {rover}: {iof_root} does not exist.")
            continue

        target_root = pancam_path / rover / subfolder_name
        sol_dirs = sorted(
            d for d in iof_root.iterdir()
            if d.is_dir() and _sol_num(d.name) is not None
        )
        created = skipped = 0
        for sol_dir in sol_dirs:
            target = target_root / sol_dir.name
            if target.exists():
                skipped += 1
            else:
                target.mkdir(parents=True, exist_ok=True)
                created += 1
        print(f"{rover}/{subfolder_name}: {created} created, {skipped} already existed")


# ── CLI ───────────────────────────────────────────────────────────────────────

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
        "--csv",
        metavar="FILE",
        help="Path to a CSV observation table (primary import method).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without writing to the database.",
    )
    parser.add_argument(
        "--build-folders",
        action="store_true",
        help="Mirror iof/sol#### tree into a new subfolder (see --subfolder).",
    )
    parser.add_argument(
        "--subfolder",
        default="working",
        metavar="NAME",
        help="Subfolder name to create under each rover root (default: working).",
    )
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="Wipe all scenes and reviews. Users are not affected.",
    )
    args = parser.parse_args()

    # --build-folders doesn't need a DB connection
    if args.build_folders:
        if not args.path:
            print("Error: --build-folders requires --path or PANCAM_PATH in config.py.")
            sys.exit(1)
        if not Path(args.path).exists():
            print(f"Error: '{args.path}' does not exist.")
            sys.exit(1)
        build_folders(args.path, args.subfolder)
        return

    initialize_db()
    conn = get_db_connection()
    try:
        if args.wipe:
            print("WARNING: --wipe will delete all scenes and reviews from the database.")
            print("Users will not be affected. This cannot be undone.")
            confirm = input("Type YES to continue: ").strip()
            if confirm != "YES":
                print("Cancelled.")
                sys.exit(0)
            wipe_scenes(conn)
            print()

        if args.csv:
            import_scenes_from_csv(conn, args.csv, dry_run=args.dry_run)
        else:
            if not args.path:
                print("Error: no path specified and PANCAM_PATH is not set in config.py.")
                print("Provide --path or use --csv for CSV import.")
                sys.exit(1)
            if not Path(args.path).exists():
                print(f"Error: '{args.path}' does not exist.")
                sys.exit(1)
            import_scenes_from_folders(conn, args.path, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
