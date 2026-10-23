"""Create an extra sign-in account, for example a read-only Viewer.

Run it from the project folder with the virtual environment active:

    python create_user.py

Roles come from ROLE_PERMISSIONS in app.py:
    admin  - full access (add, edit, deactivate, export, see contact details and notes)
    viewer - can see the member roster only; no contact details, notes, edits or exports

A user account is different from a choir member. Members are managed on the Members page;
this script is only for people who need to SIGN IN to the system.
"""

import getpass
import sqlite3
import sys

from werkzeug.security import generate_password_hash

from app import EMAIL_RE, MIN_PASSWORD_LENGTH, ROLE_PERMISSIONS, app, get_db, log_activity


def main():
    # Member logins are created automatically from the Members page, never here.
    roles = [role for role in sorted(ROLE_PERMISSIONS) if role != "member"]
    name = input("Full name: ").strip()
    email = input("Email address: ").strip().lower()
    role = input(f"Role ({' or '.join(roles)}) [viewer]: ").strip().lower() or "viewer"

    if not name:
        sys.exit("A name is required. Nothing was created.")
    if not EMAIL_RE.match(email):
        sys.exit("That does not look like an email address. Nothing was created.")
    if role not in roles:
        sys.exit(f"Unknown role. Choose one of: {', '.join(roles)}. Nothing was created.")

    password = getpass.getpass(f"Password (at least {MIN_PASSWORD_LENGTH} characters): ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit("The password is too short. Nothing was created.")
    if password != getpass.getpass("Confirm password: "):
        sys.exit("The passwords do not match. Nothing was created.")

    with app.app_context():
        db = get_db()
        try:
            cursor = db.execute(
                "INSERT INTO users (full_name, email, password_hash, role) VALUES (?, ?, ?, ?)",
                (name, email, generate_password_hash(password), role),
            )
            log_activity(db, "user.created", detail=f"{name} ({role})", target_type="user",
                         target_id=cursor.lastrowid)
            db.commit()
        except sqlite3.IntegrityError:
            sys.exit("An account with that email already exists. Nothing was created.")
    print(f"Created {role} account for {name}. They can now sign in with {email}.")


if __name__ == "__main__":
    main()