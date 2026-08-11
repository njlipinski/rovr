# build.ps1 -- Build, tag, and deploy ROVR
#
# Usage:
#   .\build.ps1               Full release: preflight build, tag, push, wait for CI, stage to R drive
#   .\build.ps1 -SkipBuild    Skip the local preflight build (tag, push, wait, stage)
#   .\build.ps1 -DeployOnly   Skip build and tagging; stage an already-built release
#
# CI is the single source of truth for shipped binaries. The local PyInstaller
# run here is only a preflight check that the spec still builds -- its output is
# never deployed. Everything staged on the R drive is downloaded from the GitHub
# Release that CI produced for this version's tag, so what users run is always
# byte-for-byte what CI built.
#
# Safe to re-run. An existing tag is reused rather than recreated, an already
# pushed tag is not re-pushed, and staging is a plain overwrite. If the R drive
# is unavailable the downloaded assets are left in dist\release with manual
# instructions, so a later -DeployOnly run finishes the job.

param(
    [switch]$SkipBuild,
    [switch]$DeployOnly
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

$repo       = 'njlipinski/rovr'
$pancamPath = 'R:\Rice\Pancam'
$stagingDir = Join-Path $PSScriptRoot 'dist\release'

# Release assets CI produces, named by architecture so the Mac launcher can
# resolve its own with a bare `uname -m` and no lookup table.
$winAsset   = 'rovr.exe'
$armAsset   = 'rovr-mac-arm64.zip'
$intelAsset = 'rovr-mac-x86_64.zip'
$expected   = @($winAsset, $armAsset, $intelAsset)

# CI runs its three build jobs in parallel; the slowest is a few minutes of
# PyInstaller. Poll often enough to feel responsive, cap high enough to absorb
# a queueing backlog without hanging the terminal indefinitely.
$pollSeconds    = 15
$waitCapMinutes = 20

# PowerShell 5.1 still negotiates TLS 1.0 by default on some machines; the
# GitHub API refuses anything below 1.2.
[Net.ServicePointManager]::SecurityProtocol =
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$apiHeaders = @{
    'Accept'     = 'application/vnd.github+json'
    'User-Agent' = 'rovr-build-script'
}

# ---------------------------------------------------------------------------
# Version and repository state
# ---------------------------------------------------------------------------

$version = (Get-Content (Join-Path $PSScriptRoot 'VERSION')).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') {
    Write-Error "VERSION must be a three-part version like 0.1.1, got '$version'."
    exit 1
}
$tag = "v$version"

Write-Host "ROVR $tag"
Write-Host ""

# The tag must point at exactly what gets deployed, and launch_rovr.command is
# staged from the working tree, so a dirty tree means the drive could end up
# with files that no commit describes.
$dirty = git status --porcelain
if ($LASTEXITCODE -ne 0) {
    Write-Error "git status failed. Is this a git repository?"
    exit 1
}
if ($dirty) {
    Write-Host "Uncommitted changes:"
    Write-Host $dirty
    Write-Error "Commit or stash before releasing -- the tag must match what gets deployed."
    exit 1
}

# ---------------------------------------------------------------------------
# Preflight build (local only, never deployed)
# ---------------------------------------------------------------------------

if (-not $SkipBuild -and -not $DeployOnly) {
    Write-Host "Preflight build (checks the spec still builds; output is not deployed)..."

    # Gitignored, so writing it does not dirty the tree checked above.
    [System.IO.File]::WriteAllText(
        (Join-Path $PSScriptRoot 'app\version.py'), "__version__ = '$version'")

    python -m PyInstaller rovr.spec
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Preflight build failed (exit code $LASTEXITCODE). Fix it before tagging."
        exit 1
    }
    Write-Host "Preflight build OK."
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Tag and push (this is what triggers CI)
# ---------------------------------------------------------------------------

if (-not $DeployOnly) {
    if (git tag -l $tag) {
        Write-Host "Tag $tag already exists locally."
    } else {
        git tag $tag
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Failed to create tag $tag."
            exit 1
        }
        Write-Host "Created tag $tag."
    }

    $remoteTag = git ls-remote --tags origin "refs/tags/$tag"
    if ($remoteTag) {
        Write-Host "Tag $tag is already on origin; CI has been triggered already."
    } else {
        Write-Host "Pushing $tag to origin (this triggers the CI build)..."
        git push origin $tag
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Failed to push tag $tag."
            exit 1
        }
    }
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Wait for CI, then download the release assets
# ---------------------------------------------------------------------------

function Get-Release {
    # Returns the release object for $tag, or $null if it does not exist yet.
    # CI creates the release when its first job finishes, so 404 is the normal
    # state for the first minute or two after a tag push.
    try {
        return Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/$tag" `
                                 -Headers $apiHeaders -UseBasicParsing
    } catch {
        return $null
    }
}

Write-Host "Waiting for CI assets ($($expected -join ', '))..."
$deadline = (Get-Date).AddMinutes($waitCapMinutes)
$release  = $null

while ($true) {
    $candidate = Get-Release
    if ($candidate) {
        $have    = @($candidate.assets | ForEach-Object { $_.name })
        $missing = @($expected | Where-Object { $have -notcontains $_ })
        if ($missing.Count -eq 0) {
            $release = $candidate
            break
        }
        Write-Host "  release exists, still waiting on: $($missing -join ', ')"
    } else {
        Write-Host "  release not published yet..."
    }

    if ((Get-Date) -gt $deadline) {
        Write-Error ("Gave up after $waitCapMinutes minutes. Check the run at " +
                     "https://github.com/$repo/actions, then re-run with -DeployOnly.")
        exit 1
    }
    Start-Sleep -Seconds $pollSeconds
}

