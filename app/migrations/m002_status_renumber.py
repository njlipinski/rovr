id = "002_status_renumber"
description = "Renumber status 6 (approved) to 7 to make room for status 6 (in supervisor review)"
dependencies = ["001_add_flags"]


def up(conn):
    conn.execute("UPDATE scenes SET status = 7 WHERE status = 6")
