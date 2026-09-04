# app/db.py
"""all SQLite operations, including creating tables and inserting data"""
import sqlite3
import time
from config import DB_PATH
from app.models import Stage, Decision, SceneStatus, Role


# Timestamps are stored UTC.  Always datetime('now'), never 'localtime', since
# every client shares one DB and local stamps from different zones sort wrong.
# Convert for display only (local_ts() in app/ui/dashboard.py). Rows predating
# this hold local time and display shifted.
_RETRY_DELAY = 0.1

# A blocked attempt costs roughly the connection's busy timeout (see
# get_db_connection), so the retry count is what sets how long a user waits.
_WRITE_RETRIES = 25
_READ_RETRIES = 5

# Failures are timed, not counted, before we suspect the drive rather than a
# peer: a blocked commit returns immediately (see _with_lock_retry) so 25
# attempts take a few seconds, while a blocked first statement waits out the 1s
# busy timeout each time and the same 25 attempts take half a minute.
_PROBE_AFTER = 2.0

# Reads page 1 of the file under a shared lock, so it touches the drive rather
# than answering from cache. sqlite_master, not sqlite_schema: the newer name
# needs SQLite 3.33+.
_PROBE_SQL = "SELECT count(*) FROM sqlite_master"

# Idle SMB sessions are dropped after roughly 15 minutes, so touch the file
# well inside that.
KEEPALIVE_SECONDS = 300


class ConnectionLost(sqlite3.OperationalError):
    """The database file could not be reached, or was reached only after the
    connection had to be reopened.

    Subclasses OperationalError so existing handlers still catch it.
    `restored` is True when the connection has already been repaired and the
    action just needs running again.
    """

    def __init__(self, message, restored=False):
        super().__init__(message)
        self.restored = restored


def _probe(conn):
    """Tell a dead handle apart from a genuinely busy database.

    Returns a replacement connection when the handle is stale, None when the
    database is only busy, and raises ConnectionLost when the drive is gone.
    """
    try:
        conn.execute(_PROBE_SQL).fetchone()
        return None
    except sqlite3.Error:
        pass
    fresh = None
    try:
        fresh = _connect()
        fresh.execute(_PROBE_SQL).fetchone()
        return fresh
    except sqlite3.Error as e:
        if fresh is not None:
            try:
                fresh.close()
            except sqlite3.Error:
                pass
        if isinstance(e, sqlite3.OperationalError) and 'locked' in str(e).lower():
            return None
        raise ConnectionLost(str(e)) from e


def _retry_on_lock(conn, fn, retries, delay, on_error=None, retry_after_reconnect=True):
    """Run fn(), retrying while SQLite reports the database is locked.

    DB_PATH lives on a shared network drive, so a second user's
    near-simultaneous write is expected to occasionally collide -- it should
    resolve within a second or two once their transaction commits.

    A dropped network session is the other failure mode, and it never clears on
    its own. Once failures have outlasted _PROBE_AFTER, _probe() decides
    which one this is. A stale handle is replaced in place and the caller's
    holders never notice. An unreachable drive raises ConnectionLost so the UI
    can say so instead of blaming contention.

    Callers use the two wrappers below rather than calling this directly.
    """
    started = time.monotonic()
    attempt = 0
    probed = False
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if on_error is not None:
                try:
                    on_error()
                except sqlite3.Error:
                    pass  # a dead handle cannot roll back, and must not mask e
            locked = 'locked' in str(e).lower()
            attempt += 1
            last = attempt >= retries
            if (not probed and isinstance(conn, _ReconnectingConnection)
                    and (not locked or last or time.monotonic() - started >= _PROBE_AFTER)):
                probed = True
                fresh = _probe(conn)
                if fresh is not None:
                    conn._swap(fresh)
                    if not retry_after_reconnect:
                        # Writes are never re-run across a reconnect: a commit
                        # whose acknowledgement was lost would be applied twice,
                        # duplicating the reviews row. The user reruns it.
                        raise ConnectionLost(str(e), restored=True) from e
                    continue
            if not locked or last:
                raise
            time.sleep(delay)


def _with_lock_retry(conn, fn, retries=_WRITE_RETRIES, delay=_RETRY_DELAY):
    """Run fn() (a DB write) with retries, rolling back between attempts.

    Every write in this module goes through here so that a lock never
    surfaces as a crash."""
    return _retry_on_lock(conn, fn, retries, delay, on_error=conn.rollback,
                            retry_after_reconnect=False)


def _with_read_retry(conn, fn, retries=_READ_RETRIES, delay=_RETRY_DELAY):
    """Run fn() (a DB read) with retries. Use _read_one/_read_all instead."""
    return _retry_on_lock(conn, fn, retries, delay)


def _read_one(conn, sql, params=()):
    """Single-row read, retried while the database is locked."""
    return _with_read_retry(conn, lambda: conn.execute(sql, params).fetchone())


def _read_all(conn, sql, params=()):
    """Multi-row read, retried while the database is locked."""
    return _with_read_retry(conn, lambda: conn.execute(sql, params).fetchall())


def _read_scalar(conn, sql, params=(), default=0):
    """First column of a single-row read (COUNT, MAX, ...), retried while the
    database is locked. `default` covers the no-row case, which an aggregate
    never hits but a plain SELECT can."""
    row = _read_one(conn, sql, params)
    return default if row is None else row[0]


