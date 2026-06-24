# build.ps1 — Build and deploy ROVR
#
# Usage: .\build.ps1
#
# Reads the version from VERSION, writes app/version.py, builds the exe,
# creates a git tag, and deploys to the R drive if available.

$ErrorActionPreference = "Stop"

# Read version
$version = (Get-Content VERSION).Trim()
$tag = "v$version"

Write-Host "Building ROVR $tag..."

# Generate version module (gitignored — not committed)
Set-Content -Path app\version.py -Value "__version__ = '$version'" -Encoding utf8

# Build exe
python -m PyInstaller rovr.spec
if (-not $?) {
    Write-Error "PyInstaller build failed."
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

# Deploy to R drive if accessible
$dest = "R:\Rice\Pancam\rovr.exe"
if (Test-Path "R:\Rice\Pancam") {
    Copy-Item dist\rovr.exe $dest -Force
    Write-Host "Deployed to $dest"
} else {
    Write-Host "R drive not available. Copy dist\rovr.exe to the Rice drive manually."
}

Write-Host ""
Write-Host "Done. ROVR $tag built successfully."
