# app/db.py
"""all SQLite operations, including creating tables and inserting data"""
import sqlite3
import time
from config import DB_PATH


def _with_lock_retry(fn, retries=25, delay=0.1):
    """Run fn() (a DB write), retrying if SQLite reports the database is
    locked by another writer. DB_PATH lives on a shared network drive, so a
    second user's near-simultaneous write is expected to occasionally collide
    here -- it should resolve within a second or two once their transaction
    commits. Every write in this module goes through this helper (or the
    connection's own short busy timeout, see get_db_connection) so that a
    lock never surfaces as a crash.

    Each attempt's own busy timeout (see get_db_connection) already does the
    real waiting -- it retries internally at a fine grain and returns the
    instant the lock clears, rather than blocking for the full timeout
    regardless. So by the time this except block runs, that 1s window has
    already been spent failing; there is nothing to gain from sleeping long
    here too. delay=0.1 just avoids hammering the network share back-to-back
    while genuinely waiting out a longer hold. Combined with the connection's
    own 1s busy timeout, worst case is a bounded ~27s of retrying before
    giving up: fast in the common case (a real collision clears in well under
    a second, often on the very first attempt), short enough that the UI
    (frozen for the duration, since this runs on Qt's main thread) doesn't
    hang for long if the network drive genuinely drops. Re-raises whatever it
    last saw once retries are exhausted, or immediately for any other kind of
    error (those aren't going to be fixed by waiting)."""
    for attempt in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if 'locked' not in str(e).lower() or attempt == retries - 1:
                raise
            time.sleep(delay)


# ── User functions ────────────────────────────────────────────────────────────

def get_user_by_username(conn, username):
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

def get_user_by_id(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

def get_all_active_analysts(conn):
    return conn.execute("SELECT * FROM users WHERE active = 1 AND role = 'analyst'").fetchall()

def get_all_users(conn):
    return conn.execute("SELECT * FROM users ORDER BY role, username").fetchall()

def create_user(conn, username, password_hash, role):
    def _write():
        conn.execute(
            "INSERT INTO users (username, active, password_hash, role) VALUES (?, 1, ?, ?)",
            (username, password_hash, role)
        )
        conn.commit()
    _with_lock_retry(_write)

def activate_user(conn, user_id):
    def _write():
        conn.execute("UPDATE users SET active = 1 WHERE id = ?", (user_id,))
        conn.commit()
    _with_lock_retry(_write)

def deactivate_user(conn, user_id):
    """Deactivate user and return their open scenes to shared pools.
    Status 1/4 (owner's work) → 0: ownership cleared so a new analyst can claim fresh.
    Status 3 where the deactivated user is the owner → 2: their scene returns to the
    peer review pool so a different analyst can still review it.
    Status 3 where the deactivated user is the peer reviewer (claimed_by) → 2: their
    claim is released so another analyst can pick up the review.
    Status 6 (supervisor's claimed scene) → 5: released back to the supervisor pool."""
    def _write():
        conn.execute("""
            UPDATE scenes
            SET status = 0, owner_id = NULL, claimed_by = NULL,
                updated_at = datetime('now', 'localtime')
            WHERE owner_id = ? AND status IN (1, 4)
        """, (user_id,))
        conn.execute("""
            UPDATE scenes
            SET status = 2, claimed_by = NULL, updated_at = datetime('now', 'localtime')
            WHERE owner_id = ? AND status = 3
        """, (user_id,))
        conn.execute("""
            UPDATE scenes
            SET status = 2, claimed_by = NULL, updated_at = datetime('now', 'localtime')
            WHERE claimed_by = ? AND status = 3
        """, (user_id,))
        conn.execute("""
            UPDATE scenes
            SET status = 5, claimed_by = NULL, updated_at = datetime('now', 'localtime')
            WHERE claimed_by = ? AND status = 6
        """, (user_id,))
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
        conn.commit()
    _with_lock_retry(_write)

def update_user_role(conn, user_id, new_role):
    def _write():
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
        conn.commit()
    _with_lock_retry(_write)

def update_user_password(conn, user_id, new_password_hash):
    def _write():
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_password_hash, user_id))
        conn.commit()
    _with_lock_retry(_write)

