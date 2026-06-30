"""ROVR database migration CLI.

Migrations run automatically on startup via initialize_db(). Use this tool
for status checks, dry-run previews, or manually applying pending migrations
before deploying a new exe.

    python setup/migrate.py          # apply any pending migrations
    python setup/migrate.py --dry-run
    python setup/migrate.py --list
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.db import get_db_connection
from app.migrations import MIGRATIONS


def _ensure_migrations_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id         TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="ROVR database migration CLI")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview pending migrations without applying them")
    parser.add_argument("--list", action="store_true",
                        help="Show applied/pending status for all migrations")
    args = parser.parse_args()

    conn = get_db_connection()
    _ensure_migrations_table(conn)
    applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}

    if args.list:
        print("Migration status:")
        for m in MIGRATIONS:
            tag = "applied" if m.id in applied else "pending"
            print(f"  [{tag:7}]  {m.id}: {m.description}")
        conn.close()
        return

    pending = [m for m in MIGRATIONS if m.id not in applied]
    if not pending:
        print("All migrations already applied — nothing to do.")
        conn.close()
        return

    label = "DRY RUN" if args.dry_run else "Applying"
    print(f"{label} — {len(pending)} pending migration(s):")

    for m in pending:
        print(f"\n  [{m.id}]  {m.description}")
        if args.dry_run:
            print("    DRY RUN — skipped")
            continue
        m.up(conn)
        conn.execute("INSERT OR IGNORE INTO schema_migrations (id) VALUES (?)", (m.id,))
        conn.commit()
        print("    Applied.")

    conn.close()
    if not args.dry_run:
        print("\nDone.")


if __name__ == "__main__":
    main()