# ── User functions ────────────────────────────────────────────────────────────

def get_user_by_username(conn, username):
    return _read_one(conn, "SELECT * FROM users WHERE username = ?", (username,))

def get_user_by_id(conn, user_id):
    return _read_one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))

def get_all_active_analysts(conn):
    return _read_all(conn, "SELECT * FROM users WHERE active = 1 AND role = 'analyst'")

def get_all_users(conn):
    return _read_all(conn, "SELECT * FROM users ORDER BY role, username")

def create_user(conn, username, password_hash, role):
    def _write():
        conn.execute(
            "INSERT INTO users (username, active, password_hash, role) VALUES (?, 1, ?, ?)",
            (username, password_hash, role)
        )
        conn.commit()
    _with_lock_retry(conn, _write)

def activate_user(conn, user_id):
    def _write():
        conn.execute("UPDATE users SET active = 1 WHERE id = ?", (user_id,))
        conn.commit()
    _with_lock_retry(conn, _write)

def deactivate_user(conn, user_id):
    """Deactivate user and return their open scenes to shared pools.
    Status 1/4 (owner's work) -> 0: ownership cleared so a new analyst can claim fresh.
    Status 3 where the deactivated user is the owner -> 2: their scene returns to the
    peer review pool so a different analyst can still review it.
    Status 3 where the deactivated user is the peer reviewer (claimed_by) -> 2: their
    claim is released so another analyst can pick up the review.
    Status 6 (supervisor's claimed scene) -> 5: released back to the supervisor pool."""
    def _write():
        conn.execute("""
            UPDATE scenes
            SET status = 0, owner_id = NULL, claimed_by = NULL,
                updated_at = datetime('now')
            WHERE owner_id = ? AND status IN (1, 4)
        """, (user_id,))
        conn.execute("""
            UPDATE scenes
            SET status = 2, claimed_by = NULL, updated_at = datetime('now')
            WHERE owner_id = ? AND status = 3
        """, (user_id,))
        conn.execute("""
            UPDATE scenes
            SET status = 2, claimed_by = NULL, updated_at = datetime('now')
            WHERE claimed_by = ? AND status = 3
        """, (user_id,))
        conn.execute("""
            UPDATE scenes
            SET status = 5, claimed_by = NULL, updated_at = datetime('now')
            WHERE claimed_by = ? AND status = 6
        """, (user_id,))
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
        conn.commit()
    _with_lock_retry(conn, _write)

def update_user_role(conn, user_id, new_role):
    def _write():
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
        conn.commit()
    _with_lock_retry(conn, _write)

def update_user_password(conn, user_id, new_password_hash):
    def _write():
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_password_hash, user_id))
        conn.commit()
    _with_lock_retry(conn, _write)

def update_username(conn, user_id, new_username):
    def _write():
        conn.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user_id))
        conn.commit()
    _with_lock_retry(conn, _write)


# ── Scene functions ───────────────────────────────────────────────────────────
#
# Status integers:
#   0 unclaimed             — Scene Pool (shared, all analysts)
#   1 claimed               — Analyst 1's to-do
#   2 pending review        — Peer Review Pool (shared, all analysts except owner)
#   3 in review             — Analyst 2's to-do
#   4 needs revision        — Analyst 1's to-do
#   5 pending supervisor    — Supervisor Pool (shared, all supervisors)
#   6 in supervisor review  — individual supervisor's to-do (claimed)
#   7 approved              — done (terminal)
#   8 issues                — flagged as problematic (terminal)

def create_scene(conn, name, scene_key, roi_filename=None, owner_id=None):
    """create a scene; owner_id and roi_filename are None for pool-imported scenes
    (owner set at claim time, roi_filename set when analyst saves the .sel file)"""
    def _write():
        conn.execute(
            # updated_at is named explicitly rather than left to the column
            # default: an existing DB keeps whatever default it was created
            # with, which predates the switch to UTC.
            "INSERT INTO scenes (name, scene_key, roi_filename, owner_id, status, updated_at)"
            " VALUES (?, ?, ?, ?, 0, datetime('now'))",
            (name, scene_key, roi_filename, owner_id)
        )
        conn.commit()
    _with_lock_retry(conn, _write)

def get_scene_by_id(conn, scene_id):
    return _read_one(conn, "SELECT * FROM scenes WHERE id = ?", (scene_id,))

def claim_from_pool(conn, scene_id, analyst_id):
    """analyst 1 atomically claims an unclaimed scene (0 -> 1), setting owner if not yet assigned"""
    def _write():
        cur = conn.execute(
            """UPDATE scenes
                SET status = 1, claimed_by = ?,
                    owner_id = COALESCE(owner_id, ?)
                WHERE id = ? AND status = 0""",
            (analyst_id, analyst_id, scene_id)
        )
        conn.commit()
        return cur.rowcount == 1
    return _with_lock_retry(conn, _write)

def claim_for_review(conn, scene_id, analyst_id):
    """analyst 2 atomically claims a scene for peer review (2 -> 3)"""
    def _write():
        cur = conn.execute(
            "UPDATE scenes SET status = 3, claimed_by = ? WHERE id = ? AND status = 2 AND owner_id != ?",
            (analyst_id, scene_id, analyst_id)
        )
        conn.commit()
        return cur.rowcount == 1
    return _with_lock_retry(conn, _write)