Write-Host "All assets published."
Write-Host ""

if (Test-Path $stagingDir) {
    Remove-Item $stagingDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

foreach ($name in $expected) {
    $asset = $release.assets | Where-Object { $_.name -eq $name } | Select-Object -First 1
    $out   = Join-Path $stagingDir $name
    Write-Host "Downloading $name ..."
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $out `
                      -Headers $apiHeaders -UseBasicParsing

    # A truncated download staged onto the drive would break every client, and
    # the API tells us the expected size, so there is no reason not to check.
    $actual = (Get-Item $out).Length
    if ($actual -ne $asset.size) {
        Write-Error "$name is $actual bytes, expected $($asset.size). Download incomplete."
        exit 1
    }
}
Write-Host ""

# ---------------------------------------------------------------------------
# Stage to the R drive
# ---------------------------------------------------------------------------

if (-not (Test-Path $pancamPath)) {
    Write-Host "R drive not available at $pancamPath -- nothing staged."
    Write-Host "Assets are downloaded and verified in: $stagingDir"
    Write-Host "Mount the drive and re-run: .\build.ps1 -DeployOnly"
    exit 0
}

# ROVR's own files live in a subfolder so they stay out of the scene data at the
# Pancam root. PANCAM_PATH in config.py still points at the root, because every
# scene path is built from it -- see app/paths.py rovr_dir().
$rovrDir = Join-Path $pancamPath 'ROVR'
New-Item -ItemType Directory -Force -Path $rovrDir | Out-Null

Write-Host "Staging to $rovrDir ..."

# The launcher text, normalized to LF. A CRLF launcher fails on macOS with
# "bad interpreter: /bin/bash^M". .gitattributes keeps the working copy LF, but
# normalizing here means a machine with different git settings still cannot
# stage a broken launcher. Comes from the working tree, which the dirty check
# above pinned to the tagged commit.
$launcherText = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'launch_rovr.command'))
$launcherText = $launcherText -replace "`r`n", "`n"

# Windows exe. Users run ROVR locally, never from the drive, so this file is
# never locked and the copy always succeeds.
Copy-Item (Join-Path $stagingDir $winAsset) (Join-Path $rovrDir $winAsset) -Force
Write-Host "  $winAsset"

# Mac bundles stay zipped on the drive on purpose. Extracting a .app on Windows
# drops the executable bit and flattens symlinks; leaving the zips opaque means
# launch_rovr.command expands them with ditto on the Mac, which restores both.
foreach ($name in @($armAsset, $intelAsset)) {
    Copy-Item (Join-Path $stagingDir $name) (Join-Path $rovrDir $name) -Force
    Write-Host "  $name"
}

# Launcher, so Mac users can refresh their own copy.
[System.IO.File]::WriteAllText((Join-Path $rovrDir 'launch_rovr.command'), $launcherText)
Write-Host "  launch_rovr.command"

# ---------------------------------------------------------------------------
# Transitional root copies
#
# Clients predating the ROVR subfolder read everything from the Pancam root, so
# the root has to stay fully populated until they have all updated once. Delete
# this whole block, and these files from the drive, when every user is on a
# build that resolves the subfolder.
#
# The exe and the root version file must move TOGETHER. Advancing the root
# version file while leaving a stale root exe puts an old client in an infinite
# relaunch loop: it copies the stale exe, restarts, still reads the newer
# version number, and copies again.
# ---------------------------------------------------------------------------

Write-Host "Staging transitional copies to $pancamPath ..."

Copy-Item (Join-Path $stagingDir $winAsset) (Join-Path $pancamPath $winAsset) -Force
Write-Host "  $winAsset"

[System.IO.File]::WriteAllText((Join-Path $pancamPath 'launch_rovr.command'), $launcherText)
Write-Host "  launch_rovr.command"

# Mac users still on the pre-arch-aware launcher copy this path unconditionally,
# so it has to keep tracking the arm64 build or they would silently stall on an
# old version while recording the new version number as installed. Best effort:
# this is the Windows-extraction path with the mode-bit problem described above.
$legacyApp = Join-Path $pancamPath 'rovr.app'
$legacyTmp = Join-Path $stagingDir 'legacy'
Expand-Archive -Path (Join-Path $stagingDir $armAsset) -DestinationPath $legacyTmp -Force
robocopy (Join-Path $legacyTmp 'rovr.app') $legacyApp /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
# robocopy uses 0-7 for success (1 = files copied, 3 = copied + extra removed).
if ($LASTEXITCODE -ge 8) {
    Write-Error "robocopy failed staging the transitional rovr.app (exit $LASTEXITCODE)."
    exit 1
}
Write-Host "  rovr.app (arm64, for pre-arch-aware launchers)"

# ---------------------------------------------------------------------------
# Version markers, written LAST in both locations
#
# Every updater reads this file to decide whether to update, so writing it
# before the payloads are staged would send a client after a build that is not
# on the drive yet. rovr_dir() also treats the subfolder's copy as the marker
# that the subfolder is complete, so it must be the last thing written there.
# ---------------------------------------------------------------------------

[System.IO.File]::WriteAllText((Join-Path $rovrDir 'rovr-version.txt'), $version)
[System.IO.File]::WriteAllText((Join-Path $pancamPath 'rovr-version.txt'), $version)
Write-Host "  rovr-version.txt -> $version (both locations)"

Write-Host ""
Write-Host "Done. ROVR $tag released and staged."
Write-Host "Windows users update on next launch. Mac users update on next launch_rovr.command."