def update_username(conn, user_id, new_username):
    def _write():
        conn.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user_id))
        conn.commit()
    _with_lock_retry(_write)


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
#   8 issues                — flagged as problematic (terminal-ish)

def create_scene(conn, name, scene_key, roi_filename=None, owner_id=None):
    """create a scene; owner_id and roi_filename are None for pool-imported scenes
    (owner set at claim time, roi_filename set when analyst saves the .sel file)"""
    def _write():
        conn.execute(
            "INSERT INTO scenes (name, scene_key, roi_filename, owner_id, status) VALUES (?, ?, ?, ?, 0)",
            (name, scene_key, roi_filename, owner_id)
        )
        conn.commit()
    _with_lock_retry(_write)

def get_scene_by_id(conn, scene_id):
    return conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()

def claim_from_pool(conn, scene_id, analyst_id):
    """analyst 1 atomically claims an unclaimed scene (0 → 1), setting owner if not yet assigned"""
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
    return _with_lock_retry(_write)

def claim_for_review(conn, scene_id, analyst_id):
    """analyst 2 atomically claims a scene for peer review (2 → 3)"""
    def _write():
        cur = conn.execute(
            "UPDATE scenes SET status = 3, claimed_by = ? WHERE id = ? AND status = 2 AND owner_id != ?",
            (analyst_id, scene_id, analyst_id)
        )
        conn.commit()
        return cur.rowcount == 1
    return _with_lock_retry(_write)

def release_scene(conn, scene_id):
    """release a claimed scene back to the appropriate pool (1 → 0, or 3 → 2)"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] == 1:
        # Returning to Scene Pool — clear ownership so the next claimer starts fresh
        sql = """UPDATE scenes
                 SET status = 0, owner_id = NULL, claimed_by = NULL,
                     updated_at = datetime('now', 'localtime')
                 WHERE id = ?"""
    elif scene['status'] == 3:
        # Returning to Peer Review Pool — only clear the reviewer's claim
        sql = "UPDATE scenes SET status = 2, claimed_by = NULL, updated_at = datetime('now', 'localtime') WHERE id = ?"
    else:
        return
    def _write():
        conn.execute(sql, (scene_id,))
        conn.commit()
    _with_lock_retry(_write)


# ── Queue getters ─────────────────────────────────────────────────────────────

def get_scene_pool(conn):
    """all unclaimed scenes available for any analyst to claim (status 0)"""
    return conn.execute("SELECT * FROM scenes WHERE status = 0").fetchall()

def get_analyst_queue(conn, user_id):
    """scenes in analyst's personal to-do: owned (1, 4) and claimed for peer review (3)"""
    return conn.execute(
        """SELECT scenes.*, users.username AS owner_username
           FROM scenes
           LEFT JOIN users ON scenes.owner_id = users.id
           WHERE (scenes.status IN (1, 4) AND scenes.owner_id = ?)
              OR (scenes.status = 3 AND scenes.claimed_by = ?)
           ORDER BY CASE WHEN scenes.status = 4 THEN 0 ELSE 1 END,
                    scenes.updated_at DESC""",
        (user_id, user_id)
    ).fetchall()

def get_ready_queue(conn):
    """scenes available for peer review (status 2, shared pool)"""
    return conn.execute("""
        SELECT scenes.*, users.username AS owner_username
        FROM scenes
        LEFT JOIN users ON scenes.owner_id = users.id
        WHERE scenes.status = 2
    """).fetchall()

def claim_for_supervisor_review(conn, scene_id, supervisor_id):
    """supervisor atomically claims a scene from the supervisor pool (5 → 6)"""
    def _write():
        cur = conn.execute(
            "UPDATE scenes SET status = 6, claimed_by = ?, updated_at = datetime('now', 'localtime') WHERE id = ? AND status = 5",
            (supervisor_id, scene_id)
        )
        conn.commit()
        return cur.rowcount == 1
    return _with_lock_retry(_write)

def release_supervisor_review(conn, scene_id):
    """return a supervisor-claimed scene to the supervisor pool (6 → 5)"""
    def _write():
        conn.execute(
            "UPDATE scenes SET status = 5, claimed_by = NULL, updated_at = datetime('now', 'localtime') WHERE id = ? AND status = 6",
            (scene_id,)
        )
        conn.commit()
    _with_lock_retry(_write)

