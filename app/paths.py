# app/paths.py
"""Filesystem path helpers for the Pancam folder layout on PANCAM_PATH.

Layout: PANCAM_PATH/<rover>/####/<kind>/  where #### is the zero-padded sol
number and <kind> is one of FolderKind's members (iof, edr, practice, working).
"""
import os
import re


class FolderKind:
    IOF      = 'iof'
    EDR      = 'edr'
    PRACTICE = 'practice'
    WORKING  = 'working'

    ALL = (IOF, EDR, PRACTICE, WORKING)


class Panel:
    """File-name suffixes for the per-scene images ROI Studio writes beside the
    .fits/.sel every time an analyst saves. Only three of the five go on a
    summary slide (see app/slides.py); the other two are listed because they
    share the convention and are what a caller would reach for to change the
    slide's panel choice."""
    LEFT_DCS  = '_left_dcs.png'
    LEFT_RGB  = '_left_rgb.png'
    RIGHT_DCS = '_right_dcs.png'
    RIGHT_RGB = '_right_rgb.png'
    SPECTRA   = '_spectra.png'

    ALL = (LEFT_DCS, LEFT_RGB, RIGHT_DCS, RIGHT_RGB, SPECTRA)


# Master collection of every scene's summary slide, laid out as
# PANCAM_PATH/summary_slides/<rover>/<name>. Each slide is also written beside
# its own source images; the file name is the same in both places.
# PDF keeps the ROI metadata table as vector text — sharp at any zoom and
# selectable — and lets a scene with more ROIs than fit continue on a second
# page. Changing SUMMARY_FORMAT and SUMMARY_SUFFIX together is all it takes to
# switch the slide to PNG.
SUMMARY_DIR    = 'summary_slides'
SUMMARY_FORMAT = 'pdf'
SUMMARY_SUFFIX = '_summary.pdf'


def sol_dir_name(sol):
    """Zero-padded sol folder name (no 'sol' prefix), e.g. sol_dir_name(21) -> '0021'."""
    return f"{int(sol):04d}"


def sol_path(pancam_path, rover, sol):
    """PANCAM_PATH/<rover>/####"""
    return os.path.join(pancam_path, rover, sol_dir_name(sol))


def kind_path(pancam_path, rover, sol, kind):
    """PANCAM_PATH/<rover>/####/<kind>"""
    return os.path.join(sol_path(pancam_path, rover, sol), kind)


# Trailing ROI Studio folder "revision" tag — unrelated to SEQ_VER, just a
# manual re-save marker analysts append to a folder name.
_REVISION_TAG_RE = re.compile(r'^(.+)_v(\d+)$', re.IGNORECASE)


def versionless_name(folder_name):
    """Strip a trailing '_v#' revision tag from an ROI Studio folder name.

    The files inside a folder are named after the folder with this tag removed,
    so this is the stem to build file names from — never the raw folder name."""
    m = _REVISION_TAG_RE.match(folder_name)
    return m.group(1) if m else folder_name


def folder_version(folder_name):
    """The revision number a folder's trailing '_v#' tag carries.

    An untagged folder is the first save, so it ranks as 1 — the same as an
    explicit '_v1', which is why ranking needs a tiebreak below it."""
    m = _REVISION_TAG_RE.match(folder_name)
    return int(m.group(2)) if m else 1


# A current-convention folder carries a free-form _<NAME> after the PMA field;
# the legacy conventions stop at the PMA. The distinction matters for ranking:
# --rename-folders converts legacy folders to the current form, so any legacy
# folder still sitting beside a current one is a straggler it skipped, and its
# revision numbers belong to a different, older sequence than the current
# folder's. Comparing the two sequences' numbers is meaningless, so convention
# outranks version.
_CURRENT_CONVENTION_RE = re.compile(r'^Sol\d{4}_p\d{4}(?:v\d+)?_PMA\d+_.+$', re.IGNORECASE)

# The artifacts that mark a directory as a real ROI Studio save, in the order
# their timestamps are trusted for the tiebreak.
_SAVE_ARTIFACTS = ('.fits', '.sel')


