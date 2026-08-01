#!/usr/bin/env python3
"""Import Pancam scenes into the ROVR database.

Primary method: CSV observation table (--csv path/to/obs_table.csv)
    Groups rows by (ROVER, SOL, SEQ_ID, obs_ix) to form unique scenes.
    All 33 CSV columns are stored in the DB; re-running is incremental (skips
    existing scene_keys) — except PMA, which is re-synced from the CSV for
    already-existing scenes if it differs, since PMA drift between the DB
    and the actual on-disk ROI Studio folder breaks "Open in ROI Studio".

Fallback method: folder scan (default, no flag needed)
    Walks MERA/####/iof and MERB/####/iof directories looking for
    Pancam IOF .IMG files. obs_ix defaults to 0 (single-pointing assumption).

Additional utility: --build-folders
    Ensures a named subfolder (default: working) exists inside every existing
    rover/#### directory.

Migration utility: --restructure-folders
    One-off move of the old rover/<kind>/solNNNN layout into the new
    rover/NNNN/<kind> layout (dropping the 'sol' prefix), for kind in
    iof, edr, practice, working. Safe to re-run — skips anything already
    migrated or missing.

Migration utility 2: --rename-folders
    One-off update to rename folder/file names.  Automated script that will crawl 
    thru R:\\Rice\\Pancam\\MERA\\####\\working\\<folder> and rename folders and 
    files to the new naming convention. Both the folder and the FITS file contained
    within will be renamed.  The new naming convention is as follows:
        Old formats:
            Sol####_p####_PMA# (where PMA can have anywhere from 1-4 #'s and no leading 0's)
            Sol####_p####v#_PMA# (where v and PMA can have anywhere from 1-4 #'s and no leading 0's)
        New format:
            Sol####_p####v#_PMA#_<NAME> (where v and PMA can have anywhere from 1-4 #'s and no leading 0's, 
            and <NAME> is the name of the scene in the database)
    any _v# version tags appended to the end of a folder are preserved in the folder name,
    and the FITS file will still omit the version tag.
    the v# between SEQID and PMA is SEQ_VER, and is found in the database.
    The <NAME> is also found in the database, and is the name of the scene.
    The .png panels ROI Studio writes beside the FITS are renamed along with it.

Migration utility 3: --fix-panel-names
    Repairs folders that an earlier --rename-folders run left half-migrated: it
    renamed the folder and its .fits/.sel but not the .png panels beside them,
    so those images still carry the pre-migration stem. Renames any panel whose
    stem doesn't match its folder. Safe to re-run.

Backup utility: --backup
    Creates a backup of the database file and the 'working' directories for
    both rovers (includes the fits and sel files),

Utility: --copy-approved
    Copies the latest .fits file for every approved scene into
    <path>\\ready_for_asdf. Safe to re-run — skips any scene whose destination
    file already exists.

Usage:
    python setup/import_scenes.py --csv obs_table.csv
    python setup/import_scenes.py --csv obs_table.csv --dry-run

    python setup/import_scenes.py
    python setup/import_scenes.py --path "R:\\Rice\\Pancam"
    python setup/import_scenes.py --dry-run

    python setup/import_scenes.py --build-folders
    python setup/import_scenes.py --build-folders --subfolder edr

    python setup/import_scenes.py --restructure-folders
    python setup/import_scenes.py --restructure-folders --dry-run

    python setup/import_scenes.py --rename-folders
    python setup/import_scenes.py --rename-folders --dry-run

    python setup/import_scenes.py --fix-panel-names
    python setup/import_scenes.py --fix-panel-names --dry-run

    python setup/import_scenes.py --wipe

    python setup/import_scenes.py --copy-approved
    python setup/import_scenes.py --copy-approved --dry-run
"""

import csv
import os
import re
import shutil
import sqlite3
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_db_connection, initialize_db
from app.models import SceneStatus
from app.paths import FolderKind, Panel, sol_dir_name, find_fits_file, versionless_name

try:
    from config import PANCAM_PATH, DB_PATH
except ImportError:
    PANCAM_PATH = None
    DB_PATH = None

# Current sol folder pattern (no 'sol' prefix) — used by the folder scanner
# and build_folders, which both operate on the current rover/####/<kind> layout.
_SOL_RE = re.compile(r'^(\d{4})$')

# Old sol folder pattern ('solNNNN') — used only by --restructure-folders to
# find sol directories still in the pre-migration rover/<kind>/solNNNN layout.
_OLD_SOL_RE = re.compile(r'^sol(\d{4})$', re.IGNORECASE)

