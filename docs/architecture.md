# Architecture

## Project Structure

```
rovr/
├── main.py                     # Entry point: init DB, launch login window
├── config.py                   # Local machine config (gitignored, copy from config.example.py)
├── config.example.py           # Template for config.py
├── VERSION                     # Single source of truth for the version number
├── rovr.spec                   # PyInstaller build spec
├── build.ps1                   # Release script: tag, push, wait for CI, stage to R drive
│
├── app/
│   ├── models.py               # Dataclasses: User, Scene, Review; SceneStatus, Decision, Stage constants
│   ├── db.py                   # All SQLite queries and DB initialization
│   ├── auth.py                 # bcrypt password hashing and login verification
│   ├── controller.py           # Workflow state machine (submit, review, claim, release, admin ops)
│   ├── local_settings.py       # Per-machine settings stored in %APPDATA%\rovr\local.json
│   ├── version.py              # Auto-generated at build time (gitignored), contains __version__
│   └── ui/
│       ├── login.py            # Login window: authenticates and routes to dashboard
│       ├── dashboard.py        # Base dashboard class: shared layout, ROI Studio launch, history
│       ├── analyst_dash.py     # Analyst view: scene pool + work queue + peer review queue
│       ├── supervisor_dash.py  # Supervisor view: approval queue + master scene list
│       └── confetti.py         # Confetti overlay for the While You Were Away summary
│
├── setup/
│   ├── manage_users.py         # CLI for user account management
│   ├── util.py                 # Maintenance CLI: scene import, backup, slides, folder migrations
│   └── requirements.txt        # PyQt6, bcrypt, matplotlib, astropy
│
└── .github/
    └── workflows/
        └── build.yml           # CI: builds Windows exe + Mac app bundles on version tag push
```

---

## Module Responsibilities

### `main.py`

Initializes the database, creates the `QApplication`, and shows `LoginUI`. When running as a PyInstaller bundle, prepends the directory containing `config.py` to `sys.path` before any imports; on macOS this means walking up past the `.app` bundle. Startup is wrapped in a try/except that logs to `%APPDATA%\rovr\error.log` and shows a dialog. Starts the keepalive timer, parented to the `QApplication` so it outlives the login window, both dashboards and every logout, like the connection it keeps alive.

On Windows frozen builds, `_try_update()` checks the R drive for a newer build, replaces the local exe and relaunches. Any failure is swallowed so a failed update never stops ROVR from starting. The relaunch strips `_PYI_*` from the child environment; inherited, those vars make the new exe treat the exiting process as its own bootloader parent and fail the onefile security check.

### `config.py`

Machine-specific paths. Branches on `sys.platform` to pick the right R:\ mount point, since Windows and Mac mount the network drive differently:

```python
import os
import sys

if sys.platform == 'win32':
    PANCAM_PATH = r"R:\Rice\Pancam"
else:
    PANCAM_PATH = "/Volumes/Research/Rice/Pancam"

DB_PATH = os.path.join(PANCAM_PATH, "rovr.sqlite")
```

Gitignored, never commit it. Copy from `config.example.py` and adjust. `ROI_STUDIO_PATH` is not in `config.py`; it lives per-machine in `local.json`.

### `app/local_settings.py`

Machine-specific settings that vary per user rather than per deployment: the ROI Studio path, the interpreter that runs it when that path is a source checkout, dark mode, UI scale, table column widths, dialog sizes, splitter positions, per-scene last-viewed times, whether confetti is on, and each username's last login on this machine (stored UTC, since it is compared against raw `reviews.timestamp` values rather than displayed). Written to `%APPDATA%\rovr\local.json` on Windows or `~/.rovr/local.json` on Mac. ROVR prompts for the ROI Studio path on first use of "Open in ROI Studio" and remembers it.

