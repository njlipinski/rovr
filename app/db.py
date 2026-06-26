# app/db.py
"""all SQLite operations, including creating tables and inserting data"""
import sqlite3
from config import DB_PATH


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
    conn.execute(
        "INSERT INTO users (username, active, password_hash, role) VALUES (?, 1, ?, ?)",
        (username, password_hash, role)
    )
    conn.commit()

def activate_user(conn, user_id):
    conn.execute("UPDATE users SET active = 1 WHERE id = ?", (user_id,))
    conn.commit()

def deactivate_user(conn, user_id):
    """Deactivate user and return their open scenes to shared pools.
    Status 1 and 4 go to unclaimed pool (0) with ownership cleared so a new analyst
    can claim fresh. Status 3 goes back to peer review pool (2) with claim cleared
    so a different analyst can review the existing ROI work."""
    conn.execute("""
        UPDATE scenes
        SET status = 0, owner_id = NULL, assigned_to = NULL, claimed_by = NULL,
            updated_at = datetime('now')
        WHERE owner_id = ? AND status IN (1, 4)
    """, (user_id,))
    conn.execute("""
        UPDATE scenes
        SET status = 2, claimed_by = NULL, updated_at = datetime('now')
        WHERE owner_id = ? AND status = 3
    """, (user_id,))
    conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
    conn.commit()

def update_user_role(conn, user_id, new_role):
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    conn.commit()

def update_user_password(conn, user_id, new_password_hash):
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_password_hash, user_id))
    conn.commit()

def update_username(conn, user_id, new_username):
    conn.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user_id))
    conn.commit()


# ── Scene functions ───────────────────────────────────────────────────────────
#
# Status integers:
#   0 unclaimed        — Scene Pool (shared, all analysts)
#   1 claimed          — Analyst 1's to-do
#   2 pending review   — Peer Review Pool (shared, all analysts except owner)
#   3 in review        — Analyst 2's to-do
#   4 needs revision   — Analyst 1's to-do
#   5 pending super    — Supervisor Pool (shared, all supervisors)
#   6 approved         — done

def create_scene(conn, name, scene_key, roi_filename=None, owner_id=None):
    """create a scene; owner_id and roi_filename are None for pool-imported scenes
    (owner set at claim time, roi_filename set when analyst saves the .sel file)"""
    conn.execute(
        "INSERT INTO scenes (name, scene_key, roi_filename, owner_id, assigned_to, status) VALUES (?, ?, ?, ?, ?, 0)",
        (name, scene_key, roi_filename, owner_id, owner_id)
    )
    conn.commit()

def get_scene_by_id(conn, scene_id):
    return conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()

def get_open_scenes_for_user(conn, user_id):
    return conn.execute(
        "SELECT * FROM scenes WHERE owner_id = ? AND status != 6",
        (user_id,)
    ).fetchall()

def claim_from_pool(conn, scene_id, analyst_id):
    """analyst 1 atomically claims an unclaimed scene (0 → 1), setting owner if not yet assigned"""
    cur = conn.execute(
        """UPDATE scenes
           SET status = 1, claimed_by = ?,
               owner_id   = COALESCE(owner_id,   ?),
               assigned_to = COALESCE(assigned_to, ?)
           WHERE id = ? AND status = 0""",
        (analyst_id, analyst_id, analyst_id, scene_id)
    )
    conn.commit()
    return cur.rowcount == 1

def claim_for_review(conn, scene_id, analyst_id):
    """analyst 2 atomically claims a scene for peer review (2 → 3)"""
    cur = conn.execute(
        "UPDATE scenes SET status = 3, claimed_by = ? WHERE id = ? AND status = 2 AND owner_id != ?",
        (analyst_id, scene_id, analyst_id)
    )
    conn.commit()
    return cur.rowcount == 1