# MER Pancam filename stem is exactly 27 chars (plus 3-char extension):
# [scid(1)][inst(1)][sclk(9)][prod(3)][site(4)][seq(5)][eye(1)][filt(1)][who(1)][ver(1)]
# The seq field (chars 17-21) is always 'P####' — the 4 digits are the seqID.
_STEM_LEN = 27

# Trailing ROI Studio folder "revision" tag — unrelated to SEQ_VER, just a
# manual re-save marker analysts append to a folder name. Preserved as-is by
# --rename-folders and never carried onto the .fits/.sel file names inside,
# mirroring app/ui/dashboard.py's _find_scene_file convention.
_REVISION_TAG_RE = re.compile(r'^(.+)_v(\d+)$', re.IGNORECASE)

# Pre-migration ROI Studio working/ folder name, once any trailing revision
# tag above has been stripped: Sol####_p####[v#]_PMA# — the embedded 'v#' is
# only present in one of the two old conventions and is never trusted (SEQ_VER
# is always re-read from the DB instead). Folders that don't match this exactly
# (e.g. already carrying a _<NAME> suffix) are assumed already migrated.
_OLD_ROI_FOLDER_RE = re.compile(r'^Sol(\d{4})_p(\d{4})(?:v\d+)?_PMA(\d+)$')


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


def _old_sol_num(dirname):
    m = _OLD_SOL_RE.match(dirname)
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


def _scan_sol(iof_dir, rover, sol):
    """Scan one sol's iof directory and return a dict of scene_key -> scene dict."""
    scenes = {}
    for f in iof_dir.iterdir():
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
    """Walk ####/iof directories for both rovers and import scenes by folder scan."""
    pancam_path = Path(pancam_root)
    rovers = ["MERA", "MERB"]

    for rover in rovers:
        rover_root = pancam_path / rover
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

            iof_dir = sol_dir / FolderKind.IOF
            if not iof_dir.exists():
                print(f"{prefix}  (no iof/ subfolder)")
                continue

            scenes = _scan_sol(iof_dir, rover, sol)
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

# Maps (ROVER, SOL, SEQ_ID, obs_ix) → representative dict, one row per unique
# scene. Subsequent rows for the same scene (same composite key, different
# filter/eye) are counted but not stored, EXCEPT: a stereo scene's L-eye and
# R-eye frames can carry different PMA (the two cameras point at slightly
# different mast angles for the same observation), and ROI Studio names its
# working/ folder after whichever frame it saved from — observed to be the
# L-eye frame. So the L-eye row is preferred as representative whenever one
# exists, regardless of row order in the CSV, to keep the stored PMA in sync
# with what ends up in the folder name. Non-stereo rows (no L/R split) keep
# the previous first-row-wins behavior.

