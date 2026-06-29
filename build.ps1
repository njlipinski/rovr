# build.ps1 — Build and deploy ROVR
#
# Usage: .\build.ps1
#
# Reads the version from VERSION, writes app/version.py, builds the exe,
# creates a git tag, and deploys to the R drive if available.

# Read version
$version = (Get-Content VERSION).Trim()
$tag = "v$version"

Write-Host "Building ROVR $tag..."

# Generate version module (gitignored — not committed)
Set-Content -Path app\version.py -Value "__version__ = '$version'" -Encoding utf8

# Build exe
python -m PyInstaller rovr.spec
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller build failed (exit code $LASTEXITCODE)."
    exit 1
}

# Create git tag if it doesn't already exist
$existingTag = git tag -l $tag
if ($existingTag) {
    Write-Host "Tag $tag already exists, skipping tag creation."
} else {
    git tag $tag
    Write-Host "Created git tag $tag. Push it with: git push origin $tag"
}

# Deploy to R drive if accessible.
# Users run ROVR from their local machines (not from the R drive), so the exe
# here is never locked — a direct copy always succeeds. ROVR checks this file
# on startup and self-updates if a newer version is present.
$dest    = "R:\Rice\Pancam\rovr.exe"
$verfile = "R:\Rice\Pancam\rovr-version.txt"
if (Test-Path "R:\Rice\Pancam") {
    Copy-Item dist\rovr.exe $dest -Force
    Set-Content $verfile $version -Encoding utf8
    Write-Host "Deployed $dest"
    Write-Host "Version file written: $verfile"
} else {
    Write-Host "R drive not available. Manually copy dist\rovr.exe to R:\Rice\Pancam\ and create rovr-version.txt containing: $version"
}

Write-Host ""
Write-Host "Done. ROVR $tag built successfully."
