# app/db.py
import sqlite3
from config import DB_PATH

"""all SQLite operations, including creating tables and inserting data"""

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
            updated_at      TEXT DEFAULT (datetime('now')),
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
            comments        TEXT,
        )
    """)
    conn.commit()
    conn.close()
