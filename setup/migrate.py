"""ROVR database migration tool.

Run all pending migrations:
    python setup/migrate.py

Preview without applying:
    python setup/migrate.py --dry-run

Show migration status:
    python setup/migrate.py --list
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.db import get_db_connection


# ── Migration functions ───────────────────────────────────────────────────────
# Each function receives (conn, dry_run) and prints its own progress.
# Must NOT commit — _run() commits after recording the migration ID.

def _001_status_renumber(conn, dry_run):
    sql = "UPDATE scenes SET status = 7 WHERE status = 6"
    if dry_run:
        print(f"    DRY RUN: {sql}")
    else:
        cur = conn.execute(sql)
        print(f"    {cur.rowcount} row(s) updated (old status 6 → new status 7)")


def _002_drop_assigned_to(conn, dry_run):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scenes)")}
    if 'assigned_to' not in cols:
        print("    Column 'assigned_to' already absent — nothing to do")
        return
    sql = "ALTER TABLE scenes DROP COLUMN assigned_to"
    if dry_run:
        print(f"    DRY RUN: {sql}")
    else:
        conn.execute(sql)
        print("    Dropped column 'assigned_to'")


# ── Migration registry ────────────────────────────────────────────────────────
# (id, description, function)
# Migrations run in order; each is applied at most once (tracked in schema_migrations).

MIGRATIONS = [
    (
        "001_status_renumber",
        "Renumber status 6 (approved) → 7 to make room for status 6 (in supervisor review)",
        _001_status_renumber,
    ),
    (
        "002_drop_assigned_to",
        "Remove redundant assigned_to column (superseded by owner_id everywhere)",
        _002_drop_assigned_to,
    ),
]

# ─────────────────────────────────────────────────────────────────────────────


def _ensure_migrations_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id         TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()


def _applied(conn):
    return {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}


def _run(conn, mid, description, func, dry_run):
    print(f"\n  [{mid}]  {description}")
    func(conn, dry_run)
    if not dry_run:
        conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (mid,))
        conn.commit()
        print("    Applied.")


def main():
    parser = argparse.ArgumentParser(description="ROVR database migration tool")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview pending migrations without applying them")
    parser.add_argument("--list", action="store_true",
                        help="Show applied/pending status for all migrations")
    args = parser.parse_args()

    conn = get_db_connection()
    _ensure_migrations_table(conn)
    applied = _applied(conn)

    if args.list:
        print("Migration status:")
        for mid, desc, _ in MIGRATIONS:
            tag = "applied" if mid in applied else "pending"
            print(f"  [{tag:7}]  {mid}: {desc}")
        conn.close()
        return

    pending = [(mid, desc, func) for mid, desc, func in MIGRATIONS if mid not in applied]

    if not pending:
        print("All migrations already applied — nothing to do.")
        conn.close()
        return

    label = "DRY RUN" if args.dry_run else "Applying"
    print(f"{label} — {len(pending)} pending migration(s):")

    for mid, desc, func in pending:
        _run(conn, mid, desc, func, dry_run=args.dry_run)

    conn.close()
    if not args.dry_run:
        print("\nDone.")
        print("\n⚠️  Remember to rebuild and redeploy rovr.exe (run build.ps1) before")
        print("   users relaunch — the new DB schema requires the matching code.")


if __name__ == "__main__":
    main()