def update_scene_flags(conn, scene_id, flags_str):
    """update the flags column on a scene"""
    def _write():
        conn.execute(
            "UPDATE scenes SET flags = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (flags_str, scene_id)
        )
        conn.commit()
    _with_lock_retry(_write)

def get_supervisor_queue(conn):
    """scenes awaiting supervisor review (status 5, shared pool)"""
    return conn.execute("""
        SELECT scenes.*, users.username AS owner_username
        FROM scenes
        LEFT JOIN users ON scenes.owner_id = users.id
        WHERE scenes.status = 5
    """).fetchall()

def get_supervisor_my_queue(conn, supervisor_id):
    """scenes this supervisor has claimed for review (status 6)"""
    return conn.execute("""
        SELECT scenes.*, users.username AS owner_username
        FROM scenes
        LEFT JOIN users ON scenes.owner_id = users.id
        WHERE scenes.status = 6 AND scenes.claimed_by = ?
    """, (supervisor_id,)).fetchall()

def get_issues_queue(conn):
    """all scenes in issues status (status 8)"""
    return conn.execute("""
        SELECT scenes.*, users.username AS owner_username
        FROM scenes
        LEFT JOIN users ON scenes.owner_id = users.id
        WHERE scenes.status = 8
        ORDER BY scenes.updated_at DESC
    """).fetchall()


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
    "VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))"
)


def record_submission(conn, scene_id, new_status, claimed_by, analyst_id, stage, decision, comments):
    """Transition a scene on analyst submission/resubmission and log it."""
    def _write():
        conn.execute(
            "UPDATE scenes SET status = ?, claimed_by = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (new_status, claimed_by, scene_id)
        )
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, analyst_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(_write)


def record_peer_review(conn, scene_id, reviewer_id, new_status, stage, decision, comments):
    """Set the peer reviewer, transition status, and log the decision."""
    def _write():
        conn.execute("UPDATE scenes SET peer_reviewer_id = ? WHERE id = ?", (reviewer_id, scene_id))
        conn.execute(
            "UPDATE scenes SET status = ?, claimed_by = NULL, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (new_status, scene_id)
        )
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, reviewer_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(_write)


def record_supervisor_review(conn, scene_id, supervisor_id, new_status, stage, decision, comments):
    """Set the supervisor, transition status, and log the decision. Used for
    both approve/kick-back and the 'mark issues' shortcut."""
    def _write():
        conn.execute("UPDATE scenes SET supervisor_id = ? WHERE id = ?", (supervisor_id, scene_id))
        conn.execute(
            "UPDATE scenes SET status = ?, claimed_by = NULL, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (new_status, scene_id)
        )
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, supervisor_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(_write)


def record_force_release(conn, scene_id, supervisor_id, stage, decision, comments):
    """Release a stuck claim (supervisor claim at 6, or an analyst's claimed/
    in-review scene at 1/3) back to the appropriate pool and log the admin
    action. Caller is responsible for checking the scene has a releasable
    claim before calling this."""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] == 6:
        release_sql = "UPDATE scenes SET status = 5, claimed_by = NULL, updated_at = datetime('now', 'localtime') WHERE id = ?"
    elif scene['status'] == 1:
        release_sql = """UPDATE scenes
                          SET status = 0, owner_id = NULL, claimed_by = NULL,
                              updated_at = datetime('now', 'localtime')
                          WHERE id = ?"""
    elif scene['status'] == 3:
        release_sql = "UPDATE scenes SET status = 2, claimed_by = NULL, updated_at = datetime('now', 'localtime') WHERE id = ?"
    else:
        return
    def _write():
        conn.execute(release_sql, (scene_id,))
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, supervisor_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(_write)


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
                updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (new_status, owner_id, peer_reviewer_id, supervisor_id, claimed_by, scene_id))
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, acting_supervisor_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(_write)


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
                updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (scene_id,))
        conn.execute(_REVIEW_INSERT_SQL, (scene_id, acting_supervisor_id, stage, decision, comments))
        conn.commit()
    _with_lock_retry(_write)


def get_all_scenes(conn):
    """master list of every scene with all user fields resolved to usernames"""
    return conn.execute("""
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
    """).fetchall()


