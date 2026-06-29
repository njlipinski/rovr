# config.example.py
"""configuration settings for ROVR"""

# Copy this file to config.py beside rovr.exe on each machine.
# Do not commit config.py to version control.
#
# Local install (recommended): users run ROVR from their own machine.
# Set PANCAM_PATH to the mounted R:\Rice\Pancam folder explicitly:
#
#   PANCAM_PATH = r"R:\Rice\Pancam"           # Windows
#   PANCAM_PATH = "/Volumes/Research/Rice/Pancam"  # Mac
#
# Running directly from R:\Rice\Pancam: the defaults below work as-is
# since _BASE resolves to the folder containing config.py.

import os
_BASE = os.path.dirname(os.path.abspath(__file__))

# SQLite database file on the R drive.
DB_PATH = os.path.join(_BASE, "rovr.sqlite")

# Root Pancam directory on the R drive. Contains rovr.exe, rovr-version.txt,
# rovr.sqlite, and rover subfolders (MERA/, MERB/) with iof/sol####/ imagery.
PANCAM_PATH = _BASE
