import sqlite3

conn = sqlite3.connect("db.sqlite")

conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        role     TEXT NOT NULL
    )
""")
conn.commit()

u_name = input("Enter username: ")
role = input("Enter role: ")

if u_name == "" or role == "":
    print("Username and role cannot be empty.")
elif u_name in [username[0] for username in conn.execute("SELECT username FROM users").fetchall()]:
    print("Username already exists.")
else:
    conn.execute("INSERT INTO users (username, role) VALUES (?, ?)", (u_name, role))
    conn.commit()

rows = conn.execute("SELECT * FROM users").fetchall()
for row in rows:
    print(row)
    