def release_scene(conn, scene_id):
    """release a claimed scene back to the appropriate pool (1 -> 0, or 3 -> 2)"""
    scene = get_scene_by_id(conn, scene_id)
    if scene is None:
        return
    if scene['status'] == 1:
        # Returning to Scene Pool — clear ownership so the next claimer starts fresh
        sql = """UPDATE scenes
                SET status = 0, owner_id = NULL, claimed_by = NULL,
                    updated_at = datetime('now')
                WHERE id = ?"""
    elif scene['status'] == 3:
        # Returning to Peer Review Pool — only clear the reviewer's claim
        sql = "UPDATE scenes SET status = 2, claimed_by = NULL, updated_at = datetime('now') WHERE id = ?"
    else:
        return
    def _write():
        conn.execute(sql, (scene_id,))
        conn.commit()
    _with_lock_retry(conn, _write)


# ── Queue getters ─────────────────────────────────────────────────────────────

def get_scene_pool(conn):
    """all unclaimed scenes available for any analyst to claim (status 0)"""
    return _read_all(conn, "SELECT * FROM scenes WHERE status = 0")

def get_analyst_queue(conn, user_id):
    """scenes in analyst's personal to-do: owned (1, 4) and claimed for peer review (3)"""
    return _read_all(
        conn,
        """SELECT scenes.*, users.username AS owner_username
            FROM scenes
            LEFT JOIN users ON scenes.owner_id = users.id
            WHERE (scenes.status IN (1, 4) AND scenes.owner_id = ?)
                OR (scenes.status = 3 AND scenes.claimed_by = ?)
            ORDER BY CASE WHEN scenes.status = 4 THEN 0 ELSE 1 END,
                    scenes.updated_at DESC""",
        (user_id, user_id)
    )

def get_ready_queue(conn):
    """scenes available for peer review (status 2, shared pool)"""
    return _read_all(conn, """
        SELECT scenes.*, users.username AS owner_username
        FROM scenes
        LEFT JOIN users ON scenes.owner_id = users.id
        WHERE scenes.status = 2
    """)

def claim_for_supervisor_review(conn, scene_id, supervisor_id):
    """supervisor atomically claims a scene from the supervisor pool (5 -> 6)"""
    def _write():
        cur = conn.execute(
            "UPDATE scenes SET status = 6, claimed_by = ?, updated_at = datetime('now') WHERE id = ? AND status = 5",
            (supervisor_id, scene_id)
        )
        conn.commit()
        return cur.rowcount == 1
    return _with_lock_retry(conn, _write)

def release_supervisor_review(conn, scene_id):
    """return a supervisor-claimed scene to the supervisor pool (6 -> 5)"""
    def _write():
        conn.execute(
            "UPDATE scenes SET status = 5, claimed_by = NULL, updated_at = datetime('now') WHERE id = ? AND status = 6",
            (scene_id,)
        )
        conn.commit()
    _with_lock_retry(conn, _write)

def update_scene_flags(conn, scene_id, flags_str):
    """update the flags column on a scene"""
    def _write():
        conn.execute(
            "UPDATE scenes SET flags = ?, updated_at = datetime('now') WHERE id = ?",
            (flags_str, scene_id)
        )
        conn.commit()
    _with_lock_retry(conn, _write)

def get_supervisor_queue(conn):
    """scenes awaiting supervisor review (status 5, shared pool)"""
    return _read_all(conn, """
        SELECT scenes.*, users.username AS owner_username
        FROM scenes
        LEFT JOIN users ON scenes.owner_id = users.id
        WHERE scenes.status = 5
    """)

def get_supervisor_my_queue(conn, supervisor_id):
    """scenes this supervisor has claimed for review (status 6)"""
    return _read_all(conn, """
        SELECT scenes.*, users.username AS owner_username
        FROM scenes
        LEFT JOIN users ON scenes.owner_id = users.id
        WHERE scenes.status = 6 AND scenes.claimed_by = ?
    """, (supervisor_id,))

def get_issues_queue(conn):
    """all scenes in issues status (status 8)"""
    return _read_all(conn, """
        SELECT scenes.*, users.username AS owner_username
        FROM scenes
        LEFT JOIN users ON scenes.owner_id = users.id
        WHERE scenes.status = 8
        ORDER BY scenes.updated_at DESC
    """)


# ── Review functions ──────────────────────────────────────────────────────────
#
# Each of these bundles a scene-table write together with its audit-log entry
# into a single commit, retried as one unit if the DB is locked -- so a lock
# error can never leave a scene half-transitioned (e.g. reviewer stamped but
# status/log not updated, or vice versa). Controller functions that used to
# make several separate db.py calls for one logical action now make one call
# here instead.

_REVIEW_INSERT_SQL = (
    "INSERT INTO reviews (scene_id, reviewer_id, stage, decision, comments, timestamp) "
    "VALUES (?, ?, ?, ?, ?, datetime('now'))"
)


def record_submission(conn, scene_id, new_status, claimed_by, analyst_id, stage, decision, comments):
    """Transition a scene on analyst submission/resubmission and log it."""
    def _write():
        conn.execute(
            "UPDATE scenes SET status = ?, claimed_by = ?, updated_at = datetime('now') WHERE id = ?",
            (new_status, claimed_by, scene_id)
        )
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, analyst_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(conn, _write)


