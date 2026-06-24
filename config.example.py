# config.example.py
"""configuration settings for ROVR"""

# Copy this file to config.py — no edits needed if rovr.exe and config.py
# are in the same folder in R:\Rice\Pancam (the default setup).
# Do not commit config.py to version control.

import os
_BASE = os.path.dirname(os.path.abspath(__file__))

# SQLite database file, beside rovr.exe on the Rice drive.
DB_PATH = os.path.join(_BASE, "rovr.sqlite")

# Root Pancam directory — the folder containing rovr.exe on the Rice drive.
# The importer expects rover subfolders (MERA, MERB) directly inside this path,
# each containing an iof/ subdirectory with sol#### folders.
PANCAM_PATH = _BASE
