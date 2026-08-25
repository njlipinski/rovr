#!/usr/bin/env python3
"""ROVR maintenance toolbox: scene import, R:\\ drive upkeep, and one-off migrations.

Run with no arguments for the command list, or `<command> --help` for one
command's options. Each task function below carries the detail on what it does
and why it is safe to re-run.

    python setup/util.py                          # list commands
    python setup/util.py import obs_table.csv
    python setup/util.py backup
    python setup/util.py build-slides --dry-run
    python setup/util.py migrate --help
"""

import csv
import os
import re
import shutil
import sqlite3
import sys
import time
import argparse
from contextlib import contextmanager
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

# Old sol folder pattern ('solNNNN') — used only by restructure_folders() to
# find sol directories still in the pre-migration rover/<kind>/solNNNN layout.
_OLD_SOL_RE = re.compile(r'^sol(\d{4})$', re.IGNORECASE)

# MER Pancam filename stem is exactly 27 chars (plus 3-char extension):
# [scid(1)][inst(1)][sclk(9)][prod(3)][site(4)][seq(5)][eye(1)][filt(1)][who(1)][ver(1)]
# The seq field (chars 17-21) is always 'P####' — the 4 digits are the seqID.
_STEM_LEN = 27

# Trailing ROI Studio folder "revision" tag — unrelated to SEQ_VER, just a
# manual re-save marker analysts append to a folder name. Preserved as-is by
# rename_folders() and never carried onto the .fits/.sel file names inside,
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
    """Convert a CSV string to int, returning None for blank/unparsable values."""
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
    """Convert a CSV string to float, returning None for blank/unparsable values."""
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
    """Walk ####/iof directories for both rovers and import scenes by folder scan.

    Fallback for when there is no CSV observation table. Only the columns the
    filenames themselves carry are populated.
    """
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
                        # updated_at is named explicitly rather than left to the
                        # column default, which on an existing DB predates the
                        # switch to storing UTC.
                        """INSERT INTO scenes
                            (name, scene_key, status, rover, sol, seq_id, obs_ix, updated_at)
                            VALUES (?, ?, 0, ?, ?, ?, ?, datetime('now'))""",
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

# Filter positions sparc's scan_pcam_files() discards before ordering an
# observation's frames. Mirrored here so the representative row chosen below is
# the same frame ROI Studio names its folder after.
_INVALID_FILTERS = frozenset({'L0', 'L1', 'L8', 'R8'})


