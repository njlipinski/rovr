# config.example.py
"""configuration settings for ROVR"""

# Copy this file to config.py and update the paths for your machine.
# Do not commit config.py to version control.

# Path to the SQLite database file — lives at the Pancam level, one folder
# above the rover directories (MERA / MERB).
DB_PATH = r"R:\Rice\Pancam\rovr.sqlite"

# Root Pancam directory on the Rice drive.
# The importer expects rover subfolders (MERA, MERB) directly inside this path,
# each containing an iof/ subdirectory with sol#### folders.
PANCAM_PATH = r"R:\Rice\Pancam"

# Path to ROI Studio executable.
ROI_STUDIO_PATH = r"C:\Program Files\ROI Studio\ROI Studio.exe"
