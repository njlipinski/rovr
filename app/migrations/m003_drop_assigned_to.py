id = "003_drop_assigned_to"
description = "Remove redundant assigned_to column (superseded by owner_id everywhere)"
dependencies = ["002_status_renumber"]


def up(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scenes)")}
    if 'assigned_to' in cols:
        conn.execute("ALTER TABLE scenes DROP COLUMN assigned_to")
