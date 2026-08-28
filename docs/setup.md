# Setup Guide

Complete setup guide for ROVR. Follow the sections relevant to your role.

---

## 1. VPN Setup (Off-Campus Access)

If you are working off-campus you must connect to the WWU VPN before mapping the network drive or accessing the database.

**Client:** Cisco Secure Client

> Reference: https://atus.wwu.edu/kb/vpn-virtual-private-network

Install and connect before proceeding with any steps that require network drive access.

---

## 2. Map the Research Network Drive

Requires VPN access from off campus.

The ROVR database and all Pancam image data live on the shared R:\ drive at `\\alpha.univ.dir.wwu.edu\Research`. You must be on the WWU network (or connected via VPN) to access it.

### Windows

1. Open File Explorer and select **This PC** from the left navigation pane.
2. Click **Map network drive** in the toolbar.
3. Choose any available drive letter (R: is conventional).
4. Enter the following path in the **Folder** field:
   ```
   \\alpha.univ.dir.wwu.edu\Research
   ```
5. Click **Finish**.
6. When prompted, sign in with your WWU credentials:
   - **Username:** `wwu\your_wwu_username`
   - **Password:** your WWU universal password

> Reference: https://atus.wwu.edu/kb/how-connect-networked-drives

### Mac

1. In Finder, go to **Go → Connect to Server** (or press `⌘K`).
2. Enter the following address:
   ```
   smb://alpha.univ.dir.wwu.edu/Research
   ```
3. Click **Connect**.
4. Sign in as a **Registered User** with:
   - **Username:** `WWU\your_wwu_username`
   - **Password:** your WWU universal password
5. The drive will open in Finder once connected.

---

## 3. ROI Studio

ROI Studio is the tool used to draw ROIs on planetary images, producing `.sel` files that ROVR tracks.

> Install instructions: https://github.com/lars-olt/roistudio/releases 

When you first click **Open in ROI Studio** inside ROVR, it will ask you to locate the ROI Studio executable on your machine (`.exe` on Windows, `.app` on macOS). ROVR remembers the path after that.

---

## 4. MERTools

Requires VPN access from off campus.

MERTools is the legacy tool for ROI creation.

> Install instructions: https://docs.google.com/document/d/1iVQ7Espf39Hbr9jIEGLv1xAonOsMJWbvNid1tcI1pls/edit?tab=t.0#heading=h.rt1yhs58sq2o

---

## 5. Running ROVR (End Users)

No Python installation required. ROVR is distributed as a self-contained executable. Install it locally on your machine.

### Windows first-time setup

1. Open the R:\ drive and navigate to `Rice\Pancam\ROVR\`.
2. Copy `rovr.exe` and `config.py` to a folder on your local machine (e.g. your Desktop or `C:\ROVR\`).
3. Double-click your local `rovr.exe`. The first time Windows may show a security warning; click **Run** to proceed.

**Auto-updates:** On every launch ROVR checks the R:\ drive for a newer version. If one is found it updates itself and restarts, with no action needed on your part.

### Mac first-time setup

You do not need to copy `rovr.app` yourself. The launcher installs it for you and picks the right build for your Mac (Apple Silicon or Intel) automatically.

1. Mount the R:\ drive (see section 2).
2. Create a folder for ROVR on your machine, e.g. `~/Applications/ROVR/`.
3. Copy these **two** files from `Pancam/ROVR/` on the R drive into that folder:
   - `launch_rovr.command`
   - `config.py` (no edits needed; the default Mac branch already points at `/Volumes/Research/Rice/Pancam`)
4. Make the launcher executable (one-time setup, run in Terminal):
   ```bash
   chmod +x ~/Applications/ROVR/launch_rovr.command
   ```
5. Double-click `launch_rovr.command`. It downloads and installs `rovr.app`, then launches it. The first time macOS may block it: go to **System Settings → Privacy & Security** and click **Open Anyway** for both the `.command` file and `rovr.app`.

**From now on, always launch ROVR via `launch_rovr.command`.** It checks for updates and downloads them from the R drive before opening the app.

If it reports that no build for your architecture is on the R drive, the deploy for your Mac type has not happened yet. Run `uname -m` to confirm which you need (`arm64` for Apple Silicon, `x86_64` for Intel) and ask for that build to be staged, or download the matching zip from the [latest ROVR release](https://github.com/njlipinski/rovr/releases/latest), unzip it, and place `rovr.app` beside the launcher by hand.

> **Upgrading from an older launcher:** launchers predating the architecture check read ROVR's files from the Pancam root, which no longer holds them, so they will silently stop finding updates. If yours is older than the copy in `Pancam/ROVR/`, replace it once (repeat step 3). The launcher never updates itself.
---

## 6. Admin / Developer Setup

This section is for supervisors or developers who need to manage users, import scenes, or modify the codebase.

### Prerequisites

- Python 3.10 or later
- pip
- Network drive mapped (section 2)

### Install

```bash
git clone https://github.com/njlipinski/rovr.git
cd rovr
pip install -r setup/requirements.txt
```

### Configure

Copy the example config:

```bash
copy config.example.py config.py   # Windows
cp config.example.py config.py     # Mac
```

`config.py` is gitignored, so every machine needs its own copy. Never commit it. The default paths branch on `sys.platform` to pick the right R:\ mount point, so no edits are needed on a standard WWU machine.

### Initialize the Database

Run the app once to create the database tables:

```bash
python main.py
```

If `DB_PATH` doesn't exist yet, the database file will be created automatically. If you see a connection error, confirm the network drive is mounted.

### Create Initial Users

ROVR has no in-app admin UI. All user management is done via the CLI:

```bash
# Create a supervisor
python setup/manage_users.py create supervisor --role supervisor