def _row_rank(row):
    """Sort key picking which CSV row of an observation represents the scene.

    Only matters for PMA. The on-disk ROI Studio working/ folder
    embeds PMA in its name and find_scene_folder() rebuilds that name from the
    stored PMA, so a scene whose stored PMA differs from its folder's has no
    findable .fits. The frames of one observation can disagree: PMA comes from
    ROVER_MOTION_COUNTER[3], and the left and right cameras sit at slightly
    different mast angles.

    ROI Studio takes PMA from the FIRST frame once sorted by SCLK, after
    discarding non-IOF products and the filters in _INVALID_FILTERS. Rank 
    the same way ROI Studio does: usable frames first, then earliest SCLK.
    Rows it would discard rank last rather than being dropped, so a scene
    whose every row is filtered out still imports.
    
    Mirrors sparc's scan_pcam_files() + load_cube(). If its ordering or
    filtering changes, this must change with it.
    """
    sclk = _to_int(row.get('SCLK'))
    usable = (
        (row.get('PRODUCT_TYPE') or '').strip().upper() == 'IOF'
        and (row.get('FILTER') or '').strip().upper() not in _INVALID_FILTERS
        and sclk is not None
    )
    return (0 if usable else 1, sclk if sclk is not None else float('inf'))


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
    (ROVER, SOL, SEQ_ID, obs_ix) group. All 33 CSV columns are stored from one
    representative row of each group.

    Re-running is incremental. For scene_keys that already exist, the rest of
    the row is left alone, except PMA. The on-disk ROI Studio working/ folder
    embeds PMA in its name and find_scene_folder() rebuilds that name from 
    the stored value, so the two must agree or the scene's .fits becomes
    unreachable. If the stored PMA doesn't match the representative row's,
    it's corrected here rather than requiring a full re-import."""

    path = Path(csv_path)
    if not path.exists():
        print(f"Error: CSV file '{csv_path}' does not exist.")
        sys.exit(1)

    existing_rows = conn.execute("SELECT scene_key, name, pma FROM scenes").fetchall()
    existing = {row[0] for row in existing_rows}
    existing_pma = {row[0]: (row[1], row[2]) for row in existing_rows}

    # First pass — group rows
    groups = {}      # scene_key -> representative row dict
    group_rank = {}  # scene_key -> that representative's selection rank
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
                print(f"  Warning: skipping row {total_rows}. Missing ROVER/SOL/SEQ_ID.")
                continue

            sol = _to_int(sol_str)
            if sol is None:
                print(f"  Warning: skipping row {total_rows}. Unparsable SOL '{sol_str}'.")
                continue
            if obs_ix is None:
                obs_ix = 0

            key = f"{rover}/sol{sol:04d}/{seq_id}/obs{obs_ix}"
            row_counts[key] = row_counts.get(key, 0) + 1
            rank = _row_rank(row)
            if key not in groups or rank < group_rank[key]:
                groups[key] = _scene_dict(key, rover, sol, seq_id, obs_ix, name, row)
                group_rank[key] = rank

    print(f"CSV: {total_rows} rows -> {len(groups)} unique scenes")
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
                    tau, rover_elevation, lon, lat,
                    updated_at
                ) VALUES (
                    :scene_key, :name, 0,
                    :fn, :rover, :sclk, :product_type, :site, :pos, :seq_id, :filter,
                    :version, :sol, :seq_ver, :lines, :pma, :obs_ix, :frame_type, :ltst,
                    :product_creation_time, :compression, :first_line, :first_sample,
                    :samples, :solar_elevation, :instrument_elevation, :instrument_azimuth,
                    :solar_azimuth, :incidence_angle, :emission_angle, :phase_angle,
                    :tau, :rover_elevation, :lon, :lat,
                    datetime('now')
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
        print(f"\n{len(pma_updates)} scene(s) have a PMA mismatch vs. the CSV. Correcting to match CSV:")
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
                    # fix_panel_names(), which repairs those).
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

    rename_folders() originally moved only the .fits/.sel when it renamed a
    folder, so every folder it touched still holds images under the
    pre-migration stem — e.g. Sol0007_p2530_PMA791_left_dcs.png inside
    Sol0007_p2530v1_PMA791_pancam_magic_carpet. Those folders look already
    migrated to rename_folders() and are skipped by it, so they need this pass.

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
        print(f"{rover}: {len(sol_dirs)} sol directories")
        for sol_dir in sol_dirs:
            working_dir = sol_dir / FolderKind.WORKING
            if not working_dir.exists():
                continue
            sol_copied = 0
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
                sol_copied += 1
            # Only sols with new files report, so a re-run stays quiet
            if sol_copied:
                rel = working_dir.relative_to(pancam_path)
                print(f"  {rel}: {sol_copied} copied", flush=True)

    print(f"Working files: {copied} copied, {skipped} already backed up (skipped)")


def audit_roi_names(conn, pancam_root):
    """Report ROI colour names in use, and scenes with no findable folder.

    A .fits carries either a current display name ('forest') or the older
    MERSpect key it replaced ('green-2'). Both resolve; one that resolves to
    nothing renders as a grey swatch with no warning.

    A scene past submission with no folder is not automatically broken: one
    with nothing to draw on never gets ROIs or a folder. Check its review notes.
    """
    from app.paths import find_scene_folder, kind_path, scene_file
    from app.roi_metadata import roi_color, _UNKNOWN_COLOR
    from app.fits_header import read_headers

    scenes = conn.execute("SELECT * FROM scenes ORDER BY id").fetchall()
    name_counts = {}
    name_samples = {}
    sel_without_fits = []
    no_folder = []
    scanned = 0

    for i, scene in enumerate(scenes):
        folder = find_scene_folder(pancam_root, scene)
        if not folder:
            no_folder.append(scene)
            continue
        fits = scene_file(folder, '.fits')
        if scene_file(folder, '.sel') and not fits:
            sel_without_fits.append((scene, folder))
        if not fits:
            continue
        try:
            headers = read_headers(fits)
        except (OSError, ValueError) as e:
            print(f"  unreadable: id={scene['id']} {scene['name']}: {e}")
            continue
        scanned += 1
        for h in headers:
            name = str(h.get('NAME') or '').strip()
            if not name:
                continue
            name_counts[name] = name_counts.get(name, 0) + 1
            name_samples.setdefault(name, [])
            if len(name_samples[name]) < 5:
                name_samples[name].append(scene['id'])
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(scenes)} scenes, {scanned} .fits read", flush=True)

    print(f"\nscenes {len(scenes)}, .fits read {scanned}, "
          f"distinct ROI names {len(name_counts)}")
    print(f".sel with no .fits: {len(sel_without_fits)} (should be 0)")

    print(f"\n{'name':<20} {'count':>7}  {'colour':<12} sample scene ids")
    for name, count in sorted(name_counts.items(), key=lambda kv: -kv[1]):
        verdict = 'UNRESOLVED' if roi_color(name) == _UNKNOWN_COLOR else 'ok'
        ids = ','.join(str(i) for i in name_samples[name])
        print(f"{name:<20} {count:>7}  {verdict:<12} {ids}")

    for scene, folder in sel_without_fits:
        print(f"  .sel only: id={scene['id']} {scene['name']} in {folder}")

    # Only scenes past submission are broken; the rest simply aren't drawn yet.
    submitted = [s for s in no_folder
                 if s['status'] not in (SceneStatus.UNCLAIMED, SceneStatus.CLAIMED,
                                        SceneStatus.ISSUES)]
    print(f"\nno findable folder: {len(no_folder)} scene(s), "
          f"{len(submitted)} of them past submission")
    for s in submitted:
        working = kind_path(pancam_root, s['rover'], s['sol'], FolderKind.WORKING)
        seq = (s['seq_id'] or '').lower()
        ver = f"v{s['seq_ver']}" if s['seq_ver'] is not None else ''
        print(f"  id={s['id']} status={s['status']} {s['name']}")
        print(f"    expected Sol{s['sol']:04d}_{seq}{ver}_PMA{s['pma']}* in {working}")