def record_peer_review(conn, scene_id, reviewer_id, new_status, stage, decision, comments):
    """Set the peer reviewer, transition status, and log the decision."""
    def _write():
        conn.execute("UPDATE scenes SET peer_reviewer_id = ? WHERE id = ?", (reviewer_id, scene_id))
        conn.execute(
            "UPDATE scenes SET status = ?, claimed_by = NULL, updated_at = datetime('now') WHERE id = ?",
            (new_status, scene_id)
        )
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, reviewer_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(conn, _write)


def record_supervisor_review(conn, scene_id, supervisor_id, new_status, stage, decision, comments):
    """Set the supervisor, transition status, and log the decision. Used for
    both approve/kick-back and the 'mark issues' shortcut."""
    def _write():
        conn.execute("UPDATE scenes SET supervisor_id = ? WHERE id = ?", (supervisor_id, scene_id))
        conn.execute(
            "UPDATE scenes SET status = ?, claimed_by = NULL, updated_at = datetime('now') WHERE id = ?",
            (new_status, scene_id)
        )
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, supervisor_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(conn, _write)


def record_force_release(conn, scene_id, supervisor_id, stage, decision, comments):
    """Release a stuck claim (supervisor claim at 6, or an analyst's claimed/
    in-review scene at 1/3) back to the appropriate pool and log the admin
    action. Caller is responsible for checking the scene has a releasable
    claim before calling this."""
    scene = get_scene_by_id(conn, scene_id)
    if scene is None:
        return
    if scene['status'] == 6:
        release_sql = "UPDATE scenes SET status = 5, claimed_by = NULL, updated_at = datetime('now') WHERE id = ?"
    elif scene['status'] == 1:
        release_sql = """UPDATE scenes
                        SET status = 0, owner_id = NULL, claimed_by = NULL,
                            updated_at = datetime('now')
                        WHERE id = ?"""
    elif scene['status'] == 3:
        release_sql = "UPDATE scenes SET status = 2, claimed_by = NULL, updated_at = datetime('now') WHERE id = ?"
    else:
        return
    def _write():
        conn.execute(release_sql, (scene_id,))
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, supervisor_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(conn, _write)


def record_scene_edit(conn, scene_id, new_status, owner_id, peer_reviewer_id, supervisor_id, claimed_by,
                        acting_supervisor_id, stage, decision, comments):
    """Reassign a scene's status/owner/peer-reviewer/supervisor/claim (supervisor
    admin edit) and log the change. Unlike the normal workflow transitions,
    this intentionally bypasses the set-once rule for the three assignment
    fields -- it exists for correcting mis-assigned scenes."""
    def _write():
        conn.execute("""
            UPDATE scenes
            SET status = ?, owner_id = ?, peer_reviewer_id = ?, supervisor_id = ?, claimed_by = ?,
                updated_at = datetime('now')
            WHERE id = ?
        """, (new_status, owner_id, peer_reviewer_id, supervisor_id, claimed_by, scene_id))
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, acting_supervisor_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(conn, _write)


def record_scene_reset(conn, scene_id, acting_supervisor_id, stage, decision, comments):
    """Wipe all ownership on a scene (supervisor admin reset) and log it."""
    def _write():
        conn.execute("""
            UPDATE scenes
            SET status = 0,
                owner_id = NULL,
                peer_reviewer_id = NULL,
                supervisor_id = NULL,
                claimed_by = NULL,
                updated_at = datetime('now')
            WHERE id = ?
        """, (scene_id,))
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, acting_supervisor_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(conn, _write)


def get_all_scenes(conn):
    """master list of every scene with all user fields resolved to usernames"""
    return _read_all(conn, """
        SELECT
            s.id,
            s.name,
            s.scene_key,
            s.status,
            s.flags,
            s.updated_at,
            o.username  AS owner_username,
            pr.username AS peer_reviewer_username,
            sv.username AS supervisor_username,
            cb.username AS claimed_by_username
        FROM scenes s
        LEFT JOIN users o  ON s.owner_id          = o.id
        LEFT JOIN users pr ON s.peer_reviewer_id   = pr.id
        LEFT JOIN users sv ON s.supervisor_id      = sv.id
        LEFT JOIN users cb ON s.claimed_by         = cb.id
        ORDER BY s.id
    """)


def get_scene_history(conn, scene_id):
    return _read_all(conn, """
        SELECT r.*, u.username AS reviewer_name
        FROM reviews r
        JOIN users u ON r.reviewer_id = u.id
        WHERE r.scene_id = ?
        ORDER BY r.timestamp DESC
    """, (scene_id,))


def add_note(conn, scene_id, author_id, body):
    def _write():
        conn.execute(
            "INSERT INTO notes (scene_id, author_id, body, timestamp) VALUES (?, ?, ?, datetime('now'))",
            (scene_id, author_id, body)
        )
        conn.execute(
            "UPDATE scenes SET updated_at = datetime('now') WHERE id = ?",
            (scene_id,)
        )
        conn.commit()
    _with_lock_retry(conn, _write)


def update_note(conn, note_id, body):
    """Edit a manually-authored note. Reviews are append-only and never editable —
    only rows in the notes table can be targeted here."""
    def _write():
        conn.execute("UPDATE notes SET body = ? WHERE id = ?", (body, note_id))
        conn.commit()
    _with_lock_retry(conn, _write)