# Create analysts
python setup/manage_users.py create analyst1 --role analyst
python setup/manage_users.py create analyst2 --role analyst

# List all users to confirm
python setup/manage_users.py list
```

You will be prompted to set a password for each user at creation time.

### Import Scenes

Scans the R:\Rice\Pancam folder for Pancam `.IMG` files and imports them as unclaimed scenes (status 0).

```bash
# Import from a CSV observation table (populates every column)
python setup/util.py import obs_table.csv

# Preview what will be imported (no changes written)
python setup/util.py import obs_table.csv --dry-run

# With no CSV, falls back to scanning the drive for IOF .IMG files
python setup/util.py import
python setup/util.py import --path "R:\Rice\Pancam"
```

Re-running safely adds only new scenes.

`setup/util.py` also holds the rest of the maintenance tasks (`backup`,
`build-slides`, `copy-approved`, `build-folders`, `wipe`, and the one-off
`migrate` folder migrations). Run it with no arguments for the full list.

---

## User Management Reference

All commands are run from the repo root:

| Command | Description |
|---------|-------------|
| `python setup/manage_users.py create <username> [--role analyst\|supervisor]` | Create a new user (prompts for password) |
| `python setup/manage_users.py list` | List all users with ID, role, and active status |
| `python setup/manage_users.py role <username> <analyst\|supervisor>` | Change a user's role |
| `python setup/manage_users.py password <username>` | Reset a user's password |
| `python setup/manage_users.py deactivate <username>` | Deactivate account; returns open scenes to shared pools |
| `python setup/manage_users.py activate <username>` | Reactivate a deactivated account |

---

## Releasing a New Version

### Run any pending database migrations first

Before deploying a new build that includes schema or data changes, run the migration tool against the live database:

```bash
python setup/migrate.py --list     # see what's pending
python setup/migrate.py --dry-run  # preview without applying
python setup/migrate.py            # apply
```

Migrations are tracked in the `schema_migrations` table, so running the tool twice is safe.

### Release a new version

One command does the whole release:

1. Edit `VERSION` to bump the version number (e.g. `0.1.0` → `0.1.1`).
2. Commit all changes including the VERSION bump.
3. Run:
   ```powershell
   .\build.ps1
   ```

`build.ps1` then, in order:

1. **Preflight build.** Runs PyInstaller locally to confirm the spec still builds. Skip it with `-SkipBuild`.
2. **Tags and pushes.** Creates `v<VERSION>` if absent and pushes it to `origin`, which triggers CI.
3. **Waits for CI.** Polls the GitHub Release for the tag until all three assets are published (a few minutes; caps out at 20 and tells you to check the Actions page).
4. **Downloads and verifies.** Pulls each asset into `dist\release`, checking the downloaded size against the size the API reports so a truncated download never reaches the drive.
5. **Stages to `R:\Rice\Pancam\ROVR`.** The exe, both Mac zips, `launch_rovr.command`, `config.py`, and finally `rovr-version.txt`.

CI is the single source of truth: everything staged on the drive is downloaded from the release, so what users run is byte-for-byte what CI built.

The script is safe to re-run. An existing tag is reused rather than recreated, an already-pushed tag is not re-pushed, and staging is a plain overwrite. If the R drive is not mounted it stops after downloading and tells you to re-run with `-DeployOnly` once it is, which skips straight to staging.

Windows users update on their next ROVR launch, Mac users on their next `launch_rovr.command`.

#### Why `rovr-version.txt` is written last

Both updaters read that file to decide whether to update. Writing it before the payloads are staged would point a client at a build that is not on the drive yet. Keep it last in any change to the staging order.

#### Why the Mac bundles stay zipped on the drive

Extracting a `.app` on Windows drops the executable bit and flattens symlinks, which can produce a bundle that will not launch. Leaving the zips opaque means `launch_rovr.command` expands them with `ditto` on the Mac, which restores both. Do not replace the zips on the drive with expanded bundles.

#### Drive layout

ROVR's own files live in `R:\Rice\Pancam\ROVR\`, keeping them out of the scene data at the Pancam root:

```
R:\Rice\Pancam\
├── ROVR\                   <- everything ROVR ships
│   ├── rovr.exe
│   ├── rovr-mac-arm64.zip
│   ├── rovr-mac-x86_64.zip
│   ├── launch_rovr.command
│   ├── config.py           <- template users copy locally; nothing reads this copy
│   └── rovr-version.txt
├── MERA\, MERB\            <- scene data, never moves
├── rovr.sqlite             <- database, stays at the root
└── summary_slides\, ready_for_asdf\
```

`PANCAM_PATH` in `config.py` still points at the **root**, not at the subfolder. Every scene path is built from it, so repointing it breaks scene lookup, summary slides, and the asdf handoff. Nobody needs to edit `config.py` for this: `rovr_dir()` in [app/paths.py](../app/paths.py) finds the subfolder itself.

`rovr_dir()` keys on `rovr-version.txt`, not on the directory, so a half-staged or empty `ROVR\` folder is never mistaken for a complete one. This is the second reason the version file is written last.

#### Former root copies (removed 2026-08-28)

The Pancam root also held `rovr.exe`, `launch_rovr.command`, `rovr-version.txt`, an arm64 `rovr.app` and `config.py`, for clients predating the subfolder. Those are deleted and `build.ps1` no longer writes them.

A client old enough to still read the root now finds nothing there and degrades quietly rather than breaking: the Windows updater's `open()` raises into its blanket `except`, and the old Mac launcher takes its "R drive not accessible" path and opens the app it already has. Either way that user is stuck on their installed version until someone reinstalls them by hand from `ROVR\`.

### GitHub Release artifacts

Pushing a version tag triggers three CI jobs (Windows, Apple Silicon Mac, Intel Mac) that build the respective binaries and attach them to a GitHub Release:

| Asset | For |
|---|---|
| `rovr.exe` | Windows |
| `rovr-mac-arm64.zip` | Apple Silicon Macs (M1 and later) |
| `rovr-mac-x86_64.zip` | Intel Macs |
| `launch_rovr.command` | Both Mac builds |

The Mac zips are named with the exact strings `uname -m` reports, so the launcher resolves its own build as `rovr-mac-$(uname -m).zip` with no lookup table. Renaming these breaks that, and `build.ps1` expects them too.

Both zips contain a bundle named `rovr.app`, so only the zip name distinguishes them. Installing the wrong one gives "The application cannot be opened because it has an incorrect executable format" on launch.

The Intel job runs on GitHub's `macos-15-intel` runner and asserts both the interpreter and the built binary are x86_64, so a runner-image change cannot silently ship an arm64 bundle under the Intel name. GitHub retires x86_64 macOS support around August 2027, at which point that job stops working and Intel Macs will need a different build path.