# Mirrors ROI Studio's _save_annotated() styling. Close, not identical.
_LABEL_DPI     = 150
_LABEL_FIG_IN  = (12, 9)
_LABEL_FS      = 8
_LABEL_PAD     = 0.2
_LABEL_BOX     = (20 / 255, 20 / 255, 20 / 255)
_LABEL_ALPHA   = 200 / 255
_LABEL_EDGE_W  = 1.5


def _roi_mask_boxes(fits_path, eye):
    """[(name, (x, y, w, h)), ...] and (w, h) of the mask, for one eye.

    One union-mask HDU per ROI per eye, so a box is that mask's non-zero
    bounding box. Two blobs give one box spanning both."""
    import numpy as np
    from app.fits_header import iter_hdus

    boxes, dims = [], None
    for header, data in iter_hdus(fits_path):
        name = str(header.get('NAME') or '').strip()
        if not name or str(header.get('EYE', '')).strip().lower() != eye:
            continue
        w, h = header.get('NAXIS1'), header.get('NAXIS2')
        if not (isinstance(w, int) and isinstance(h, int)):
            continue
        if abs(header.get('BITPIX', 8)) != 8 or len(data) < w * h:
            continue
        mask = np.frombuffer(data[:w * h], dtype=np.uint8).reshape(h, w)
        rows = np.flatnonzero(mask.any(axis=1))
        cols = np.flatnonzero(mask.any(axis=0))
        if not rows.size or not cols.size:
            continue
        boxes.append((name, (int(cols[0]), int(rows[0]),
                             int(cols[-1] - cols[0]) + 1, int(rows[-1] - rows[0]) + 1)))
        dims = (w, h)
    return boxes, dims


def _panel_box(box, factor):
    """A mask-space box in panel pixels. Mask rows run top-down like the
    panel, so the two differ only by a uniform scale."""
    x, y, w, h = box
    return (x * factor, y * factor, w * factor, h * factor)


