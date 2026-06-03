# app/db.py
"""all SQLite operations, including creating tables and inserting data"""
import sqlite3
from config import DB_PATH


"""
User related functions
"""

def get_user_by_username(conn, username):
    """returns a user by their username"""
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

def get_all_active_analysts(conn):
    return conn.execute("SELECT * FROM users WHERE active = 1 AND role = 'analyst'").fetchall()

def create_user(conn, username, password_hash, role):
    """creates a new user in the database"""
    conn.execute("INSERT INTO users (username, active, password_hash, role) VALUES (?, ?, ?, ?)", (username, 1, password_hash, role))
    conn.commit()
    
def deactivate_user(conn, user_id):
    """deactivates a user in the database"""
    conn.execute("""
        UPDATE scenes SET status = 'needs_attention', updated_at = datetime('now')
        WHERE owner_id = ? AND status NOT IN ('approved', 'needs_attention')
    """, (user_id,))
    conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
    conn.commit()
    
def update_user_role(conn, user_id, new_role):
    """updates a user's role in the database"""
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    conn.commit()
    
def update_user_password(conn, user_id, new_password_hash):
    """updates a user's password in the database"""
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_password_hash, user_id))
    conn.commit()
    
""" 
Scene related functions

States: approved, needs_revision, pending_supervisor, needs_attention, draft
"""

def create_scene(conn, name, roi_filename, owner_id):
    """creates a new scene in the database"""
    conn.execute(
        "INSERT INTO scenes (name, roi_filename, owner_id, assigned_to, status) VALUES (?, ?, ?, ?, ?)",
        (name, roi_filename, owner_id, owner_id, 'draft')
    )
    conn.commit()
    
def get_scene_by_id(conn, scene_id):
    """returns a scene by its ID"""
    return conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()

def get_open_scenes_for_user(conn, user_id):
    """returns all open scenes for a specific user"""
    return conn.execute("SELECT * FROM scenes WHERE owner_id = ? AND status NOT IN ('approved', 'needs_attention')", (user_id,)).fetchall()

def update_scene_status(conn, scene_id, new_status):
    """updates a scene's status in the database"""
    conn.execute("UPDATE scenes SET status = ?, updated_at = datetime('now') WHERE id = ?", (new_status, scene_id))
    conn.commit() 

def update_scene_assignment(conn, scene_id, new_analyst_id):
    """reassigns a scene to a different analyst"""
    conn.execute("UPDATE scenes SET assigned_to = ? WHERE id = ?", (new_analyst_id, scene_id))
    conn.commit()
    
def get_analyst_queue(conn, user_id):
    """returns all scenes that have been kicked back and are in the needs revision status"""
    return conn.execute("SELECT * FROM scenes WHERE status = 'needs_revision' AND assigned_to = ?", (user_id,)).fetchall()

def get_ready_queue(conn):
    """returns all scenes that are in the ready for review status"""
    return conn.execute("SELECT * FROM scenes WHERE status = 'pending_review'").fetchall()

def get_supervisor_queue(conn):
    """returns all scenes that are in the 'pending_supervisor' status"""
    return conn.execute("SELECT * FROM scenes WHERE status = 'pending_supervisor'").fetchall()

def get_needs_attention_queue(conn):
    """returns all scenes that are in the 'needs_attention' status"""
    return conn.execute("SELECT * FROM scenes WHERE status = 'needs_attention'").fetchall()

def log_review(conn, scene_id, reviewer_id, stage, decision, comments):
    """logs a review decision in the database"""
    conn.execute(
        "INSERT INTO reviews (scene_id, reviewer_id, stage, decision, comments) VALUES (?, ?, ?, ?, ?)",
        (scene_id, reviewer_id, stage, decision, comments)
    )
    conn.commit()
    
def get_scene_history(conn, scene_id):
    """returns the review history for a specific scene"""
    return conn.execute("""
        SELECT r.*, u.username AS reviewer_name
        FROM reviews r
        JOIN users u ON r.reviewer_id = u.id
        WHERE r.scene_id = ?
        ORDER BY r.timestamp DESC
    """, (scene_id,)).fetchall()

"""
DB connection and initialization functions
"""

def get_db_connection():
    """returns a connection to the SQLite database"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    WAL = "PRAGMA journal_mode=WAL;"
    conn.execute(WAL)
    foreign_keys = "PRAGMA foreign_keys=ON;"
    conn.execute(foreign_keys)
    return conn

def initialize_db():
    """initializes the database with necessary tables"""
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
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            roi_filename    TEXT NOT NULL,
            owner_id        INTEGER NOT NULL REFERENCES users (id),
            assigned_to     INTEGER NOT NULL REFERENCES users (id),
            status          TEXT NOT NULL,
            submitted_at    TEXT,
            updated_at      TEXT DEFAULT (datetime('now'))
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
    conn.commit()
    conn.close()
