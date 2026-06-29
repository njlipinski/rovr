#!/bin/bash
# ROVR Launcher — auto-updates from the R drive then launches ROVR.
#
# Setup: place this file in the same folder as rovr.app and config.py.
# If your R drive is mounted at a different path, change PANCAM_PATH below.

PANCAM_PATH="/Volumes/Research/Rice/Pancam"

# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$SCRIPT_DIR/rovr.app"
VER_FILE="$SCRIPT_DIR/.rovr-version"

if [ ! -d "$APP" ]; then
    osascript -e 'display alert "ROVR not found" message "Place launch_rovr.command in the same folder as rovr.app."'
    exit 1
fi

# Read locally installed version
LOCAL_VER="0.0.0"
[ -f "$VER_FILE" ] && LOCAL_VER=$(cat "$VER_FILE")

# Check R drive for latest version; skip update if R drive not mounted
if [ ! -f "$PANCAM_PATH/rovr-version.txt" ]; then
    echo "R drive not accessible — launching current version."
    open "$APP"
    exit 0
fi
LATEST_VER=$(tr -d '[:space:]' < "$PANCAM_PATH/rovr-version.txt")

# Returns 0 (true) if $1 > $2 as a semver
version_gt() {
    local IFS='.' a b
    local -a v1=($1) v2=($2)
    for i in 0 1 2; do
        a=${v1[$i]:-0}; b=${v2[$i]:-0}
        ((a > b)) && return 0
        ((a < b)) && return 1
    done
    return 1
}

if ! version_gt "$LATEST_VER" "$LOCAL_VER"; then
    open "$APP"
    exit 0
fi

# Newer version available — copy from R drive
echo "Updating ROVR $LOCAL_VER → $LATEST_VER..."

SOURCE_APP="$PANCAM_PATH/rovr.app"
if [ ! -d "$SOURCE_APP" ]; then
    echo "Mac build not found on R drive ($SOURCE_APP)."
    echo "Deploy the latest Mac build, then relaunch."
    open "$APP"
    exit 0
fi

rm -rf "$APP"
if rsync -a "$SOURCE_APP" "$SCRIPT_DIR/"; then
    # Clear Gatekeeper quarantine so macOS doesn't block the updated app
    xattr -r -d com.apple.quarantine "$APP" 2>/dev/null
    echo "$LATEST_VER" > "$VER_FILE"
    echo "Updated to $LATEST_VER."
else
    echo "Copy failed — check R drive connection. Launching current version."
fi

echo "Launching ROVR..."
open "$APP"