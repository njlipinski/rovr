# config.example.py
"""configuration settings for ROVR"""

# Copy this file to config.py beside rovr.exe (Windows) or rovr.app (Mac).
# Do not commit config.py to version control.

import os
import sys

if sys.platform == 'win32':
    PANCAM_PATH = r"R:\Rice\Pancam"
else:
    PANCAM_PATH = "/Volumes/Research/Rice/Pancam"

DB_PATH = os.path.join(PANCAM_PATH, "rovr.sqlite")