def get_scene_history(conn, scene_id):
    return conn.execute("""
        SELECT r.*, u.username AS reviewer_name
        FROM reviews r
        JOIN users u ON r.reviewer_id = u.id
        WHERE r.scene_id = ?
        ORDER BY r.timestamp DESC
    """, (scene_id,)).fetchall()


def add_note(conn, scene_id, author_id, body):
    def _write():
        conn.execute(
            "INSERT INTO notes (scene_id, author_id, body, timestamp) VALUES (?, ?, ?, datetime('now', 'localtime'))",
            (scene_id, author_id, body)
        )
        conn.execute(
            "UPDATE scenes SET updated_at = datetime('now', 'localtime') WHERE id = ?",
            (scene_id,)
        )
        conn.commit()
    _with_lock_retry(_write)


def update_note(conn, note_id, body):
    """Edit a manually-authored note. Reviews are append-only and never editable —
    only rows in the notes table can be targeted here."""
    def _write():
        conn.execute("UPDATE notes SET body = ? WHERE id = ?", (body, note_id))
        conn.commit()
    _with_lock_retry(_write)


def delete_note(conn, note_id):
    def _write():
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
    _with_lock_retry(_write)


def get_scene_thread(conn, scene_id):
    """Interleaved notes + review-comments for a scene, oldest first.
    Each row: type ('note'/'review'), id, timestamp, author_name, author_id,
    content, decision. Only type='note' rows are ever editable/deletable —
    reviews are an append-only audit log."""
    return conn.execute("""
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
    """, (scene_id, scene_id)).fetchall()


def add_science_note(conn, scene_id, author_id, body):
    def _write():
        conn.execute(
            "INSERT INTO science_notes (scene_id, author_id, body, timestamp) VALUES (?, ?, ?, datetime('now', 'localtime'))",
            (scene_id, author_id, body)
        )
        conn.execute(
            "UPDATE scenes SET updated_at = datetime('now', 'localtime') WHERE id = ?",
            (scene_id,)
        )
        conn.commit()
    _with_lock_retry(_write)


def update_science_note(conn, note_id, body):
    def _write():
        conn.execute("UPDATE science_notes SET body = ? WHERE id = ?", (body, note_id))
        conn.commit()
    _with_lock_retry(_write)


def delete_science_note(conn, note_id):
    def _write():
        conn.execute("DELETE FROM science_notes WHERE id = ?", (note_id,))
        conn.commit()
    _with_lock_retry(_write)


def get_science_notes(conn, scene_id):
    """Manually authored science notes for a scene, oldest first. No housekeeping
    (review/decision) entries — only rows added directly through the Science Notes
    dialog."""
    return conn.execute("""
        SELECT 'note' AS type, n.id AS id, n.timestamp, u.username AS author_name,
               n.author_id AS author_id, n.body AS content
        FROM science_notes n
        JOIN users u ON n.author_id = u.id
        WHERE n.scene_id = ?
        ORDER BY n.timestamp ASC
    """, (scene_id,)).fetchall()


def get_analyst_in_progress(conn, user_id):
    """Scenes this analyst has contributed to that are still in the pipeline.
    Owned scenes at status 2/3/5 (submitted and being processed), plus scenes
    they peer-reviewed at status 4/5 (decision made, scene still moving)."""
    return conn.execute("""
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
    """, (user_id, user_id, user_id)).fetchall()


def get_analyst_completed(conn, user_id):
    """Scenes the analyst was involved in that have reached APPROVED (status 7)."""
    return conn.execute("""
        SELECT s.*,
               o.username AS owner_username,
               CASE WHEN s.owner_id = ? THEN 'Owner' ELSE 'Peer Reviewer' END AS my_role
        FROM scenes s
        LEFT JOIN users o ON s.owner_id = o.id
        WHERE s.status = 7
          AND (s.owner_id = ? OR s.peer_reviewer_id = ?)
        ORDER BY s.updated_at DESC
    """, (user_id, user_id, user_id)).fetchall()


def get_supervisor_in_progress(conn, user_id):
    """Scenes this supervisor has kicked back that are still being revised (status 4)."""
    return conn.execute("""
        SELECT s.*, o.username AS owner_username
        FROM scenes s
        LEFT JOIN users o ON s.owner_id = o.id
        WHERE s.supervisor_id = ? AND s.status = 4
        ORDER BY s.updated_at DESC
    """, (user_id,)).fetchall()


