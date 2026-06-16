#!/usr/bin/env python3
"""CLI for managing ROVR user accounts.

Usage (run from repo root):
    python setup/manage_users.py create <username> [--role analyst|supervisor]
    python setup/manage_users.py list
    python setup/manage_users.py deactivate <username>
    python setup/manage_users.py activate <username>
    python setup/manage_users.py role <username> <analyst|supervisor>
    python setup/manage_users.py password <username>
"""

import sys
import argparse
import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import (
    get_db_connection, initialize_db,
    get_user_by_username, get_all_users,
    create_user, activate_user, deactivate_user,
    update_user_role, update_user_password,
)
from app.auth import hash_password

ROLES = ('analyst', 'supervisor')


def _get_user_or_exit(conn, username):
    user = get_user_by_username(conn, username)
    if not user:
        print(f"Error: user '{username}' not found.")
        sys.exit(1)
    return user


def _confirm(prompt):
    return input(f"{prompt} [y/N]: ").strip().lower() == 'y'


def _prompt_password():
    while True:
        pw = getpass.getpass("Password: ")
        if not pw:
            print("Password cannot be empty.")
            continue
        pw2 = getpass.getpass("Confirm password: ")
        if pw != pw2:
            print("Passwords do not match, try again.")
            continue
        return pw


def cmd_create(conn, args):
    if get_user_by_username(conn, args.username):
        print(f"Error: username '{args.username}' already exists.")
        sys.exit(1)
    password = _prompt_password()
    create_user(conn, args.username, hash_password(password), args.role)
    print(f"Created {args.role} '{args.username}'.")


def cmd_list(conn, _):
    users = get_all_users(conn)
    if not users:
        print("No users found.")
        return
    print(f"{'ID':<6} {'Username':<20} {'Role':<12} {'Active'}")
    print("-" * 46)
    for u in users:
        status = "yes" if u['active'] else "no"
        print(f"{u['id']:<6} {u['username']:<20} {u['role']:<12} {status}")


def cmd_deactivate(conn, args):
    user = _get_user_or_exit(conn, args.username)
    if not user['active']:
        print(f"'{args.username}' is already inactive.")
        sys.exit(1)
    if not _confirm(f"Deactivate '{args.username}'? This will flag their open scenes as 'needs attention' (status 7) for supervisor reassignment."):
        print("Aborted.")
        return
    deactivate_user(conn, user['id'])
    print(f"Deactivated '{args.username}'.")


def cmd_activate(conn, args):
    user = _get_user_or_exit(conn, args.username)
    if user['active']:
        print(f"'{args.username}' is already active.")
        sys.exit(1)
    activate_user(conn, user['id'])
    print(f"Activated '{args.username}'.")


def cmd_role(conn, args):
    user = _get_user_or_exit(conn, args.username)
    if user['role'] == args.role:
        print(f"'{args.username}' is already a {args.role}.")
        sys.exit(1)
    if not _confirm(f"Change '{args.username}' from {user['role']} to {args.role}?"):
        print("Aborted.")
        return
    update_user_role(conn, user['id'], args.role)
    print(f"Updated '{args.username}' to {args.role}.")


def cmd_password(conn, args):
    user = _get_user_or_exit(conn, args.username)
    password = _prompt_password()
    update_user_password(conn, user['id'], hash_password(password))
    print(f"Password updated for '{args.username}'.")


def main():
    parser = argparse.ArgumentParser(
        prog="manage_users",
        description="Manage ROVR user accounts.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("create", help="Create a new user account.")
    p.add_argument("username")
    p.add_argument("--role", choices=ROLES, default="analyst",
                   help="Role to assign (default: analyst).")

    sub.add_parser("list", help="List all user accounts.")

    p = sub.add_parser("deactivate", help="Deactivate a user account.")
    p.add_argument("username")

    p = sub.add_parser("activate", help="Reactivate a deactivated account.")
    p.add_argument("username")

    p = sub.add_parser("role", help="Change a user's role.")
    p.add_argument("username")
    p.add_argument("role", choices=ROLES)

    p = sub.add_parser("password", help="Reset a user's password.")
    p.add_argument("username")

    args = parser.parse_args()

    initialize_db()
    conn = get_db_connection()
    try:
        {
            "create":     cmd_create,
            "list":       cmd_list,
            "deactivate": cmd_deactivate,
            "activate":   cmd_activate,
            "role":       cmd_role,
            "password":   cmd_password,
        }[args.command](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
