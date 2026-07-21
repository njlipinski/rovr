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


def find_scene_file(pancam_path, scene, ext):
    """Return the path to the most recent file with the given extension (e.g.
    '.sel', '.fits') for this scene under working/, or None."""
    rover   = scene['rover']
    sol     = scene['sol']
    seq_id  = scene['seq_id']
    seq_ver = scene['seq_ver']
    pma     = scene['pma']
    if None in (rover, sol, seq_id, pma):
        return None

    sol_dir = kind_path(pancam_path, rover, sol, FolderKind.WORKING)
    if not os.path.isdir(sol_dir):
        return None

    # ROI Studio folder names have gone through three conventions, all of which
    # may additionally carry a trailing "_v#" revision tag on the FOLDER only:
    #   base_name                          (original)
    #   base_name_v#                       (original, revised)
    #   base_name_NAME                     (current "stable" — NAME is free-form)
    #   base_name_NAME_v#                  (current, revised)
    # The file inside a folder is always that folder's own name with the
    # trailing "_v#" stripped — never reconstructed independently — so we
    # derive the expected file name per-folder instead of assuming base_name.
    def scan(is_match):
        candidates = []
        for entry in os.scandir(sol_dir):
            if not entry.is_dir():
                continue
            m = _REVISION_TAG_RE.match(entry.name)
            versionless = m.group(1) if m else entry.name
            if not is_match(versionless):
                continue
            file_path = os.path.join(entry.path, versionless + ext)
            if os.path.isfile(file_path):
                candidates.append(file_path)
        return candidates

    # Whether SEQ_VER is folded into the name depends on which ROI Studio
    # convention was in effect at the time of that particular save, not on
    # whether the DB happens to have a seq_ver value for this scene — a
    # scene can pick up a seq_ver later while its on-disk folders (saved
    # under an older convention) never had it embedded. Try the strict
    # match (bare, plus the DB's seq_ver if set) first: it covers the
    # common case and keeps a useful signal (no match found) when DB and
    # disk genuinely disagree about which scene a folder belongs to. Only
    # fall back to a seq_ver-agnostic wildcard if the strict match finds
    # nothing.
    seq_id_lower = seq_id.lower()
    strict_names = {f"Sol{sol:04d}_{seq_id_lower}_PMA{pma}"}
    if seq_ver is not None:
        strict_names.add(f"Sol{sol:04d}_{seq_id_lower}v{seq_ver}_PMA{pma}")

    candidates = scan(lambda v: v in strict_names or any(
        v.startswith(b + '_') for b in strict_names
    ))

    if not candidates:
        wildcard_re = re.compile(
            rf'^Sol{sol:04d}_{re.escape(seq_id_lower)}(?:v\d+)?_PMA{pma}(?:_.*)?$'
        )
        candidates = scan(lambda v: bool(wildcard_re.match(v)))

    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def find_sel_file(pancam_path, scene):
    """Return the path to the most recent .sel file for this scene under working/, or None."""
    return find_scene_file(pancam_path, scene, '.sel')


def find_fits_file(pancam_path, scene):
    """Return the path to the most recent .fits file for this scene under working/, or None."""
    return find_scene_file(pancam_path, scene, '.fits')
