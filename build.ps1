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

# Deploy to R drive if accessible
$dest   = "R:\Rice\Pancam\rovr.exe"
$backup = "R:\Rice\Pancam\rovr.exe.bak"
if (Test-Path "R:\Rice\Pancam") {
    # Windows locks a running exe against overwrite but allows renaming.
    # Rename the current exe out of the way first so the copy always succeeds,
    # even if users have ROVR open. Running instances keep their file handle;
    # the backup is cleaned up on the next deploy.
    if (Test-Path $backup) { Remove-Item $backup -Force }
    if (Test-Path $dest)   { Rename-Item $dest $backup -Force }
    Copy-Item dist\rovr.exe $dest -Force
    if (Test-Path $backup) { try { Remove-Item $backup -Force -ErrorAction Stop } catch {} }
    Write-Host "Deployed to $dest"
} else {
    Write-Host "R drive not available. Copy dist\rovr.exe to the Rice drive manually."
}

Write-Host ""
Write-Host "Done. ROVR $tag built successfully."