def label_scene_panel(folder, fits_path, suffix, eye):
    """Write the ROI-labelled twin of one panel. Returns its path, or None.

    None means no panel or no masks for that eye, which is a skip, not a
    failure: not every observation captured both eyes."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.image as mpimg
    import matplotlib.patches as mpatches
    import matplotlib.pyplot as plt
    import numpy as np
    from app.paths import find_panel
    from app.roi_metadata import roi_color

    panel = find_panel(folder, suffix)
    if not panel:
        return None
    boxes, dims = _roi_mask_boxes(fits_path, eye)
    if not boxes or not dims:
        return None

    img = mpimg.imread(panel)
    if img.dtype != np.uint8:
        img = (img * 255).astype(np.uint8)
    factor = img.shape[1] / dims[0]
    fig, ax = plt.subplots(figsize=_LABEL_FIG_IN, dpi=_LABEL_DPI)
    fig.patch.set_facecolor('black')
    ax.set_facecolor('black')
    ax.imshow(img)
    ax.axis('off')
    for name, box in boxes:
        x, y, w, h = _panel_box(box, factor)
        color = roi_color(name)
        ax.add_patch(mpatches.Rectangle((x, y), w, h, linewidth=_LABEL_EDGE_W,
                                        edgecolor=color, facecolor='none'))
        pad_pts = _LABEL_FS * _LABEL_PAD
        ax.annotate(name, xy=(x, y), xytext=(pad_pts, pad_pts),
                    textcoords='offset points', color=color, fontfamily='Arial',
                    fontsize=_LABEL_FS, horizontalalignment='left',
                    verticalalignment='bottom', clip_on=False, annotation_clip=False,
                    bbox={'boxstyle': f'square,pad={_LABEL_PAD}',
                          'facecolor': _LABEL_BOX, 'edgecolor': 'none',
                          'alpha': _LABEL_ALPHA})

    out = os.path.splitext(panel)[0] + '_with_roi_names.png'
    fig.savefig(out, bbox_inches='tight', pad_inches=0, dpi=_LABEL_DPI)
    plt.close(fig)
    return out


def label_panels(conn, pancam_root, scene=None):
    """Backfill the ROI-labelled RGB panel for saves made before ROI Studio
    started writing one. Right eye only, matching the summary slide.

    Safe to re-run and interrupt: a scene that already has one is skipped."""
    from app.paths import Panel, find_scene_folder, find_panel, scene_file

    rows = conn.execute("SELECT * FROM scenes ORDER BY id").fetchall()
    if scene:
        rows = [r for r in rows
                if str(r['id']) == str(scene) or r['name'] == scene]
        if not rows:
            _fail(f"no scene matching '{scene}'.")

    written = skipped = no_folder = 0

    for s in rows:
        folder = find_scene_folder(pancam_root, s)
        if not folder:
            no_folder += 1
            continue
        fits = scene_file(folder, '.fits')
        if not fits:
            no_folder += 1
            continue
        if find_panel(folder, Panel.RIGHT_RGB_NAMED):
            skipped += 1
            continue
        out = label_scene_panel(folder, fits, Panel.RIGHT_RGB, 'right')
        if out is None:
            skipped += 1
            continue
        written += 1
        print(f"  {s['name']}: {out}", flush=True)

    print(f"\n{written} labelled, {skipped} already had one or nothing to do, "
            f"{no_folder} without a folder or .fits")


def build_summary_slides(conn, pancam_root, statuses, dry_run=False, force=False):
    """Build summary slides for every scene in the given statuses.

    Backfill for scenes that reached a supervisor before slides existed. Going
    forward the dashboards build one as a scene enters supervisor review, so
    this only needs running once (and again after fixing a broken folder).

    Safe to re-run and safe to interrupt: a scene whose slide is already newer
    than everything it was built from is skipped, so a second run picks up
    where the last one stopped. A scene with an incomplete folder is reported
    and the run continues -- collecting those into one list at the end is the
    point, since they need a human to re-save them.
    """
    from app.paths import find_scene_folder, scene_file
    from app.slides import build_summary_slide, missing_panels, slide_is_current

    scenes = conn.execute(
        "SELECT * FROM scenes WHERE status IN (%s) ORDER BY rover, sol, seq_id"
        % ','.join('?' * len(statuses)),
        tuple(statuses),
    ).fetchall()

    total = len(scenes)
    labels = ', '.join(SceneStatus.LABELS.get(s, str(s)) for s in sorted(statuses))
    print(f"{total} scene(s) in status: {labels}\n")

    built = skipped = 0
    problems = []
    gapped = []
    for i, scene in enumerate(scenes, 1):
        prefix = f"[{i}/{total}  {i / total * 100:5.1f}%]  {scene['name']}"
        try:
            if not force and slide_is_current(pancam_root, scene):
                skipped += 1
                if i % 25 == 0 or i == total:
                    print(f"{prefix}  (up to date)")
                continue

            # Resolved up front so the dry run reports exactly what a real run
            # would hit -- the point of the preview is to surface the scenes
            # needing a human before committing half an hour of rendering.
            folder = find_scene_folder(pancam_root, scene)
            if folder is None:
                raise FileNotFoundError("no ROI Studio folder found - nothing saved for it yet")
            if scene_file(folder, '.fits') is None:
                raise FileNotFoundError(f"{os.path.basename(folder)} has no .fits file")
            gaps = missing_panels(folder)
            if gaps:
                gapped.append((scene['name'], SceneStatus.LABELS.get(scene['status']),
                                os.path.basename(folder), gaps))

            note = "  [dry run] would build" if dry_run else "  built"
            if gaps:
                note += f" (placeholder for: {', '.join(gaps)})"
            if not dry_run:
                build_summary_slide(pancam_root, scene, folder=folder)
            print(f"{prefix}{note}")
            built += 1
        except (FileNotFoundError, OSError, ValueError) as e:
            problems.append((scene['name'], SceneStatus.LABELS.get(scene['status']), str(e)))
            print(f"{prefix}  SKIPPED - {e}")

    verb = "would build" if dry_run else "built"
    print(f"\n{built} {verb}, {skipped} already up to date, {len(problems)} could not be built")
    if gapped:
        # Not failures: a panel the source observation never had the filters to
        # produce is permanent, and re-saving will not change it. Listed so a
        # human can tell those apart from a genuinely interrupted save.
        print(f"\n{len(gapped)} slide(s) built with a placeholder for a missing panel:")
        for name, status, folder, gaps in gapped:
            print(f"  {name}  [{status}]  {folder}")
            print(f"     no {', '.join(gaps)}")
    if problems:
        print("\nScenes that could not be built at all:")
        for name, status, err in problems:
            print(f"  {name}  [{status}]")
            print(f"     {err}")


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

def _fail(msg):
    print(f"Error: {msg}")
    sys.exit(1)


def _pancam_root(args):
    """Resolve and validate --path, exiting if it is unset or missing."""
    if not args.path:
        _fail("no Pancam path. Pass --path or set PANCAM_PATH in config.py.")
    if not Path(args.path).exists():
        _fail(f"'{args.path}' does not exist.")
    return args.path


@contextmanager
def _db():
    """Open a migrated DB connection, closing it when the command finishes."""
    initialize_db()
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()


def _statuses(raw):
    """Parse a --status list, exiting unless every entry is a real status."""
    try:
        values = [int(s) for s in raw.split(',') if s.strip()]
    except ValueError:
        _fail(f"--status must be a comma-separated list of integers, got '{raw}'.")
    unknown = [s for s in values if s not in SceneStatus.LABELS]
    if not values or unknown:
        _fail(f"--status has no valid status values ({unknown or 'empty'}).")
    return values


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_import(args):
    with _db() as conn:
        if args.csv:
            import_scenes_from_csv(conn, args.csv, dry_run=args.dry_run)
        else:
            import_scenes_from_folders(conn, _pancam_root(args), dry_run=args.dry_run)


def cmd_wipe(args):
    print("WARNING: this deletes all scenes and reviews from the database.")
    print("Users will not be affected. This cannot be undone.")
    if input("Type YES to continue: ").strip() != "YES":
        print("Cancelled.")
        return
    with _db() as conn:
        wipe_scenes(conn)


def cmd_backup(args):
    root = _pancam_root(args)
    if not DB_PATH:
        _fail("backup requires DB_PATH in config.py.")
    backup_scenes(root, DB_PATH)


def cmd_audit_roi_names(args):
    root = _pancam_root(args)
    with _db() as conn:
        audit_roi_names(conn, root)


def cmd_label_panels(args):
    root = _pancam_root(args)
    with _db() as conn:
        label_panels(conn, root, scene=args.scene)


def cmd_build_slides(args):
    root = _pancam_root(args)
    statuses = _statuses(args.status)
    with _db() as conn:
        build_summary_slides(conn, root, statuses, dry_run=args.dry_run, force=args.force)


def cmd_copy_approved(args):
    root = _pancam_root(args)
    with _db() as conn:
        copy_approved_fits(conn, root, dry_run=args.dry_run)


def cmd_build_folders(args):
    build_folders(_pancam_root(args), args.subfolder)


def cmd_restructure_folders(args):
    restructure_folders(_pancam_root(args), dry_run=args.dry_run)


def cmd_rename_folders(args):
    root = _pancam_root(args)
    with _db() as conn:
        rename_folders(conn, root, dry_run=args.dry_run)


def cmd_fix_panel_names(args):
    fix_panel_names(_pancam_root(args), dry_run=args.dry_run)


# ── Parser ────────────────────────────────────────────────────────────────────

def build_parser():
    # Shared options, mixed into only the commands that honor them -- a command
    # that ignores --dry-run should not advertise it.
    path_opt = argparse.ArgumentParser(add_help=False)
    path_opt.add_argument(
        "--path",
        default=PANCAM_PATH,
        metavar="DIR",
        help="Root Pancam folder holding MERA/ and MERB/ (default: PANCAM_PATH in config.py).",
    )
    preview_opt = argparse.ArgumentParser(add_help=False)
    preview_opt.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )

    parser = argparse.ArgumentParser(
        prog="setup/util.py",
        description="ROVR maintenance toolbox: scene import, R:\\ drive upkeep, migrations.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("import", parents=[path_opt, preview_opt],
                        help="Import scenes from a CSV observation table.")
    p.add_argument("csv", nargs="?", metavar="CSV",
                    help="CSV observation table. Omit to fall back to scanning "
                        "<path> for IOF .IMG files, which populates far fewer columns.")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("wipe", help="Delete all scenes and reviews. Users are not affected.")
    p.set_defaults(func=cmd_wipe)

    p = sub.add_parser("backup", parents=[path_opt],
                        help="Back up the database and both rovers' working/ trees (.fits and .sel).")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("audit-roi-names", parents=[path_opt],
                        help="Report ROI colour names in use and scenes with no findable folder.")
    p.set_defaults(func=cmd_audit_roi_names)

    p = sub.add_parser("label-panels", parents=[path_opt],
                        help="Backfill the ROI-labelled right-eye RGB panel for older saves.")
    p.add_argument("--scene", metavar="ID_OR_NAME",
                    help="Only this scene, by id or name. Omit to do the whole archive.")
    p.set_defaults(func=cmd_label_panels)

    p = sub.add_parser("build-slides", parents=[path_opt, preview_opt],
                        help="Build summary slides for scenes that reached a supervisor before slides existed.")
    p.add_argument("--status", default="5,6,7", metavar="LIST",
                    help="Comma-separated statuses to build for (default: 5,6,7 -- what a supervisor sees).")
    p.add_argument("--force", action="store_true",
                    help="Rebuild every slide, even ones already up to date.")
    p.set_defaults(func=cmd_build_slides)

    p = sub.add_parser("copy-approved", parents=[path_opt, preview_opt],
                        help="Copy each approved scene's latest .fits into <path>/ready_for_asdf.")
    p.set_defaults(func=cmd_copy_approved)

    p = sub.add_parser("build-folders", parents=[path_opt],
                        help="Ensure a subfolder exists under every rover/#### directory.")
    p.add_argument("--subfolder", default=FolderKind.WORKING, metavar="NAME",
                    help=f"Subfolder to create (default: {FolderKind.WORKING}).")
    p.set_defaults(func=cmd_build_folders)

    # One-off migrations, kept behind their own group so the routine commands
    # above stay readable. All three are safe to re-run.
    mig_parser = sub.add_parser("migrate", help="One-off R:\\ drive folder migrations.")
    mig = mig_parser.add_subparsers(dest="migration", metavar="migration")
    mig_parser.set_defaults(func=lambda _args, p=mig_parser: p.print_help())

    p = mig.add_parser("restructure-folders", parents=[path_opt, preview_opt],
                        help="Move rover/<kind>/solNNNN into rover/NNNN/<kind>.")
    p.set_defaults(func=cmd_restructure_folders)

    p = mig.add_parser("rename-folders", parents=[path_opt, preview_opt],
                        help="Rename working/ ROI folders and their files to Sol####_p####v#_PMA#_<NAME>.")
    p.set_defaults(func=cmd_rename_folders)

    p = mig.add_parser("fix-panel-names", parents=[path_opt, preview_opt],
                        help="Rename .png panels left behind by an earlier rename-folders run.")
    p.set_defaults(func=cmd_fix_panel_names)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