def delete_note(conn, note_id):
    def _write():
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
    _with_lock_retry(conn, _write)


def get_scene_thread(conn, scene_id):
    """Interleaved notes + review-comments for a scene, oldest first.
    Each row: type ('note'/'review'), id, timestamp, author_name, author_id,
    content, decision. Only type='note' rows are ever editable/deletable.
    Reviews are an append-only audit log."""
    return _read_all(conn, """
        SELECT 'note' AS type, n.id AS id, n.timestamp, u.username AS author_name,
                n.author_id AS author_id, n.body AS content, NULL AS decision
        FROM notes n
        JOIN users u ON n.author_id = u.id
        WHERE n.scene_id = ?
        UNION ALL
        SELECT 'review' AS type, r.id AS id, r.timestamp, u.username AS author_name,
                r.reviewer_id AS author_id, r.comments AS content, r.decision
        FROM reviews r
        JOIN users u ON r.reviewer_id = u.id
        WHERE r.scene_id = ?
        ORDER BY timestamp ASC
    """, (scene_id, scene_id))


def add_science_note(conn, scene_id, author_id, body):
    def _write():
        conn.execute(
            "INSERT INTO science_notes (scene_id, author_id, body, timestamp) VALUES (?, ?, ?, datetime('now'))",
            (scene_id, author_id, body)
        )
        conn.execute(
            "UPDATE scenes SET updated_at = datetime('now') WHERE id = ?",
            (scene_id,)
        )
        conn.commit()
    _with_lock_retry(conn, _write)


def update_science_note(conn, note_id, body):
    def _write():
        conn.execute("UPDATE science_notes SET body = ? WHERE id = ?", (body, note_id))
        conn.commit()
    _with_lock_retry(conn, _write)


def delete_science_note(conn, note_id):
    def _write():
        conn.execute("DELETE FROM science_notes WHERE id = ?", (note_id,))
        conn.commit()
    _with_lock_retry(conn, _write)


def get_science_notes(conn, scene_id):
    """Manually authored science notes for a scene, oldest first."""
    return _read_all(conn, """
        SELECT 'note' AS type, n.id AS id, n.timestamp, u.username AS author_name,
                n.author_id AS author_id, n.body AS content
        FROM science_notes n
        JOIN users u ON n.author_id = u.id
        WHERE n.scene_id = ?
        ORDER BY n.timestamp ASC
    """, (scene_id,))


def get_analyst_in_progress(conn, user_id):
    """Scenes this analyst has contributed to that are still in the pipeline.
    Owned scenes at status 2/3/5 (submitted and being processed), plus scenes
    they peer-reviewed at status 4/5 (decision made, scene still moving)."""
    return _read_all(conn, """
        SELECT s.*,
                o.username AS owner_username,
                cb.username AS current_holder,
                CASE WHEN s.owner_id = ? THEN 'Owner' ELSE 'Peer Reviewer' END AS my_role
        FROM scenes s
        LEFT JOIN users o  ON s.owner_id   = o.id
        LEFT JOIN users cb ON s.claimed_by = cb.id
        WHERE (s.owner_id = ? AND s.status IN (2, 3, 5, 6))
            OR (s.peer_reviewer_id = ? AND s.status IN (4, 5, 6))
        ORDER BY s.updated_at DESC
    """, (user_id, user_id, user_id))


def get_analyst_completed(conn, user_id):
    """Scenes the analyst was involved in that have reached APPROVED (status 7)."""
    return _read_all(conn, """
        SELECT s.*,
                o.username AS owner_username,
                CASE WHEN s.owner_id = ? THEN 'Owner' ELSE 'Peer Reviewer' END AS my_role
        FROM scenes s
        LEFT JOIN users o ON s.owner_id = o.id
        WHERE s.status = 7
            AND (s.owner_id = ? OR s.peer_reviewer_id = ?)
        ORDER BY s.updated_at DESC
    """, (user_id, user_id, user_id))


def get_supervisor_in_progress(conn, user_id):
    """Scenes this supervisor has kicked back that are still being revised (status 4)."""
    return _read_all(conn, """
        SELECT s.*, o.username AS owner_username
        FROM scenes s
        LEFT JOIN users o ON s.owner_id = o.id
        WHERE s.supervisor_id = ? AND s.status = 4
        ORDER BY s.updated_at DESC
    """, (user_id,))


# ── Connection and initialization ─────────────────────────────────────────────

# Monday of the current calendar week (Mon-Sun), used for "_week" stat counts.
_WEEK_START_SQL = "DATE('now','localtime','weekday 0','-6 days')"
# Monday/Sunday of the previous calendar week, used for "_last" stat counts.
_LAST_WEEK_START_SQL = f"DATE({_WEEK_START_SQL},'-7 days')"
_LAST_WEEK_END_SQL = f"DATE({_WEEK_START_SQL},'-1 days')"
_TODAY_SQL = "DATE('now','localtime')"
# Stored timestamps are UTC, so convert before bucketing: the day boundary has
# to be the reader's midnight, not UTC's (which falls mid-afternoon locally).
_LOCAL_DAY_SQL = "DATE(r.timestamp,'localtime')"