# ── Connection and initialization ─────────────────────────────────────────────

# Monday of the current calendar week (Mon-Sun), used for "_week" stat counts.
_WEEK_START_SQL = "DATE('now','localtime','weekday 0','-6 days')"


def _kickback_scene_count(conn, user_id, date_filter=None):
    """COUNT(DISTINCT scene_id) of kickbacks against scenes owned by user_id --
    a scene kicked back more than once in the period still counts once."""
    sql = ("SELECT COUNT(DISTINCT r.scene_id) FROM reviews r JOIN scenes s ON r.scene_id=s.id "
           "WHERE s.owner_id=? AND r.decision='needs_revision'")
    if date_filter:
        sql += f" AND {date_filter}"
    return conn.execute(sql, (user_id,)).fetchone()[0]


def _kickback_event_count(conn, user_id, date_filter=None):
    """COUNT(*) of kickbacks against scenes owned by user_id -- non-distinct,
    so a scene kicked back twice in the period counts twice."""
    sql = ("SELECT COUNT(*) FROM reviews r JOIN scenes s ON r.scene_id=s.id "
           "WHERE s.owner_id=? AND r.decision='needs_revision'")
    if date_filter:
        sql += f" AND {date_filter}"
    return conn.execute(sql, (user_id,)).fetchone()[0]


def get_user_stats(conn, user_id):
    """Stat counts for one user derived from the reviews + scenes tables."""
    def count(sql):
        return conn.execute(sql, (user_id,)).fetchone()[0]

    week_filter = f"DATE(r.timestamp)>={_WEEK_START_SQL}"
    today_filter = "DATE(r.timestamp)=DATE('now','localtime')"

    kicked_back_total = _kickback_scene_count(conn, user_id)
    kicked_back_week = _kickback_scene_count(conn, user_id, week_filter)
    kicked_back_today = _kickback_scene_count(conn, user_id, today_filter)

    def avg_kickbacks_per_scene(date_filter, scene_count):
        events = _kickback_event_count(conn, user_id, date_filter)
        return round(events / scene_count, 2) if scene_count else 0.0

    return {
        'submitted_total': count(
            "SELECT COUNT(DISTINCT scene_id) FROM reviews "
            "WHERE reviewer_id=? AND decision='submitted'"),
        'submitted_week': count(
            "SELECT COUNT(DISTINCT scene_id) FROM reviews "
            "WHERE reviewer_id=? AND decision='submitted' "
            f"AND DATE(timestamp)>={_WEEK_START_SQL}"),
        'submitted_today': count(
            "SELECT COUNT(DISTINCT scene_id) FROM reviews "
            "WHERE reviewer_id=? AND decision='submitted' "
            "AND DATE(timestamp)=DATE('now','localtime')"),
        'peer_reviewed_total': count(
            "SELECT COUNT(*) FROM reviews "
            "WHERE reviewer_id=? AND stage='peer_review'"),
        'peer_reviewed_week': count(
            "SELECT COUNT(*) FROM reviews "
            "WHERE reviewer_id=? AND stage='peer_review' "
            f"AND DATE(timestamp)>={_WEEK_START_SQL}"),
        'peer_reviewed_today': count(
            "SELECT COUNT(*) FROM reviews "
            "WHERE reviewer_id=? AND stage='peer_review' "
            "AND DATE(timestamp)=DATE('now','localtime')"),
        'approved_total': count(
            "SELECT COUNT(*) FROM reviews r JOIN scenes s ON r.scene_id=s.id "
            "WHERE s.owner_id=? AND r.stage='supervisor_review' AND r.decision='approved'"),
        'approved_week': count(
            "SELECT COUNT(*) FROM reviews r JOIN scenes s ON r.scene_id=s.id "
            "WHERE s.owner_id=? AND r.stage='supervisor_review' AND r.decision='approved' "
            f"AND DATE(r.timestamp)>={_WEEK_START_SQL}"),
        'approved_today': count(
            "SELECT COUNT(*) FROM reviews r JOIN scenes s ON r.scene_id=s.id "
            "WHERE s.owner_id=? AND r.stage='supervisor_review' AND r.decision='approved' "
            "AND DATE(r.timestamp)=DATE('now','localtime')"),
        'kicked_back_total': kicked_back_total,
        'kicked_back_week': kicked_back_week,
        'kicked_back_today': kicked_back_today,
        'avg_kickbacks_per_scene_total': avg_kickbacks_per_scene(None, kicked_back_total),
        'avg_kickbacks_per_scene_week': avg_kickbacks_per_scene(week_filter, kicked_back_week),
        'avg_kickbacks_per_scene_today': avg_kickbacks_per_scene(today_filter, kicked_back_today),
    }