def _scene_dict(key, rover, sol, seq_id, obs_ix, name, row):
    return {
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

def import_scenes_from_csv(conn, csv_path, dry_run=False):
    """Read a CSV observation table and import one scene per unique
    (ROVER, SOL, SEQ_ID, obs_ix) group. All 33 CSV columns are stored
    from one representative row of each group — see _scene_dict's L-eye
    preference for how that row is picked.

    For scene_keys that already exist, the rest of the row is left alone
    (see module docstring — re-running is incremental), except PMA: the
    on-disk ROI Studio working/ folder embeds PMA in its name, and its
    stereo pair's L-eye and R-eye rows can carry different PMA (the two
    cameras point at slightly different mast angles for the same
    observation) — ROI Studio has been observed to always name its folder
    after the L-eye frame. If the scene's already-stored pma doesn't match
    the (now correctly L-eye-preferred) representative row's pma, it's
    corrected here rather than requiring a full re-import."""

    path = Path(csv_path)
    if not path.exists():
        print(f"Error: CSV file '{csv_path}' does not exist.")
        sys.exit(1)

    existing_rows = conn.execute("SELECT scene_key, name, pma FROM scenes").fetchall()
    existing = {row[0] for row in existing_rows}
    existing_pma = {row[0]: (row[1], row[2]) for row in existing_rows}

    # First pass — group rows
    groups = {}          # scene_key → representative row dict
    group_is_l_eye = {}  # scene_key → whether that representative is an L-eye row
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
            is_l_eye = (row.get('FILTER') or '').strip().upper().startswith('L')
            if key not in groups or (is_l_eye and not group_is_l_eye[key]):
                groups[key] = _scene_dict(key, rover, sol, seq_id, obs_ix, name, row)
                group_is_l_eye[key] = is_l_eye

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

    # PMA drift correction — see docstring. Only scenes already in the DB
    # (skipped above) are eligible; a CSV pma of None never overwrites a
    # known value.
    pma_updates = []
    for key in existing:
        s = groups.get(key)
        if s is None or s['pma'] is None:
            continue
        name, db_pma = existing_pma[key]
        if db_pma != s['pma']:
            pma_updates.append((key, name, db_pma, s['pma']))

    if pma_updates:
        row_label = "[dry run] " if dry_run else ""
        print(f"\n{len(pma_updates)} scene(s) have a PMA mismatch vs. the CSV — correcting to match CSV:")
        for key, name, old_pma, new_pma in pma_updates:
            print(f"  {row_label}{name} ({key}): pma {old_pma} -> {new_pma}")
        if not dry_run:
            for key, _, _, new_pma in pma_updates:
                conn.execute("UPDATE scenes SET pma = ? WHERE scene_key = ?", (new_pma, key))
            conn.commit()


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
    """Ensure <subfolder_name> exists inside every existing rover/#### directory.

    Creates missing directories; skips any that already exist. No-op safe.
    """
    pancam_path = Path(pancam_root)
    rovers = ["MERA", "MERB"]
    for rover in rovers:
        rover_root = pancam_path / rover
        if not rover_root.exists():
            print(f"Skipping {rover}: {rover_root} does not exist.")
            continue

        sol_dirs = sorted(
            d for d in rover_root.iterdir()
            if d.is_dir() and _sol_num(d.name) is not None
        )
        created = skipped = 0
        for sol_dir in sol_dirs:
            target = sol_dir / subfolder_name
            if target.exists():
                skipped += 1
            else:
                target.mkdir(parents=True, exist_ok=True)
                created += 1
        print(f"{rover}/*/{subfolder_name}: {created} created, {skipped} already existed")


def restructure_folders(pancam_root, dry_run=False):
    """One-off migration: move rover/<kind>/solNNNN -> rover/NNNN/<kind>
    for kind in FolderKind.ALL (iof, edr, practice, working), dropping the
    old 'sol' prefix to match the current bare-#### naming convention.

    Safe to re-run — skips any sol dir whose destination already exists,
    and removes each <kind> root once it's empty.
    """
    pancam_path = Path(pancam_root)
    rovers = ["MERA", "MERB"]

    for rover in rovers:
        rover_root = pancam_path / rover
        if not rover_root.exists():
            print(f"Skipping {rover}: {rover_root} does not exist.")
            continue

        moved = skipped = 0
        for kind in FolderKind.ALL:
            kind_root = rover_root / kind
            if not kind_root.exists():
                continue

            sol_dirs = sorted(
                d for d in kind_root.iterdir()
                if d.is_dir() and _old_sol_num(d.name) is not None
            )
            for sol_dir in sol_dirs:
                sol = _old_sol_num(sol_dir.name)
                target = rover_root / sol_dir_name(sol) / kind
                if target.exists():
                    print(f"  SKIP {sol_dir} -> {target} (destination already exists)")
                    skipped += 1
                    continue
                label = "[dry run] " if dry_run else ""
                print(f"  {label}{sol_dir} -> {target}")
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(sol_dir), str(target))
                moved += 1

            if not dry_run and kind_root.exists() and not any(kind_root.iterdir()):
                kind_root.rmdir()

        print(f"{rover}: {moved} sol/<kind> folder(s) moved, {skipped} skipped (destination existed)")


