#!/bin/bash
# ROVR Launcher -- auto-updates from the R drive then launches ROVR.
#
# Setup: place this file in the same folder as config.py. On first run it will
# install rovr.app for you from the R drive, so you do not need to copy the app
# yourself.
#
# If your R drive is mounted at a different path, change PANCAM_PATH below.

PANCAM_PATH="/Volumes/Research/Rice/Pancam"

# ----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$SCRIPT_DIR/rovr.app"
VER_FILE="$SCRIPT_DIR/.rovr-version"
TMP="$SCRIPT_DIR/.rovr-update.tmp"

# ROVR's files live in PANCAM_PATH/ROVR, keeping them out of the scene data at
# the Pancam root. Older layouts kept them at the root, so fall back there.
# The version file is the marker for a fully staged directory (the deploy script
# writes it last), so its presence is what makes a candidate valid.
ROVR_DIR="$PANCAM_PATH/ROVR"
if [ ! -f "$ROVR_DIR/rovr-version.txt" ]; then
    ROVR_DIR="$PANCAM_PATH"
fi
VERSION_SRC="$ROVR_DIR/rovr-version.txt"

# uname -m reports arm64 on Apple Silicon and x86_64 on Intel, which is exactly
# how the build names its zips. An arm64 app will not launch on an Intel Mac and
# vice versa, so this has to pick the right one.
ARCH="$(uname -m)"
ZIP="$ROVR_DIR/rovr-mac-$ARCH.zip"

# Pre-arch-aware drive layout: an unzipped bundle, always at the Pancam root and
# always arm64, so it is only ever a usable fallback on Apple Silicon.
LEGACY_APP="$PANCAM_PATH/rovr.app"

trap 'rm -rf "$TMP"' EXIT

alert() {
    osascript -e "display alert \"ROVR\" message \"$1\"" >/dev/null 2>&1
}

# Expand a zipped bundle from the drive and swap it in.
# ditto (not unzip) because it restores executable bits and symlinks, which the
# drive may not have preserved.
install_from_zip() {
    local src="$1"
    rm -rf "$TMP"
    mkdir -p "$TMP" || return 1

    if ! ditto -x -k "$src" "$TMP" 2>/dev/null; then
        echo "Could not expand $src."
        return 1
    fi
    if [ ! -d "$TMP/rovr.app" ]; then
        echo "$src did not contain rovr.app."
        return 1
    fi

    rm -rf "$APP"
    # Same volume as the temp dir, so this is a rename rather than a copy.
    mv "$TMP/rovr.app" "$APP" || return 1

    # Belt and braces in case the drive round trip dropped the mode bits.
    chmod +x "$APP/Contents/MacOS/"* 2>/dev/null
    # Clear Gatekeeper quarantine so macOS does not block the app.
    xattr -r -d com.apple.quarantine "$APP" 2>/dev/null
    return 0
}

# Copy the legacy unzipped bundle. Apple Silicon only -- see LEGACY_APP above.
install_from_legacy() {
    if [ "$ARCH" != "arm64" ]; then
        return 1
    fi
    [ -d "$LEGACY_APP" ] || return 1

    rm -rf "$APP"
    rsync -a "$LEGACY_APP" "$SCRIPT_DIR/" || return 1
    chmod +x "$APP/Contents/MacOS/"* 2>/dev/null
    xattr -r -d com.apple.quarantine "$APP" 2>/dev/null
    return 0
}

no_build_message() {
    if [ "$ARCH" = "x86_64" ]; then
        echo "No Intel build on the R drive (looked for $ZIP)."
        echo "Ask for rovr-mac-x86_64.zip to be deployed, or download it from"
        echo "https://github.com/njlipinski/rovr/releases/latest"
    else
        echo "No Apple Silicon build on the R drive (looked for $ZIP)."
    fi
}

# ----------------------------------------------------------------------------
# First run: no local app yet, so install one.
# ----------------------------------------------------------------------------

if [ ! -d "$APP" ]; then
    echo "Installing ROVR for $ARCH..."
    if [ -f "$ZIP" ] && install_from_zip "$ZIP"; then
        [ -f "$VERSION_SRC" ] &&
            tr -d '[:space:]' < "$VERSION_SRC" > "$VER_FILE"
        echo "Installed."
    elif install_from_legacy; then
        [ -f "$VERSION_SRC" ] &&
            tr -d '[:space:]' < "$VERSION_SRC" > "$VER_FILE"
        echo "Installed."
    else
        no_build_message
        alert "ROVR could not be installed. Check that the R drive is mounted, then try again."
        exit 1
    fi
fi

# ----------------------------------------------------------------------------
# Update check
# ----------------------------------------------------------------------------

LOCAL_VER="0.0.0"
[ -f "$VER_FILE" ] && LOCAL_VER=$(cat "$VER_FILE")

# Skip the update if the R drive is not mounted; ROVR cannot do much without it
# anyway, but launching lets the user see a real error instead of nothing.
if [ ! -f "$VERSION_SRC" ]; then
    echo "R drive not accessible, launching current version."
    open "$APP"
    exit 0
fi
LATEST_VER=$(tr -d '[:space:]' < "$VERSION_SRC")

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

echo "Updating ROVR $LOCAL_VER to $LATEST_VER..."

if [ -f "$ZIP" ] && install_from_zip "$ZIP"; then
    echo "$LATEST_VER" > "$VER_FILE"
    echo "Updated to $LATEST_VER."
elif install_from_legacy; then
    echo "$LATEST_VER" > "$VER_FILE"
    echo "Updated to $LATEST_VER."
else
    # The version file only advances on a successful install, so a failure here
    # retries on the next launch instead of pretending to be up to date.
    no_build_message
    echo "Launching current version ($LOCAL_VER)."
fi

echo "Launching ROVR..."
open "$APP"
