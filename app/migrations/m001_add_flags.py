id = "001_add_flags"
description = "Add flags column to scenes table"
dependencies = []


def up(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scenes)")}
    if 'flags' not in cols:
        conn.execute("ALTER TABLE scenes ADD COLUMN flags TEXT NOT NULL DEFAULT '{}'")