def _period_counts(conn, from_where_sql, params, distinct_col=None):
    """Run one query computing {total, week, last, today} counts via
    conditional aggregation on r.timestamp, instead of separate near-identical
    queries. 'last' is the previous calendar week (Mon-Sun). Pass
    distinct_col='r.scene_id' to count distinct scenes; otherwise counts rows.
    from_where_sql is a fixed (non-user-input) 'table ... WHERE ...' fragment
    referencing the reviews table as 'r'."""
    if distinct_col:
        total_expr = f"COUNT(DISTINCT {distinct_col})"
        week_expr = f"COUNT(DISTINCT CASE WHEN {_LOCAL_DAY_SQL}>={_WEEK_START_SQL} THEN {distinct_col} END)"
        last_expr = (f"COUNT(DISTINCT CASE WHEN {_LOCAL_DAY_SQL}>={_LAST_WEEK_START_SQL} "
                    f"AND {_LOCAL_DAY_SQL}<={_LAST_WEEK_END_SQL} THEN {distinct_col} END)")
        today_expr = f"COUNT(DISTINCT CASE WHEN {_LOCAL_DAY_SQL}={_TODAY_SQL} THEN {distinct_col} END)"
    else:
        total_expr = "COUNT(*)"
        week_expr = f"COUNT(CASE WHEN {_LOCAL_DAY_SQL}>={_WEEK_START_SQL} THEN 1 END)"
        last_expr = (f"COUNT(CASE WHEN {_LOCAL_DAY_SQL}>={_LAST_WEEK_START_SQL} "
                    f"AND {_LOCAL_DAY_SQL}<={_LAST_WEEK_END_SQL} THEN 1 END)")
        today_expr = f"COUNT(CASE WHEN {_LOCAL_DAY_SQL}={_TODAY_SQL} THEN 1 END)"
    # The fallback is unreachable -- an aggregate always returns one row -- but
    # nothing in the types says so, and four columns do not fit _read_scalar.
    row = _read_one(
        conn,
        f"SELECT {total_expr}, {week_expr}, {last_expr}, {today_expr} FROM {from_where_sql}", params
    ) or (0, 0, 0, 0)
    return {'total': row[0], 'week': row[1], 'last': row[2], 'today': row[3]}


def _submitted_counts(conn, user_id):
    """Distinct scenes user_id submitted (as owner)."""
    return _period_counts(
        conn, "reviews r WHERE r.reviewer_id=? AND r.decision=?",
        (user_id, Decision.SUBMITTED), distinct_col='r.scene_id')


def _peer_reviewed_counts(conn, user_id):
    """Peer-review decisions user_id made on others' scenes."""
    return _period_counts(
        conn, "reviews r WHERE r.reviewer_id=? AND r.stage=?",
        (user_id, Stage.PEER_REVIEW))


def _approved_counts(conn, user_id, owner_column):
    """Supervisor approvals for scenes where scenes.<owner_column> = user_id.
    owner_column is a fixed internal column name ('owner_id' or
    'supervisor_id'), never user input."""
    return _period_counts(
        conn,
        f"reviews r JOIN scenes s ON r.scene_id=s.id "
        f"WHERE s.{owner_column}=? AND r.stage=? AND r.decision=?",
        (user_id, Stage.SUPERVISOR_REVIEW, Decision.APPROVED))


def _kickback_counts(conn, user_id, owner_column):
    """Distinct scenes where scenes.<owner_column> = user_id that were kicked
    back (any stage, peer or supervisor) at least once in each period. A
    scene kicked back more than once in the period still counts once."""
    return _period_counts(
        conn,
        f"reviews r JOIN scenes s ON r.scene_id=s.id "
        f"WHERE s.{owner_column}=? AND r.decision=?",
        (user_id, Decision.NEEDS_REVISION), distinct_col='r.scene_id')


def _my_multi_kick_count(conn, user_id):
    """Count of distinct scenes kicked back 2+ times by one supervisor in a
    given period."""
    return _period_counts(
        conn,
        "("
        "  SELECT r.scene_id, r.timestamp,"
        "         ROW_NUMBER() OVER (PARTITION BY r.scene_id"
        "                            ORDER BY r.timestamp, r.id) AS kick_num"
        "  FROM reviews r JOIN scenes s ON r.scene_id=s.id"
        "  WHERE s.supervisor_id=? AND r.stage=? AND r.decision=?"
        ") r WHERE r.kick_num >= 2",
        (user_id, Stage.SUPERVISOR_REVIEW, Decision.NEEDS_REVISION),
        distinct_col='r.scene_id'
    )
    
    
def _multi_kickback_counts(conn, user_id):
    """Distinct scenes owned by user_id that needed 2+ rounds of SUPERVISOR
    revision. ROW_NUMBER stamps each supervisor kick-back with its position in
    that scene's kick order, and only the 2nd and later ones are counted, so a
    scene lands in the period where it crossed the two-kick line. The all-time
    total is therefore every scene that ever reached two kicks."""
    return _period_counts(
        conn,
        "("
        "  SELECT r.scene_id, r.timestamp,"
        "         ROW_NUMBER() OVER (PARTITION BY r.scene_id"
        "                            ORDER BY r.timestamp, r.id) AS kick_num"
        "  FROM reviews r JOIN scenes s ON r.scene_id=s.id"
        "  WHERE s.owner_id=? AND r.stage=? AND r.decision=?"
        ") r WHERE r.kick_num >= 2",
        (user_id, Stage.SUPERVISOR_REVIEW, Decision.NEEDS_REVISION),
        distinct_col='r.scene_id')