def rename_folders(conn, pancam_root, dry_run=False):
    """One-off migration: rename ROI Studio working/ folders — and the .fits/.sel
    files inside them — from the old Sol####_p####[v#]_PMA# convention to the
    current Sol####_p####v#_PMA#_<NAME> convention, where the v# (SEQ_VER) and
    <NAME> segments come from the matching DB scene row (matched on rover, sol,
    seq_id, and pma).

    A trailing "_v#" revision tag is preserved on the folder as-is, and is
    never carried onto the file names inside it (the files are always named
    after the folder's own name with that trailing tag stripped).

    Safe to re-run — only folders whose (revision-tag-stripped) name is a bare
    Sol####_p####[v#]_PMA# match are touched; anything else is assumed already
    migrated. Folders with no matching scene, an ambiguous (>1) match, or a
    rename destination that already exists are skipped with a warning.
    """
    pancam_path = Path(pancam_root)
    rovers = ["MERA", "MERB"]

    for rover in rovers:
        rover_root = pancam_path / rover
        if not rover_root.exists():
            print(f"Skipping {rover}: {rover_root} does not exist.")
            continue

        sol_dirs = sorted(
            d for d in rover_root.iterdir()
            if d.is_dir() and _sol_num(d.name) is not None
        )

        renamed = skipped_not_old = skipped_no_match = 0
        skipped_ambiguous = skipped_conflict = 0

        for sol_dir in sol_dirs:
            working_dir = sol_dir / FolderKind.WORKING
            if not working_dir.exists():
                continue

            for folder in sorted(d for d in working_dir.iterdir() if d.is_dir()):
                m = _REVISION_TAG_RE.match(folder.name)
                if m:
                    versionless, revision_suffix = m.group(1), f"_v{m.group(2)}"
                else:
                    versionless, revision_suffix = folder.name, ""

                old_match = _OLD_ROI_FOLDER_RE.match(versionless)
                if not old_match:
                    skipped_not_old += 1
                    continue

                sol_str, seq_digits, pma_str = old_match.groups()
                sol = int(sol_str)
                pma = int(pma_str)
                seq_id = f"P{seq_digits}"

                rows = conn.execute(
                    "SELECT name, seq_ver FROM scenes WHERE rover=? AND sol=? AND seq_id=? AND pma=?",
                    (rover, sol, seq_id, pma),
                ).fetchall()

                if not rows:
                    print(f"  SKIP {folder} (no matching scene in DB for {rover} sol{sol:04d} {seq_id} PMA{pma})")
                    skipped_no_match += 1
                    continue
                if len(rows) > 1:
                    print(f"  SKIP {folder} ({len(rows)} matching scenes in DB for {rover} sol{sol:04d} {seq_id} PMA{pma} - ambiguous)")
                    skipped_ambiguous += 1
                    continue

                name, seq_ver = rows[0]
                if seq_ver is not None:
                    new_versionless = f"Sol{sol:04d}_p{seq_digits}v{seq_ver}_PMA{pma}_{name}"
                else:
                    new_versionless = f"Sol{sol:04d}_p{seq_digits}_PMA{pma}_{name}"
                new_folder_name = new_versionless + revision_suffix
                new_folder_path = working_dir / new_folder_name

                if new_folder_path.exists():
                    print(f"  SKIP {folder} -> {new_folder_path} (destination already exists)")
                    skipped_conflict += 1
                    continue

                label = "[dry run] " if dry_run else ""
                print(f"  {label}{folder.name} -> {new_folder_name}")

                if not dry_run:
                    # The .png panels ROI Studio saves alongside the .fits/.sel
                    # follow the same stem and have to move with it — leaving
                    # them behind is what stranded older folders' images under
                    # a name that no longer matches anything (see
                    # --fix-panel-names, which repairs those).
                    #
                    # Renaming the files bumps this folder's own mtime, which
                    # is what Explorer sorts by, so it is captured first and
                    # put back after the folder itself moves.
                    folder_stat = folder.stat()
                    for ext in (".fits", ".sel") + Panel.ALL:
                        old_file = folder / (versionless + ext)
                        if old_file.exists():
                            st = old_file.stat()
                            new_file = folder / (new_versionless + ext)
                            old_file.rename(new_file)
                            os.utime(new_file, (st.st_atime, st.st_mtime))
                    folder.rename(new_folder_path)
                    os.utime(new_folder_path, (folder_stat.st_atime, folder_stat.st_mtime))

                renamed += 1

        print(
            f"{rover}: {renamed} renamed, {skipped_not_old} already migrated / not old format, "
            f"{skipped_no_match} no DB match, {skipped_ambiguous} ambiguous, "
            f"{skipped_conflict} conflicts\n"
        )