def release_scene(conn, scene_id):
    """release a claimed scene back to the appropriate pool (1 → 0, or 3 → 2)"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] == 1:
        target = 0
    elif scene['status'] == 3:
        target = 2
    else:
        return
    conn.execute(
        "UPDATE scenes SET status = ?, claimed_by = NULL, updated_at = datetime('now') WHERE id = ?",
        (target, scene_id)
    )
    conn.commit()

def update_scene_status(conn, scene_id, new_status):
    """update scene status and clear any claim lock"""
    conn.execute(
        "UPDATE scenes SET status = ?, claimed_by = NULL, updated_at = datetime('now') WHERE id = ?",
        (new_status, scene_id)
    )
    conn.commit()

def reset_scene(conn, scene_id):
    """supervisor admin reset: wipe all ownership and return scene to unclaimed pool"""
    conn.execute("""
        UPDATE scenes
        SET status = 0,
            owner_id = NULL,
            assigned_to = NULL,
            peer_reviewer_id = NULL,
            supervisor_id = NULL,
            claimed_by = NULL,
            updated_at = datetime('now')
        WHERE id = ?
    """, (scene_id,))
    conn.commit()

def update_scene_assignment(conn, scene_id, new_analyst_id):
    conn.execute("UPDATE scenes SET assigned_to = ? WHERE id = ?", (new_analyst_id, scene_id))
    conn.commit()

def set_peer_reviewer(conn, scene_id, reviewer_id):
    conn.execute("UPDATE scenes SET peer_reviewer_id = ? WHERE id = ?", (reviewer_id, scene_id))
    conn.commit()

def set_supervisor(conn, scene_id, supervisor_id):
    conn.execute("UPDATE scenes SET supervisor_id = ? WHERE id = ?", (supervisor_id, scene_id))
    conn.commit()


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
           WHERE (scenes.status IN (1, 4) AND scenes.assigned_to = ?)
              OR (scenes.status = 3 AND scenes.claimed_by = ?)""",
        (user_id, user_id)
    ).fetchall()

def get_peer_review_claimed(conn, user_id):
    """scenes analyst 2 has currently claimed for peer review (status 3)"""
    return conn.execute(
        "SELECT * FROM scenes WHERE status = 3 AND claimed_by = ?",
        (user_id,)
    ).fetchall()

def get_ready_queue(conn):
    """scenes available for peer review (status 2, shared pool)"""
    return conn.execute("SELECT * FROM scenes WHERE status = 2").fetchall()

def get_supervisor_queue(conn):
    """scenes awaiting supervisor approval (status 5, shared pool)"""
    return conn.execute("""
        SELECT scenes.*, users.username AS owner_username
        FROM scenes
        LEFT JOIN users ON scenes.owner_id = users.id
        WHERE scenes.status = 5
    """).fetchall()


# ── Review functions ──────────────────────────────────────────────────────────

def log_review(conn, scene_id, reviewer_id, stage, decision, comments):
    conn.execute(
        "INSERT INTO reviews (scene_id, reviewer_id, stage, decision, comments) VALUES (?, ?, ?, ?, ?)",
        (scene_id, reviewer_id, stage, decision, comments)
    )
    conn.commit()

def get_all_scenes(conn):
    """master list of every scene with all user fields resolved to usernames"""
    return conn.execute("""
        SELECT
            s.id,
            s.name,
            s.scene_key,
            s.roi_filename,
            s.status,
            s.submitted_at,
            s.updated_at,
            o.username  AS owner_username,
            a.username  AS assigned_to_username,
            pr.username AS peer_reviewer_username,
            sv.username AS supervisor_username,
            cb.username AS claimed_by_username
        FROM scenes s
        LEFT JOIN users o  ON s.owner_id          = o.id
        LEFT JOIN users a  ON s.assigned_to        = a.id
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
    conn.execute(
        "INSERT INTO notes (scene_id, author_id, body) VALUES (?, ?, ?)",
        (scene_id, author_id, body)
    )
    conn.commit()


def get_scene_thread(conn, scene_id):
    """Interleaved notes + review-comments for a scene, oldest first.
    Each row: type ('note'/'review'), timestamp, author_name, content, decision."""
    return conn.execute("""
        SELECT 'note' AS type, n.timestamp, u.username AS author_name,
               n.body AS content, NULL AS decision
        FROM notes n
        JOIN users u ON n.author_id = u.id
        WHERE n.scene_id = ?
        UNION ALL
        SELECT 'review' AS type, r.timestamp, u.username AS author_name,
               r.comments AS content, r.decision
        FROM reviews r
        JOIN users u ON r.reviewer_id = u.id
        WHERE r.scene_id = ? AND r.comments IS NOT NULL
        ORDER BY timestamp ASC
    """, (scene_id, scene_id)).fetchall()


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
        WHERE (s.owner_id = ? AND s.status IN (2, 3, 5))
           OR (s.peer_reviewer_id = ? AND s.status IN (4, 5))
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

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
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
            assigned_to             INTEGER REFERENCES users (id),
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
    conn.commit()
    conn.close()