Layout sizes are stored as fractions of the total so a saved layout still applies at a different window size or UI scale: `get_column_widths`/`set_column_widths` (keyed by the table's joined headers) and `get_splitter_sizes`/`set_splitter_sizes` (keyed per splitter, e.g. `analyst.rows`).

### `app/version.py`

Auto-generated at build time by `build.ps1` and GitHub Actions. Falls back to `"dev"` when running from source. The title bar shows the current version.

### `app/models.py`

Plain dataclasses (`User`, `Scene`, `Review`) and constants namespaces:
- `SceneStatus`: integer constants 0-8
- `Decision`: strings written to `reviews.decision`: `submitted`, `approved`, `needs_revision`, `force_released`, `status_override`, `reset`
- `Stage`: strings written to `reviews.stage`: `submission`, `peer_review`, `supervisor_review`, `admin`

### `app/db.py`

All SQL lives here. Nothing outside this module touches the database directly. Functions accept an open `conn` (from `get_db_connection()`) so callers control transaction scope.

Connection settings applied on every open:

```python
conn = sqlite3.connect(DB_PATH, timeout=1.0)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=DELETE;")
conn.execute("PRAGMA foreign_keys=ON;")
```

`journal_mode=DELETE` is required: WAL mode creates `.shm`/`.wal` files that network filesystems don't reliably support. `timeout=1.0` sets SQLite's busy handler so a single locked statement fails fast.

Every write function goes through `_with_lock_retry()`, so a lock collision resolves silently instead of raising. A logical action spanning more than one statement (a status change plus an assignment field plus a `reviews` row) is one `record_*` function (`record_submission`, `record_peer_review`, `record_supervisor_review`, `record_force_release`, `record_scene_edit`, `record_scene_reset`) issuing all its writes under one `conn.commit()`, so a lock error can't leave a scene half-updated.

Timestamps are written UTC `datetime('now')`, never `'localtime'`: one shared DB means a local stamp is only meaningful on the machine that wrote it. `local_ts()` converts for display; stat counts bucket on `DATE(ts,'localtime')` so a day ends at the reader's midnight.

Reads use `_read_one` / `_read_all` / `_read_scalar`. Prefer `_read_scalar` for single-value queries (`COUNT`, `MAX`); it handles the no-row case, which `_read_one(...)[0]` leaves to chance.

`get_db_connection()` returns a wrapper around the real handle, not the handle itself. One connection is opened at startup and shared by every window and dialog, so a dropped network session is repaired by swapping the handle underneath that object rather than by restarting the app. `_probe()` reads on the existing handle to tell a stale connection from a genuinely busy database. A stale one is replaced; an unreachable drive raises `ConnectionLost`, which the UI reports as a network problem. Reads re-run after a reconnect, writes do not: they raise `ConnectionLost(restored=True)` so the user reruns them, since a commit whose acknowledgement was lost would otherwise log the review twice.

### `app/auth.py`

`hash_password()`, `verify_password()`, `authenticate_user()`. Passwords are hashed with bcrypt (salted). `authenticate_user()` returns the user row on success or `None` on failure, and rejects inactive accounts.

### `app/controller.py`

Implements workflow transitions. All state changes go through this module; the UI never calls `db.py` for a write. Each function validates preconditions (correct current status, correct actor) before delegating to the matching atomic `record_*` function in `db.py`.

Supervisor-only operations:
- `supervisor_edit_scene()`: override a scene's status and reassign owner/peer reviewer/supervisor/claimed_by. Any field passed as `models.UNCHANGED` keeps that scene's current value, which is how a batch edit touches one field across scenes that disagree on the rest. `None` still means clear the field.
- `supervisor_reset_scene()`: wipe all ownership/claim data and return to unclaimed (status 0)
- `mark_scene_issues()`: status 6 to 8, for a flagged scene reaching supervisor review
- `force_release_scene()`: release a stuck status-3 claim back to the peer review pool (defined, not yet wired to a UI control)

All four log to `reviews`.

`asdf_stamps()` builds the `FitsStamp` cards (approved flag plus analyst, reviewer and supervisor usernames) for a scene's ASDF copies.

### ASDF collections

`ready_for_asdf_preliminary/<rover>/` holds a stamped copy of each scene's `.fits` for statuses 2-7; `ready_for_asdf/<rover>/` holds status 7 only. `paths.sync_asdf_copies()` writes or removes a scene's copies to match its status, and `Dashboard._sync_asdf_copies()` runs it after every workflow write except claims and releases. Stamps go on HDU 0 only, where ROI Studio keeps scene-wide cards. Working files are never stamped. `setup/util.py sync-asdf` applies the same rule to every scene. See ADR-030.

### `app/ui/`

PyQt6 widgets. `Dashboard` is the base class; `AnalystDashboard` and `SupervisorDashboard` extend it.

**`dashboard.py`** provides:
- Shared window layout (title bar with version + username, Filters button, ☰ menu with See Stats / Change Username / Change Password, Logout button)
- `_run_db_action(fn, error_title)`: run a controller write, showing a dialog instead of crashing on either a `ValueError` (business rule) or a `sqlite3.OperationalError` that outlasted every retry in `db.py`. Every write-triggering handler goes through this.
- `handle_open_roi(scene_id)` / `handle_open_notebook(scene_id)`: act on one scene by ID. Subclasses must never redefine these names.
  - The stored ROI Studio path may be a packaged build (`.exe`, `.app`) or a source checkout's `main.py`. `roi_studio_command(path, args, interpreter)` turns it into the right command: a `.py` runs under its own interpreter with `cwd` set to the repo root, a macOS `.app` goes through `open -n -a` because a bundle is a directory rather than a binary, and anything else runs directly. Picking a `.py` resolves the interpreter once: `find_venv_python()` looks for `.venv`/`venv`/`env` beside the script and one level up, and only asks if there is none. Both paths live in `local_settings`. `tests/test_roi_studio_launch.py` pins the `.exe`/`.app` commands so a change here cannot alter what existing users launch.
- `local_ts(value)`: render a stored UTC timestamp in the viewer's zone. Every timestamp shown to a user goes through it. Unparseable values pass through unchanged.
- `handle_open_folder(scene_id)`: open the scene's saved ROI Studio folder in Explorer/Finder via `QDesktopServices`. A scene with no findable folder falls back to that sol's `working/` folder and says so.
- `act_open_roi(table)`, `act_open_notebook(table)`, `act_open_folder(table)`, `act_notes(table)`, `act_science_notes(table)`, `act_flag(table)`, `act_summary_slide(table)`: the actions that work the same on any scene table. Each reads the selection from the table it is given, so no dashboard defines per-table wrappers.
- `generate_summary_slide(scene_id, force=False)` / `handle_open_summary_slide(scene_id)`: build a scene's summary slide (skipping the work when the one on disk is newer than everything it was built from) and open it in the system PDF viewer. `generate_summary_slide()` returns a problem description instead of raising, so a batch pools its failures into one dialog and a slide can never block a review.
- `build_tray(tray, table, actions, enabled=True)`: rebuild a tray's buttons. Entries in `actions` are either a `SCENE_ACTIONS` key (bound to `table` by `_bind()`, which swallows the `checked` bool `QPushButton.clicked` emits) or an explicit `(label, method name)` pair.
- `make_tray_bar(sections)`: build the dashboard's single shared tray and wire single-table selection. `sections` is a sequence of `(table, title, actions)`; `actions` may be a list or a zero-argument callable, for tables whose buttons depend on the selected row's status. Also builds the Refresh button, which sits outside `shared_tray` so `build_tray()` never clears it.
- `make_rows_splitter(key, top, tray_bar, bottom, weights=(1, 1))`: stack a row of tables, the tray, and a second row of tables in one vertical splitter. The tray is a pinned, non-collapsible middle pane; `weights` is the top/bottom stretch used until the user drags.
- `update_shared_tray()` / `_on_table_selection_changed(table)`: rebuild the tray for the active table, and keep exactly one table selected at a time (`_syncing` guards against `clearSelection()` re-entering the handler)
- `_review_callbacks(table, scene_id)`: overridable hook returning the `on_approve`/`on_kick_back` kwargs for the notes dialog when the selected row is one this user may act on. Empty on the base class.
- `_show_notes(scene_id, scene_name, ...)`: fetches history from `get_scene_thread()` and shows `NotesDialog`. When `on_approve`/`on_kick_back` are supplied (only from a reviewable context: analyst status 3, supervisor status 6) the dialog also shows those buttons, which send the note box's text as the review comment and close on success.
- `_mark_viewed(scene_id)`: clears the new-activity highlight after notes are read
- `_away_summary_items()` / `_show_away_summary()`: the "While You Were Away" dialog, raised from `showEvent` rather than `__init__`, where a modal can come up behind a window that has not been shown yet. `_away_summary_items()` returns `[(count, singular, plural, celebrate)]` and is empty on the base class, so supervisors get no dialog until there is something worth telling them; `AnalystDashboard` overrides it with `db.get_owned_activity_since()`. A `celebrate` line with a nonzero count drops a `ConfettiOverlay` before the dialog opens, so it is already falling behind it. ☰ menu → Confetti turns it off per machine; the summary still shows. The window comes from `LoginUI._consume_away_window()`, which reads the stored stamp before re-stamping it and does so once per process, so a mid-shift logout/login neither empties nor resets the window. A user with no stamp on this machine is a first run: it sets the baseline and shows nothing, since an empty stamp would match every row in `reviews`. Nothing to report means no dialog. See `tests/test_away_summary.py`.
- `_fill_table()`: single hook point for populating any scene table; stores `scene_key` per row and applies the active filters
- `_apply_filters()` / `_open_filter_dialog()`: Rover / Sol Range / SeqID Range filtering applied to every table on both dashboards
- `PersistentSplitter(orientation, key)`: `QSplitter` that remembers where the user dragged its handles, under `key` in `local_settings`. Saved sizes are reapplied on every resize rather than restored once, and a pinned pane like the tray keeps its own extent while the free panes renormalize over the remainder.
- `make_scene_table()`, `make_button_tray()`, `make_section(label, table, tray=None, count_fn=None)`, `clear_tray()`: UI construction helpers. `make_section()`'s `tray` is optional; both dashboards omit it now that buttons live in the shared tray.
- `SCENE_ACTIONS` / `SCENE_BUTTONS`: module-level button spec, key to `(label, act_* method)`, and the full set in tray order. Every tray uses the same `'notes'` key ("Review"); whether its dialog can also approve or kick back comes from `_review_callbacks()`, not from the button.
- Button colors come from `styles.color_button(btn)`, called once in `build_tray()` and on `NotesDialog`'s Approve/Kick Back. `styles.BUTTON_COLOR_BY_LABEL` maps a label to a fill: green moves a scene forward (Approve, Submit), blue opens the Review dialog, red sends it back or ends it (Kick Back, Mark Bad Scene, Reset Scene). Applied per widget rather than as a theme rule, because the light theme runs with no stylesheet at all and these must look the same in both themes.
- `SCENE_SHORTCUTS`: keyboard shortcuts keyed by `SCENE_ACTIONS` key (`R` = Open in ROI Studio, `E` = Review). Adding a hotkey is one line there: `_install_scene_shortcuts()` binds it to both dashboards, `_run_scene_shortcut()` runs it on whichever table holds the selection, and `build_tray()` puts the key in that button's tooltip. A shortcut fires only if the selected table's own tray offers the action, so it can never reach one a table deliberately withholds.
- `selected_id()`, `selected_ids()`, `selected_status()`, `_scene_name_from(table)`: read selected row data by column index
- `refresh_task_list()`: abstract; each dashboard subclass must implement it
- `NotesDialog`, `FilterDialog`, `StatsDialog`, `ChangeUsernameDialog`, `ChangePasswordDialog`: shared modal dialogs

**`analyst_dash.py`**: four sections. My Work Queue (status 1/4 owned, or 3 claimed for peer review), In Progress (read-only: owned scenes at status 2/3/5/6, or scenes this analyst peer-reviewed at status 4/5/6), Ready for Peer Review (status 2, shared pool), and Unclaimed Scenes (status 0, shared pool), plus My Completed Scenes and All Scenes tabs. Defines only the handlers unique to this role (Submit, Release, Approve, Claim) plus a `_review_callbacks()` override; the shared scene actions come from `Dashboard`. Kicking a scene back is done from the Review dialog, not a tray button.

**`supervisor_dash.py`**: four sections. My Work Queue (status 6, claimed scenes to approve/kick back/mark bad), Supervisor Pool (status 5, shared, claim), In Progress (status 4 scenes this supervisor kicked back, read-only), and All Scenes (master list, with Edit Scene and Reset Scene). Edit Scene on a multi-row selection opens `EditSceneDialog` in batch mode, where every field defaults to "leave unchanged" and only the ones moved off it are written; the batch runs through `run_bulk_action()`, so each scene is written and logged independently. Defines only the role-specific handlers (Approve, Mark Bad Scene, Release, Claim, Edit Scene, Reset Scene) plus a `_review_callbacks()` override. Kicking a scene back is done from the Review dialog, not a tray button.

**`app/slides.py`**: renders a scene's summary slide, a one-page PDF with left-eye DCS, right-eye RGB, spectra and per-ROI metadata in a 2x2 grid, written both beside its source images and into `summary_slides/<rover>/`. The three image panels are composited from the PNGs ROI Studio writes on every save, at native size, never cropped. `build_summary_slide()` and `slide_is_current()` are pure functions of a scene row and the Pancam tree, with no Qt and no SQL, so they can run off the UI thread.

**`app/fits_header.py`**: minimal FITS header reader (fixed 80-byte cards, `HIERARCH`, `CONTINUE`), used to pull ROI metadata out of a scene's `.fits` without pulling in astropy. Reads headers only, seeking past pixel data.

**`app/roi_metadata.py`**: extracts the analyst-assigned metadata ROI Studio stamps onto each ROI's image HDU (left eye only; the right-eye HDUs repeat it), and loads field order and labels at run time from ROI Studio's own `pcam_roi_metadata.json`. The vocabulary of allowed values is deliberately not mirrored here: ROVR displays values and never validates them, so changing categories needs no ROVR release.

---

## Database Schema

The database is a single SQLite file shared across all users via the R:\ drive.

### `users`

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `username` | TEXT UNIQUE | Login name and display name |
| `active` | INTEGER | `1` = active, `0` = deactivated |
| `password_hash` | TEXT | bcrypt hash |
| `role` | TEXT | `analyst` or `supervisor` |

### `scenes`

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `name` | TEXT | Scene name |
| `scene_key` | TEXT UNIQUE | Path-derived key for deduplication on import |
| `roi_filename` | TEXT UNIQUE | Path to `.sel` file on R:\ drive |
| `owner_id` | INTEGER FK → users.id | Analyst 1; never changes (cleared only on deactivation) |
| `peer_reviewer_id` | INTEGER FK → users.id (nullable) | Set once when Analyst 2 submits their decision |
| `supervisor_id` | INTEGER FK → users.id (nullable) | Set once when supervisor submits their decision |
| `status` | INTEGER | 0-8; see status reference below |
| `claimed_by` | INTEGER FK → users.id (nullable) | User currently holding a claim (status 1, 3, or 6) |
| `submitted_at` | TEXT | Declared but never written |
| `updated_at` | TEXT | UTC timestamp of most recent status change |

### `reviews`

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | Auto-increment |
| `scene_id` | INTEGER FK → scenes.id | |
| `reviewer_id` | INTEGER FK → users.id | |
| `timestamp` | TEXT | UTC timestamp, written as `datetime('now')` |
| `stage` | TEXT | `submission`, `peer_review`, `supervisor_review`, `admin` |
| `decision` | TEXT | `submitted`, `approved`, `needs_revision`, `force_released`, `status_override`, `reset` |
| `comments` | TEXT | Optional note when kicking back; always `NULL` on approve |

The `reviews` table is append-only. Every status-changing action writes a row here. Never UPDATE or DELETE rows.

---

## Scene Status Reference

| Value | Name | Queue | Who acts next |
|-------|------|-------|---------------|
| `0` | unclaimed | Scene Pool, shared, all analysts | Any analyst |
| `1` | claimed | Analyst 1's to-do list | Analyst 1 (owner) |
| `2` | pending review | Peer Review Pool, shared, all analysts except owner | Any analyst except owner |
| `3` | in review | Analyst 2's to-do list | Analyst 2 (peer reviewer) |
| `4` | needs revision | Analyst 1's to-do list | Analyst 1 (owner) |
| `5` | pending supervisor | Supervisor Pool, shared, all supervisors | Any supervisor |
| `6` | in supervisor review | Supervisor's to-do list | Supervisor (claimant) |
| `7` | approved | terminal | none |
| `8` | issues | terminal | none |

**Shared pools** (scenes visible to all users of a role): status 0, 2, and 5.

---

## Claim / Release Design

Multiple analysts share the same database file over a network drive. Without protection, two analysts could see the same `pending_review` scene and both try to claim it.

ROVR prevents this with an atomic conditional UPDATE:

**Analyst 1 claims from the Scene Pool (status 0 to 1):**
```sql
UPDATE scenes SET status = 1, claimed_by = ?
WHERE id = ? AND status = 0
```

**Analyst 2 claims from the Peer Review Pool (status 2 to 3):**
```sql
UPDATE scenes SET status = 3, claimed_by = ?
WHERE id = ? AND status = 2 AND owner_id != ?
```

**Supervisor claims from the Supervisor Pool (status 5 to 6):**
```sql
UPDATE scenes SET status = 6, claimed_by = ?
WHERE id = ? AND status = 5
```

SQLite serializes writes, so only one simultaneous claim matches the WHERE condition. Each `db.claim_*()` function returns `rowcount == 1` for success. A claimed scene (status 1 or 3) must never be added back into a shared-pool query (`WHERE status = 0` / `WHERE status = 2`); any refactor that does so would re-surface a claimed scene to other users.

Release paths:
- **Normal:** analyst submits or approves, scene advances, `claimed_by` cleared. Exception: resubmitting a status-4 scene that already has a `supervisor_id` sets `claimed_by` to that supervisor directly (status 4 to 6) instead of clearing it.
- **Manual release:** analyst clicks Release, `release_scene_to_pool()`, back to status 0 or 2
- **Force release:** supervisor force-releases a stuck claim, `force_release_scene()`, logged in `reviews`
- **Admin override:** supervisor uses Edit Scene or Reset Scene in the master list, `supervisor_edit_scene()` or `supervisor_reset_scene()`, logged in `reviews`

---

## Workflow Invariants

These rules must hold at all times. Violating them is a bug, not a design choice.

- **An analyst cannot review their own scene.** `owner_id` is checked both when claiming a scene for peer review (status 2 to 3) and when submitting a peer decision.
- **A resubmission after kickback goes directly to the supervisor and never re-enters peer review.**
- **Review comments are optional on both decisions.** The Approve/Kick Back buttons in `NotesDialog` send the note box's text as `comments`, or `None` if left blank.
- **Deactivating a user must return their scenes to the shared pools atomically.** `deactivate_user()` updates the user's `active` flag and all their open scenes in one transaction. These writes must never be separated. See User Deactivation in [workflow.md](workflow.md).
- **A kickback with no owner goes to the pool, not to needs-revision.** If `owner_id` is NULL, `peer_review_scene()`/`supervisor_review_scene()` route a kickback to status 0 instead of 4, since there is no owner left to revise it. 
- **Only active users (`active = 1`) can authenticate.** `authenticate_user()` silently rejects a deactivated user's credentials even if correct.
- **A summary slide is never a precondition for the workflow.** Generation runs after the DB write that moved the scene has committed, and reports failure instead of raising.
- **Every write in `db.py` retries on a locked database, and a multi-statement action commits as one atomic unit.** 

## Role Constraints

### Analysts
- Can only submit or resubmit scenes they own (`owner_id = analyst_id`); status must be 1 or 4.
- Can only claim scenes for peer review that they do NOT own; scene must be status 2.
- Can only approve or kick back scenes they are currently holding (status 3, `claimed_by = analyst_id`).
- Cannot access the Supervisor Pool (status 5) or perform supervisor actions.

### Supervisors
- Claim scenes from the Supervisor Pool (status 5 to 6); review and approve/kick back from status 6.
- Can force-release any stuck claim (status 3) regardless of which analyst holds it.
- Can override a scene's status to any valid value (0-8), and reassign owner, peer reviewer, supervisor, and claimed-by, via Edit Scene in the master list.  Selecting several rows edits them as a batch: fields left on "leave unchanged" keep each scene's own value.
- Can wipe all ownership/claim data and reset a scene to unclaimed (status 0) via Reset Scene.
- Cannot submit scenes as an analyst; the supervisor role is review-only.

## Data Integrity Rules

- `owner_id`, `peer_reviewer_id`, and `supervisor_id` are each set at most once during normal workflow, never overwritten except through the supervisor override below.
- **Exception:** a supervisor can reassign `owner_id`, `peer_reviewer_id`, `supervisor_id`, and `claimed_by` via Edit Scene, to correct a mis-assigned scene. Every use is logged to `reviews` as `scene_edited` with a diff of what changed.
- `roi_filename` is a path to a `.sel` file on the `R:\` drive, set at scene creation and never changed. Analysts edit the file in place.
- The `reviews` table is append-only. It is the authoritative audit trail for every action taken on every scene.

---

## Out of Scope (Current Prototype)

Potential future work, not part of the current build:

- Admin account with an inbox collecting bug reports raised from inside ROVR
- asdf integration after supervisor approval

---

## Versioning and Build

The `VERSION` file at the repo root is the single source of truth. The build pipeline (`build.ps1` / GitHub Actions) reads it, writes `app/version.py` (gitignored), and passes it to PyInstaller. The title bar shows the current version at runtime.