def fix_panel_names(pancam_root, dry_run=False):
    """Rename ROI Studio's .png panels to match the folder they sit in.

    --rename-folders originally moved only the .fits/.sel when it renamed a
    folder, so every folder it touched still holds images under the
    pre-migration stem — e.g. Sol0007_p2530_PMA791_left_dcs.png inside
    Sol0007_p2530v1_PMA791_pancam_magic_carpet. Those folders look already
    migrated to --rename-folders and are skipped by it, so they need this pass.

    Only files ending in one of the known panel suffixes are considered, and a
    file is renamed only if its stem differs from the folder's own
    (revision-tag-stripped) name. Anything prefixed OLD_ is left alone -- those
    are deliberate archives, not stragglers -- and a rename whose destination
    already exists is skipped rather than overwriting it.
    """
    pancam_path = Path(pancam_root)

    for rover in ["MERA", "MERB"]:
        rover_root = pancam_path / rover
        if not rover_root.exists():
            print(f"Skipping {rover}: {rover_root} does not exist.")
            continue

        sol_dirs = sorted(
            d for d in rover_root.iterdir()
            if d.is_dir() and _sol_num(d.name) is not None
        )

        renamed = already = skipped_conflict = skipped_archive = 0

        for sol_dir in sol_dirs:
            working_dir = sol_dir / FolderKind.WORKING
            if not working_dir.exists():
                continue

            for folder in sorted(d for d in working_dir.iterdir() if d.is_dir()):
                stem = versionless_name(folder.name)
                # Renaming a file updates its *directory's* mtime, which is what
                # Explorer sorts "Date modified" by. Left alone, a repair pass
                # floats every touched folder to the top of the listing and can
                # invert which revision looks newest. Restored below.
                folder_stat = folder.stat()
                folder_touched = False
                for f in sorted(p for p in folder.iterdir() if p.is_file()):
                    suffix = next((s for s in Panel.ALL if f.name.lower().endswith(s.lower())), None)
                    if suffix is None:
                        continue
                    if f.name.startswith("OLD_"):
                        skipped_archive += 1
                        continue
                    if f.name == stem + suffix:
                        already += 1
                        continue
                    target = folder / (stem + suffix)
                    if target.exists():
                        print(f"  SKIP {f} -> {target.name} (destination already exists)")
                        skipped_conflict += 1
                        continue
                    label = "[dry run] " if dry_run else ""
                    print(f"  {label}{folder.name}/{f.name} -> {target.name}")
                    if not dry_run:
                        # Carry the original timestamps across explicitly. A
                        # rename preserves mtime on every filesystem this has
                        # been tried on, but these files are the inputs
                        # slide_is_current() compares a slide against, and a
                        # bulk re-stamp to "now" would silently mark every
                        # scene's panels newer than its slide. Too cheap not to
                        # guarantee outright.
                        st = f.stat()
                        f.rename(target)
                        os.utime(target, (st.st_atime, st.st_mtime))
                        folder_touched = True
                    renamed += 1

                if folder_touched:
                    os.utime(folder, (folder_stat.st_atime, folder_stat.st_mtime))

        print(
            f"{rover}: {renamed} panel(s) renamed, {already} already correct, "
            f"{skipped_conflict} conflicts, {skipped_archive} OLD_ archives left alone\n"
        )


