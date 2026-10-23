import getpass
import sys

from werkzeug.security import generate_password_hash

from app import app, get_db

email = input("Account email: ").strip().lower()
password = getpass.getpass("New password (at least 8 characters): ")

if len(password) < 8:
    sys.exit("Password is too short. Nothing was changed.")
if password != getpass.getpass("Confirm new password: "):
    sys.exit("Passwords do not match. Nothing was changed.")

with app.app_context():
    db = get_db()
    result = db.execute(
        "UPDATE users SET password_hash = ? WHERE email = ?",
        (generate_password_hash(password), email),
    )
    db.commit()
    print("Password updated." if result.rowcount else "No account found with that email.")