def _multi_kickback_ratio(conn, user_id, multi_kickback_scenes):
    """All-time only -- of the scenes user_id has completed (status=APPROVED),
    what fraction needed 2+ rounds of SUPERVISOR revision along the way.
    Approximate: scenes with 2+ kicks that haven't reached APPROVED yet count
    toward neither the numerator nor the denominator."""
    completed_scenes = _read_scalar(
        conn,
        "SELECT COUNT(*) FROM scenes WHERE owner_id=? AND status=?",
        (user_id, SceneStatus.APPROVED)
    )
    rate = round(multi_kickback_scenes / completed_scenes, 2) if completed_scenes else 0.0
    return {
        'completed_scenes_total': completed_scenes,
        'multi_kickback_rate_total': rate,
    }


def get_user_stats(conn, user_id):
    """Stat counts for one analyst, derived from the reviews + scenes tables."""
    submitted = _submitted_counts(conn, user_id)
    peer_reviewed = _peer_reviewed_counts(conn, user_id)
    approved = _approved_counts(conn, user_id, 'owner_id')
    multi_kickbacks = _multi_kickback_counts(conn, user_id)

    stats = {
        'submitted_total': submitted['total'],
        'submitted_week': submitted['week'],
        'submitted_last': submitted['last'],
        'submitted_today': submitted['today'],
        'peer_reviewed_total': peer_reviewed['total'],
        'peer_reviewed_week': peer_reviewed['week'],
        'peer_reviewed_last': peer_reviewed['last'],
        'peer_reviewed_today': peer_reviewed['today'],
        'approved_total': approved['total'],
        'approved_week': approved['week'],
        'approved_last': approved['last'],
        'approved_today': approved['today'],
        'multi_kickback_scenes_total': multi_kickbacks['total'],
        'multi_kickback_scenes_week': multi_kickbacks['week'],
        'multi_kickback_scenes_last': multi_kickbacks['last'],
        'multi_kickback_scenes_today': multi_kickbacks['today'],
    }
    stats.update(_multi_kickback_ratio(conn, user_id, multi_kickbacks['total']))
    return stats


def get_supervisor_stats(conn, user_id):
    """Stat counts for one supervisor, derived from the reviews + scenes tables."""
    approved = _approved_counts(conn, user_id, 'supervisor_id')
    kickbacks = _kickback_counts(conn, user_id, 'supervisor_id')
    return {
        'approved_total': approved['total'],
        'approved_week': approved['week'],
        'approved_today': approved['today'],
        'kicked_back_total': kickbacks['total'],
        'kicked_back_week': kickbacks['week'],
        'kicked_back_today': kickbacks['today'],
        'multi_kickback_scenes_total': _my_multi_kick_count(conn, user_id)['total'],
        'multi_kickback_scenes_week': _my_multi_kick_count(conn, user_id)['week'],
        'multi_kickback_scenes_today': _my_multi_kick_count(conn, user_id)['today'],
    }


def get_all_user_stats(conn):
    """Return [(user_row, stats_dict)] for all active users, sorted by username."""
    users = _read_all(
        conn, "SELECT id, username, role FROM users WHERE active=1 ORDER BY username"
    )
    return [(u, get_user_stats(conn, u['id'])) for u in users]


def get_supervisor_analyst_coverage(conn, supervisor_id):
    """One row per active analyst: how much of that analyst's work this
    supervisor is carrying, for spreading their time across the team.

    'in_progress' and 'approved_mine' count only scenes stamped with this
    supervisor; 'approved_any' counts the analyst's approved scenes whoever
    signed them off, so the pair shows whether a low share means the analyst is
    being missed or simply covered by someone else. LEFT JOIN so an analyst with
    no scenes still gets a row of zeros rather than dropping out of the list."""
    return _read_all(
        conn,
        """
        SELECT u.id, u.username,
                SUM(CASE WHEN s.supervisor_id = ? AND s.status IN (?, ?)
                        THEN 1 ELSE 0 END) AS in_progress,
                SUM(CASE WHEN s.supervisor_id = ? AND s.status = ?
                        THEN 1 ELSE 0 END) AS approved_mine,
                SUM(CASE WHEN s.status = ? THEN 1 ELSE 0 END) AS approved_any
        FROM users u
        LEFT JOIN scenes s ON s.owner_id = u.id
        WHERE u.active = 1 AND u.role = ?
        GROUP BY u.id, u.username
        ORDER BY u.username
        """,
        (supervisor_id, SceneStatus.NEEDS_REVISION, SceneStatus.IN_SUPERVISOR_REVIEW,
        supervisor_id, SceneStatus.APPROVED,
        SceneStatus.APPROVED,
        Role.ANALYST))


def get_all_supervisor_stats(conn):
    """Return [(user_row, stats_dict)] for all active supervisors, sorted by username."""
    users = _read_all(
        conn, "SELECT id, username, role FROM users WHERE active=1 AND role='supervisor' ORDER BY username"
    )
    return [(u, get_supervisor_stats(conn, u['id'])) for u in users]