def backup_scenes(pancam_root, db_path):
    """Back up the database and both rovers' 'working' directories under
    into R:\\Rice\\Backup. 

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
    backup_root = pancam_path.parent / "Backup"
    db_backup_dir = backup_root / "db"
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
        print(f"Error: database backup failed after {retries} attempts: {last_error}")
        print("The network drive may be busy or briefly unreachable -- try again in a moment.")
        return

    print(f"Database backed up to {db_backup_path}")

    copied = skipped = 0
    for rover in ["MERA", "MERB"]:
        rover_root = pancam_path / rover
        if not rover_root.exists():
            print(f"Skipping {rover}: {rover_root} does not exist.")
            continue

        sol_dirs = sorted(
            d for d in rover_root.iterdir() if d.is_dir() and _sol_num(d.name) is not None
        )
        for sol_dir in sol_dirs:
            working_dir = sol_dir / FolderKind.WORKING
            if not working_dir.exists():
                continue
            for item in working_dir.rglob("*"):
                if item.is_dir():
                    continue
                dest = backup_root / item.relative_to(pancam_path)
                if dest.exists():
                    skipped += 1
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
                copied += 1

    print(f"Working files: {copied} copied, {skipped} already backed up (skipped)")


def copy_approved_fits(conn, pancam_root, dry_run=False):
    """Copy the latest .fits file for every approved (status 7) scene into
    <pancam_root>/ready_for_asdf.

    Safe to re-run — a destination file that already exists is left alone
    (never re-copied), same spirit as backup_scenes' working-file mirror.
    """
    dest_dir = Path(pancam_root) / "ready_for_asdf"
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)

    scenes = conn.execute(
        "SELECT * FROM scenes WHERE status = ?", (SceneStatus.APPROVED,)
    ).fetchall()

    copied = skipped = missing = 0
    for scene in scenes:
        fits_path = find_fits_file(pancam_root, scene)
        if not fits_path:
            print(f"  MISSING .fits for {scene['name']} (scene id {scene['id']})")
            missing += 1
            continue

        dest_path = dest_dir / os.path.basename(fits_path)
        if dest_path.exists():
            skipped += 1
            continue

        label = "[dry run] " if dry_run else ""
        print(f"  {label}{fits_path} -> {dest_path}")
        if not dry_run:
            shutil.copy2(fits_path, dest_path)
        copied += 1

    print(
        f"\nApproved scenes: {len(scenes)} total, {copied} copied, "
        f"{skipped} already present, {missing} missing .fits file"
    )


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
        help="Ensure a subfolder (see --subfolder) exists under every rover/#### directory.",
    )
    parser.add_argument(
        "--subfolder",
        default="working",
        metavar="NAME",
        help="Subfolder name to create under each rover/#### directory (default: working).",
    )
    parser.add_argument(
        "--restructure-folders",
        action="store_true",
        help="One-off migration: move rover/<kind>/solNNNN into rover/NNNN/<kind> "
            "for kind in iof, edr, practice, working.",
    )
    parser.add_argument(
        "--rename-folders",
        action="store_true",
        help="One-off migration: rename working/ ROI folders (and their .fits/.sel "
            "files) from Sol####_p####[v#]_PMA# to Sol####_p####v#_PMA#_<NAME>, "
            "using SEQ_VER and NAME from the matching DB scene.",
    )
    parser.add_argument(
        "--fix-panel-names",
        action="store_true",
        help="Rename ROI Studio .png panels to match the folder they sit in, "
            "repairing folders whose images were left behind by an earlier "
            "--rename-folders run.",
    )
    parser.add_argument(
        "--wipe",
        action="store_true",
        help="Wipe all scenes and reviews. Users are not affected.",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create a backup of the database file and the 'working' directories for both rovers (includes fits and sel files)."
    )
    parser.add_argument(
        "--copy-approved",
        action="store_true",
        help="Copy the latest .fits file for every approved scene into <path>/ready_for_asdf.",
    )
    args = parser.parse_args()

    # --build-folders and --restructure-folders don't need a DB connection
    if args.build_folders:
        if not args.path:
            print("Error: --build-folders requires --path or PANCAM_PATH in config.py.")
            sys.exit(1)
        if not Path(args.path).exists():
            print(f"Error: '{args.path}' does not exist.")
            sys.exit(1)
        build_folders(args.path, args.subfolder)
        return

    if args.restructure_folders:
        if not args.path:
            print("Error: --restructure-folders requires --path or PANCAM_PATH in config.py.")
            sys.exit(1)
        if not Path(args.path).exists():
            print(f"Error: '{args.path}' does not exist.")
            sys.exit(1)
        restructure_folders(args.path, dry_run=args.dry_run)
        return

    if args.rename_folders:
        if not args.path:
            print("Error: --rename-folders requires --path or PANCAM_PATH in config.py.")
            sys.exit(1)
        if not Path(args.path).exists():
            print(f"Error: '{args.path}' does not exist.")
            sys.exit(1)
        initialize_db()
        conn = get_db_connection()
        try:
            rename_folders(conn, args.path, dry_run=args.dry_run)
        finally:
            conn.close()
        return

    if args.fix_panel_names:
        if not args.path:
            print("Error: --fix-panel-names requires --path or PANCAM_PATH in config.py.")
            sys.exit(1)
        if not Path(args.path).exists():
            print(f"Error: '{args.path}' does not exist.")
            sys.exit(1)
        fix_panel_names(args.path, dry_run=args.dry_run)
        return

    if args.backup:
        if not args.path:
            print("Error: --backup requires --path or PANCAM_PATH in config.py.")
            sys.exit(1)
        if not DB_PATH:
            print("Error: --backup requires DB_PATH in config.py.")
            sys.exit(1)
        backup_scenes(args.path, DB_PATH)
        return

    if args.copy_approved:
        if not args.path:
            print("Error: --copy-approved requires --path or PANCAM_PATH in config.py.")
            sys.exit(1)
        if not Path(args.path).exists():
            print(f"Error: '{args.path}' does not exist.")
            sys.exit(1)
        initialize_db()
        conn = get_db_connection()
        try:
            copy_approved_fits(conn, args.path, dry_run=args.dry_run)
        finally:
            conn.close()
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