def _matching_folders(pancam_path, scene):
    """Every working/ folder that belongs to this scene, as (path, versionless)."""
    rover   = scene['rover']
    sol     = scene['sol']
    seq_id  = scene['seq_id']
    seq_ver = scene['seq_ver']
    pma     = scene['pma']
    if None in (rover, sol, seq_id, pma):
        return []

    sol_dir = kind_path(pancam_path, rover, sol, FolderKind.WORKING)
    if not os.path.isdir(sol_dir):
        return []

    # ROI Studio folder names have gone through three conventions, all of which
    # may additionally carry a trailing "_v#" revision tag on the FOLDER only:
    #   base_name                          (original)
    #   base_name_v#                       (original, revised)
    #   base_name_NAME                     (current "stable" — NAME is free-form)
    #   base_name_NAME_v#                  (current, revised)
    # The files inside a folder are always that folder's own name with the
    # trailing "_v#" stripped — never reconstructed independently.
    def scan(is_match):
        found = []
        for entry in os.scandir(sol_dir):
            if not entry.is_dir():
                continue
            versionless = versionless_name(entry.name)
            if not is_match(versionless):
                continue
            # A directory only counts as a save if it actually holds one.
            if any(os.path.isfile(os.path.join(entry.path, versionless + ext))
                   for ext in _SAVE_ARTIFACTS):
                found.append((entry.path, versionless))
        return found

    # Whether SEQ_VER is folded into the name depends on which ROI Studio
    # convention was in effect at that particular save, not on whether the DB
    # happens to have a seq_ver for this scene — a scene can pick up a seq_ver
    # later while its on-disk folders never had it embedded, and a
    # folder-scan import never sets one at all.
    #
    # Both the exact-name and the seq_ver-agnostic match run, and their results
    # are unioned. Trying the strict pass alone first (as this did) meant that
    # for a scene with no seq_ver the only match was the legacy folder, and the
    # current-convention folder sitting right beside it was never even
    # considered — which is the opposite of preferring the current convention.
    # Both patterns are already pinned to this sol, seq and PMA, so the union
    # cannot pull in another scene's folder.
    seq_id_lower = seq_id.lower()
    strict_names = {f"Sol{sol:04d}_{seq_id_lower}_PMA{pma}"}
    if seq_ver is not None:
        strict_names.add(f"Sol{sol:04d}_{seq_id_lower}v{seq_ver}_PMA{pma}")
    wildcard_re = re.compile(
        rf'^Sol{sol:04d}_{re.escape(seq_id_lower)}(?:v\d+)?_PMA{pma}(?:_.*)?$'
    )

    return scan(lambda v: (
        v in strict_names
        or any(v.startswith(b + '_') for b in strict_names)
        or bool(wildcard_re.match(v))
    ))


def _folder_rank(path, versionless):
    """Sort key deciding which of a scene's folders is the current one.

    Newest is a question about revision history, not about file timestamps: a
    migration, a backup restore, or an analyst reopening an old folder all move
    mtimes without producing a newer save, and some multi-folder
    scenes on the drive have the two orderings disagreeing. So version leads,
    and mtime only breaks a genuine tie (an untagged folder against an explicit
    _v1). Convention outranks both — see _CURRENT_CONVENTION_RE."""
    mtimes = [
        os.path.getmtime(os.path.join(path, versionless + ext))
        for ext in _SAVE_ARTIFACTS
        if os.path.isfile(os.path.join(path, versionless + ext))
    ]
    return (
        bool(_CURRENT_CONVENTION_RE.match(versionless)),
        folder_version(os.path.basename(path)),
        max(mtimes, default=0.0),
    )


def find_scene_folder(pancam_path, scene):
    """Return the one folder holding this scene's current save, or None.

    This is the single place a scene's folder is chosen. Everything else —
    .fits, .sel, the panel images, the summary slide — is derived from what
    this returns, so a scene can never be read out of two folders at once.
    Never rank folders anywhere else."""
    folders = _matching_folders(pancam_path, scene)
    if not folders:
        return None
    path, _versionless = max(folders, key=lambda f: _folder_rank(*f))
    return path


def scene_file(folder, ext):
    """The path to one of a folder's own files, or None if it isn't there."""
    if not folder:
        return None
    path = os.path.join(folder, versionless_name(os.path.basename(folder)) + ext)
    return path if os.path.isfile(path) else None


def find_scene_file(pancam_path, scene, ext):
    """Return this scene's file with the given extension, or None.

    Resolves the scene's folder first and reads only from there — it never
    picks a file from a different folder than its siblings came from."""
    return scene_file(find_scene_folder(pancam_path, scene), ext)


def find_sel_file(pancam_path, scene):
    """Return the .sel from this scene's current folder, or None."""
    return find_scene_file(pancam_path, scene, '.sel')


def find_fits_file(pancam_path, scene):
    """Return the .fits from this scene's current folder, or None."""
    return find_scene_file(pancam_path, scene, '.fits')


def find_panel(folder, suffix):
    """Return the path to one of ROI Studio's per-scene images inside `folder`, or None.

    Normally the file is the folder's own (revision-tag-stripped) name plus the
    suffix. The fallback glob covers folders that predate --fix-panel-names,
    where an early --rename-folders run moved the .fits/.sel but left the
    images on the old stem. It only ever looks inside `folder`, so it cannot
    reach across into another revision."""
    if not folder or not os.path.isdir(folder):
        return None
    exact = os.path.join(folder, versionless_name(os.path.basename(folder)) + suffix)
    if os.path.isfile(exact):
        return exact
    matches = [
        os.path.join(folder, n) for n in os.listdir(folder)
        if n.lower().endswith(suffix.lower())
    ]
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def summary_slide_paths(pancam_path, scene, folder):
    """Return (beside_sources, master) paths for this scene's summary slide.

    The slide is written twice under one name: next to the images it was built
    from, and into the master collection at summary_slides/<rover>/. The master
    copy is filed under a rover subfolder because the stem carries sol, seq and
    PMA but not the rover — MERA and MERB sol 0007 would otherwise collide in a
    flat directory."""
    name = versionless_name(os.path.basename(folder)) + SUMMARY_SUFFIX
    master_dir = os.path.join(pancam_path, SUMMARY_DIR, scene['rover'])
    return os.path.join(folder, name), os.path.join(master_dir, name)
