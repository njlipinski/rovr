# app/paths.py
"""Filesystem path helpers for the Pancam folder layout on PANCAM_PATH.

Layout: PANCAM_PATH/<rover>/####/<kind>/  where #### is the zero-padded sol
number and <kind> is one of FolderKind's members (iof, edr, practice, working).
"""
import os


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