def get_supervisor_stats(conn, user_id):
    """Stat counts for one supervisor derived from the reviews + scenes tables."""
    def count(sql):
        return conn.execute(sql, (user_id,)).fetchone()[0]
    return {
        'approved_total': count(
            "SELECT COUNT(*) FROM reviews r JOIN scenes s ON r.scene_id=s.id "
            "WHERE s.supervisor_id=? AND r.stage='supervisor_review' AND r.decision='approved'"),
        'approved_week': count(
            "SELECT COUNT(*) FROM reviews r JOIN scenes s ON r.scene_id=s.id "
            "WHERE s.supervisor_id=? AND r.stage='supervisor_review' AND r.decision='approved' "
            f"AND DATE(r.timestamp)>={_WEEK_START_SQL}"),
        'approved_today': count(
            "SELECT COUNT(*) FROM reviews r JOIN scenes s ON r.scene_id=s.id "
            "WHERE s.supervisor_id=? AND r.stage='supervisor_review' AND r.decision='approved' "
            "AND DATE(r.timestamp)=DATE('now','localtime')"),
        'kicked_back_total': count(
            "SELECT COUNT(DISTINCT r.scene_id) FROM reviews r JOIN scenes s ON r.scene_id=s.id "
            "WHERE s.supervisor_id=? AND r.decision='needs_revision'"),
        'kicked_back_week': count(
            "SELECT COUNT(DISTINCT r.scene_id) FROM reviews r JOIN scenes s ON r.scene_id=s.id "
            "WHERE s.supervisor_id=? AND r.decision='needs_revision' "
            f"AND DATE(r.timestamp)>={_WEEK_START_SQL}"),
        'kicked_back_today': count(
            "SELECT COUNT(DISTINCT r.scene_id) FROM reviews r JOIN scenes s ON r.scene_id=s.id "
            "WHERE s.supervisor_id=? AND r.decision='needs_revision' "
            "AND DATE(r.timestamp)=DATE('now','localtime')"),
    }
    

def get_all_user_stats(conn):
    """Return [(user_row, stats_dict)] for all active users, sorted by username."""
    users = conn.execute(
        "SELECT id, username, role FROM users WHERE active=1 ORDER BY username"
    ).fetchall()
    return [(u, get_user_stats(conn, u['id'])) for u in users]


def get_all_supervisor_stats(conn):
    """Return [(user_row, stats_dict)] for all active supervisors, sorted by username."""
    users = conn.execute(
        "SELECT id, username, role FROM users WHERE active=1 AND role='supervisor' ORDER BY username"
    ).fetchall()
    return [(u, get_supervisor_stats(conn, u['id'])) for u in users]


def _run_migrations(conn):
    from app.migrations import MIGRATIONS
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id         TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}
    for m in MIGRATIONS:
        if m.id not in applied:
            m.up(conn)
            conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (m.id,))
            conn.commit()


def get_db_connection():
    # timeout=1.0 sets SQLite's own busy handler, so every statement already
    # retries internally (at sub-second intervals) for up to 1s before raising
    # "database is locked" -- kept short so a single attempt can't silently
    # eat many seconds. _with_lock_retry then adds up to 25 further attempts
    # at a ~0.1s cadence around every write in this module.
    conn = sqlite3.connect(DB_PATH, timeout=1.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

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
            updated_at              TEXT DEFAULT (datetime('now', 'localtime')),
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
            timestamp       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
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
            timestamp   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            body        TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS science_notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id    INTEGER NOT NULL REFERENCES scenes (id),
            author_id   INTEGER NOT NULL REFERENCES users (id),
            timestamp   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            body        TEXT NOT NULL
        )
    """)
    conn.commit()
    _run_migrations(conn)
    conn.close()