def get_owned_activity_since(conn, user_id, since):
    """Scenes owned by user_id that a reviewer acted on after `since`, for the
    While You Were Away summary. Returns {'approved': n, 'kicked_back': n}."""
    row = _read_one(
        conn,
        """
        SELECT COUNT(DISTINCT CASE WHEN r.stage = ? AND r.decision = ?
                                    THEN r.scene_id END),
                COUNT(DISTINCT CASE WHEN r.decision = ? THEN r.scene_id END)
        FROM reviews r
        JOIN scenes s ON r.scene_id = s.id
        WHERE s.owner_id = ? AND r.timestamp > ?
        """,
        (Stage.SUPERVISOR_REVIEW, Decision.APPROVED, Decision.NEEDS_REVISION,
        user_id, since)
    ) or (0, 0)
    return {'approved': row[0], 'kicked_back': row[1]}


def _run_migrations(conn):
    from app.migrations import MIGRATIONS
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id         TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}
    for m in MIGRATIONS:
        if m.id not in applied:
            m.up(conn)
            conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (m.id,))
            conn.commit()


def _connect():
    """Open a raw connection with ROVR's required settings. Reconnecting goes
    through here too, so a replacement handle is configured identically."""
    # timeout=1.0 sets SQLite's own busy handler, so every statement already
    # retries internally for up to 1s before raising "database is locked"
    conn = sqlite3.connect(DB_PATH, timeout=1.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


class _ReconnectingConnection:
    """Holds the real sqlite3 handle so it can be replaced underneath the app.

    One connection is opened at startup and handed to the login window, both
    dashboards and every dialog, and it survives logout. A dropped network
    session kills that handle but none of those references, so the handle is
    swapped in place instead and nobody has to be told.
    """

    def __init__(self):
        self._inner = _connect()

    # Explicit rather than left to __getattr__, so each call re-resolves
    # _inner. A bound method captured before a swap would keep talking to the
    # dead handle, and _with_lock_retry passes conn.rollback around as exactly
    # that.
    def execute(self, *args):
        return self._inner.execute(*args)

    def commit(self):
        return self._inner.commit()

    def rollback(self):
        return self._inner.rollback()

    def cursor(self, *args):
        return self._inner.cursor(*args)

    def close(self):
        self._inner.close()

    def __getattr__(self, name):
        if name == '_inner':  # only before __init__ finishes; else infinite recursion
            raise AttributeError(name)
        return getattr(self._inner, name)

    def _swap(self, fresh):
        """Install a fresh handle, discarding the old one best-effort. The
        rollback and close release whatever locks it still holds if it is only
        half dead."""
        old, self._inner = self._inner, fresh
        for op in (old.rollback, old.close):
            try:
                op()
            except sqlite3.Error:
                pass


def get_db_connection():
    return _ReconnectingConnection()


def keepalive(conn):
    """Ping the database so an idle network session is not dropped, replacing
    the handle if it has already died. Runs on a timer. Returns False if the 
    drive is unreachable."""
    try:
        _with_read_retry(conn, lambda: conn.execute(_PROBE_SQL).fetchone(), retries=1)
        return True
    except sqlite3.Error:
        return False

def initialize_db():
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT NOT NULL UNIQUE,
            active          INTEGER NOT NULL,
            password_hash   TEXT NOT NULL,
            role            TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scenes (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            -- workflow fields
            scene_key               TEXT NOT NULL UNIQUE,
            name                    TEXT NOT NULL,
            roi_filename            TEXT UNIQUE,
            status                  INTEGER NOT NULL DEFAULT 0,
            owner_id                INTEGER REFERENCES users (id),
            peer_reviewer_id        INTEGER REFERENCES users (id),
            supervisor_id           INTEGER REFERENCES users (id),
            claimed_by              INTEGER REFERENCES users (id),
            submitted_at            TEXT,
            updated_at              TEXT DEFAULT (datetime('now')),
            -- CSV metadata (representative first-row values for the filter group)
            fn                      TEXT,
            rover                   TEXT,
            sclk                    INTEGER,
            product_type            TEXT,
            site                    INTEGER,
            pos                     INTEGER,
            seq_id                  TEXT,
            filter                  TEXT,
            version                 INTEGER,
            sol                     INTEGER,
            seq_ver                 INTEGER,
            lines                   INTEGER,
            pma                     INTEGER,
            obs_ix                  INTEGER,
            frame_type              TEXT,
            ltst                    TEXT,
            product_creation_time   TEXT,
            compression             TEXT,
            first_line              INTEGER,
            first_sample            INTEGER,
            samples                 INTEGER,
            solar_elevation         REAL,
            instrument_elevation    REAL,
            instrument_azimuth      REAL,
            solar_azimuth           REAL,
            incidence_angle         REAL,
            emission_angle          REAL,
            phase_angle             REAL,
            tau                     REAL,
            rover_elevation         REAL,
            lon                     REAL,
            lat                     REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id        INTEGER NOT NULL REFERENCES scenes (id),
            reviewer_id     INTEGER NOT NULL REFERENCES users (id),
            timestamp       TEXT NOT NULL DEFAULT (datetime('now')),
            stage           TEXT NOT NULL,
            decision        TEXT NOT NULL,
            comments        TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id    INTEGER NOT NULL REFERENCES scenes (id),
            author_id   INTEGER NOT NULL REFERENCES users (id),
            timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
            body        TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS science_notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id    INTEGER NOT NULL REFERENCES scenes (id),
            author_id   INTEGER NOT NULL REFERENCES users (id),
            timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
            body        TEXT NOT NULL
        )
    """)
    conn.commit()
    _run_migrations(conn)
    conn.close()
