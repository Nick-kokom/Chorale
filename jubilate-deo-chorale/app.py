"""JUBILATE DEO CHORALE - Catholic parish liturgical choir management system.

Foundation: app setup, SQLite connection, first-run admin setup, login/logout,
and a protected dashboard that reads real data from the database.
Members module: add, view, edit and deactivate choir members, with role-based access.
Feature modules (schedule, announcements, music, attendance) come later.
"""

import calendar
import csv
import hashlib
import io
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import uuid
import webbrowser
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urlparse

from dotenv import load_dotenv
from music_storage import StorageError, StorageNotFound, make_storage
from flask import (
    Flask, Response, abort, flash, g, jsonify, make_response, redirect, render_template, request,
    send_from_directory, session, url_for,
)
from werkzeug.routing import BuildError
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

APP_NAME = "Jubilate Deo Chorale"
TAGLINE = "Sing with faith. Serve through music."
MIN_PASSWORD_LENGTH = 8

# Application roles. A role controls what a signed-in USER may do in this system.
# It is NOT the same thing as a member's choir position (Choir Director, Section
# Leader...). A choir position never grants any access here.
# Unknown roles get no permissions at all (deny by default).
ROLE_PERMISSIONS = {
    "admin": {"dashboard.view", "members.view", "members.private", "members.notes",
              "members.manage", "members.credentials", "members.export",
              "members.delete", "members.archive", "members.purge", "announcements.manage",
              "members.reset", "members.choices", "members.access", "schedule.view", "schedule.manage", "schedule.clear",
              "music.view", "music.manage"},
    "viewer": {"dashboard.view", "members.view", "schedule.view"},   # roster only: no contact details, notes, edits or exports
    "member": {"member.home"},                      # a choir member's own login. Nothing administrative.
}

# =====================================================================================
# WHERE TO CHANGE THE CHOICES  (voice sections, choir positions, membership statuses)
# =====================================================================================
# TIP: in VS Code press Ctrl+Shift+F and search for one of these tags to jump to EVERY place
# that deals with it (in app.py, the templates and style.css):
#       [VOICE SECTION]        [CHOIR POSITION]        [MEMBERSHIP STATUS]
#
#  VOICE SECTIONS and CHOIR POSITIONS live in the DATABASE and are managed on the website:
#  Members page -> "Voice sections & positions" (see [MANAGE CHOICES] below). The two lists further
#  down (DEFAULT_VOICE_SECTIONS, DEFAULT_CHOIR_POSITIONS) only fill an EMPTY database the very first time.
#  Do NOT run  python manage_choices.py sync  any more: it would retire or remove choices that you
#  added on the website, because they are not in those two lists.
#
#  MEMBERSHIP STATUSES are part of how the system works, so they are fixed in code. Changing
#  the wording is safe; adding or removing one is not (see the notes at MEMBER_STATUSES).
# =====================================================================================

# ---------- [MEMBERSHIP STATUS] ----------
# THIS IS THE LIST OF MEMBERSHIP STATUSES.  Each line is   "code stored in the database": "label shown on screen".
#   SAFE:   change the LABEL on the right (for example "Temporarily Inactive" -> "On Leave").
#   NOT SAFE on its own: deleting a line or adding a new one. Each status is also used by
#           - the CHECK rule on the members table (search [MEMBERSHIP STATUS] in init_db below),
#           - the buttons Deactivate / Restore / Mark temporarily inactive,
#           - the summary-card counts and the badge colours (static/css/style.css, [BADGES]).
#   Please ask before adding or removing one, so all of those are changed together.
MEMBER_STATUSES = {
    "active": "Active",
    "temporarily_inactive": "Temporarily Inactive",
    "inactive": "Inactive",
}

PAGE_SIZE = 15                       # members per page in the table
NAME_MAX = 60                        # longest first / middle / last name
NOTES_MAX = 2000                     # longest notes text
PHOTO_MAX_BYTES = 2 * 1024 * 1024    # profile photos: 2 MB
FILE_MAX_BYTES = 15 * 1024 * 1024    # shared choir files (PowerPoints, documents, designs): 15 MB
MUSIC_MAX_MB = max(1, int(os.environ.get("MUSIC_MAX_MB", "30") or 30))   # Music Library files. On Vercel keep this at 4 (its request limit is 4.5 MB)
MUSIC_MAX_BYTES = MUSIC_MAX_MB * 1024 * 1024
PHOTO_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
PHOTO_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|webp)$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")
MEMBER_CODE_RE = re.compile(r"^JDC-[0-9]{1,9}$")

# [RECORD TYPE] The Add person form can record two kinds of people:
#   "member"  a choir member (members table: Member ID, voice section, membership status, login...)
#   "leader"  a NON-MEMBER organizational leader such as the Parish Priest (org_leaders table).
#             No Member ID, no voice section, no membership status, no login, and never counted as a member.
RECORD_TYPES = ("member", "leader")
# The organizational tree shows members and leaders side by side and links each card with the person's id.
# Leader ids are shifted by this number so they can never be mistaken for a member id (member_photo and
# member_detail hand such ids over to the leader pages).
LEADER_ID_OFFSET = 1_000_000_000

# Shown when a deleted (archived or permanently deleted) member tries to sign in.
NO_ACCESS_MESSAGE = "You don't have an access. If you think this is wrong, contact the admin."

# MEMBER SIGN-IN SWITCH. Every member has a login (Member ID + generated password) and the member area
# now exists, so this is True. Set it to False to switch member sign-in off for everyone: nobody can sign
# in with a Member ID, and members who are signed in are signed out on their next click.
MEMBER_LOGIN_ENABLED = True

# FIRST SIGN-IN: the password the administrator hands out is temporary. While this is True a member must
# choose their own password before they can use anything else. (Off for now, see below.)
FORCE_PASSWORD_CHANGE = False
MIN_NEW_PASSWORD_LENGTH = 8

# MEMBERS CHANGING THEIR OWN PASSWORD. For now members simply use the password the administrator gave
# them: they are not asked to change it, and the "Change password" menu item is hidden. Set this to True
# later (and FORCE_PASSWORD_CHANGE to True if you want to make the first change compulsory).
MEMBER_PASSWORD_CHANGE_ENABLED = False

# TOO MANY WRONG PASSWORDS: after MAX_FAILED_LOGINS wrong tries for the same Member ID or email within
# LOGIN_WINDOW_MINUTES, sign-in is refused until the time passes. MAX_FAILED_PER_IP does the same per computer.
MAX_FAILED_LOGINS = 5
MAX_FAILED_PER_IP = 30
LOGIN_WINDOW_MINUTES = 15
TOO_MANY_ATTEMPTS_MESSAGE = f"Too many sign-in attempts. Please wait {LOGIN_WINDOW_MINUTES} minutes and try again."
GENERIC_LOGIN_ERROR = "The email, Member ID or password is incorrect."
MEMBER_LOGIN_ERROR = "The Member ID or password is incorrect."

# ---------- [POSITION CAPABILITIES] ----------
# What a CHOIR POSITION may do INSIDE THE MEMBER AREA. A position never opens an administrator page.
# The key is the position name exactly as it appears in your choir positions list (capitals do not matter).
#   announcements.post  post announcements for everyone or for one voice section
#   tasks.manage        assign tasks and tick them as done
#   attendance.manage   take attendance
#   files.general       upload and edit General Choir Files (everyone else can only view and download them)
#   finance.post        post the treasurer's report (shown on every member's dashboard)
#   schedule.manage     create, edit and cancel schedules (every member can VIEW the schedule)
# To give a position more, add its name here. Positions not listed here are ordinary members.
POSITION_CAPABILITIES = {
    "President": {"announcements.post", "schedule.manage", "files.general"},
    "Choir Director": {"announcements.post", "schedule.manage", "files.general"},
    "Internal Secretary": {"announcements.post", "tasks.manage", "attendance.manage", "schedule.manage", "files.general"},
    "Treasurer": {"finance.post"},
}
# [OFFICER DETAILS] The profile panel on the Members page shows an officer's position title, an official description
# and their duties. The app has NO stored description for a position, so write them here (nothing is invented for you).
# The key is the position name exactly as in your choir positions list (capitals do not matter). Both parts are optional:
#     "Undersecretary": {
#         "description": "One or two sentences describing the position.",
#         "duties": ["First duty", "Second duty"],
#     },
# A position that is not written here still shows its title and what it can do in this app (from POSITION_CAPABILITIES).
POSITION_DESCRIPTIONS = {}
# Wording for the rights in POSITION_CAPABILITIES that have no entry in ACTION_PERMISSIONS (see the Action Center below).
EXTRA_CAPABILITY_LABELS = {
    "files.general": ("Manage General Choir Files", "Upload and edit the shared files and the Music Library"),
}
ATTENDANCE_STATUSES = {"present": "Present", "late": "Late", "excused": "Excused", "absent": "Absent"}
ANNOUNCEMENT_TITLE_MAX = 120
ANNOUNCEMENT_BODY_MAX = 2000
TASK_TITLE_MAX = 120
TASK_DETAILS_MAX = 500

# Generated passwords: PASSWORD_LENGTH characters, always a mix of letters and numbers.
# Look-alike characters (0/O, 1/l/I) are left out on purpose.
PASSWORD_LENGTH = 8
PASSWORD_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PASSWORD_DIGITS = "23456789"

# ---------- [VOICE SECTION] ----------
# THIS IS THE LIST OF VOICE SECTIONS. Add, erase or reorder the names between the brackets.
# After saving this file, run   python manage_choices.py sync   in the terminal so the database
# follows (editing this list alone does NOT change a database that already exists).
DEFAULT_VOICE_SECTIONS = ["Soprano", "Alto", "Tenor", "Bass"]
# ---------- [CHOIR POSITION] ----------
# THIS IS THE LIST OF CHOIR POSITIONS. Add, erase or reorder the names between the brackets.
# After saving this file, run   python manage_choices.py sync   in the terminal so the database
# follows (editing this list alone does NOT change a database that already exists).
# A position is a role in the choir only. It never gives anyone access to this system.
# 'Member' is pre-selected for new members, so keep a position called Member.
DEFAULT_CHOIR_POSITIONS = [
    "Member", "Parish Priest", "Choir Director", "President", "Vice President",
    "Internal Secretary", "Treasurer", "External Vice President", "Undersecretary",
    "Public Relation Officer", "Public Relations Officer 1", "Public Relations Officer 2",
    "Documentation Staff", "Technical Staff", "Property Custodian"
]
# ---------- [ORGANIZATIONAL STRUCTURE] ----------
# The shape of the tree on the Members page. Each box is (key, title shown in the tree, position names that fill it).
# A person fills a box when their CHOIR POSITION is one of the names listed (capitals do not matter).
# So "Vice President" fills the Internal Vice President box and "Internal Secretary" fills the Secretary box.
# Boxes are always shown, even when nobody holds them. More than one person can hold the same position.
# Positions that are not listed here and are not "Member" appear under "Other positions" (nobody is hidden).
# To rename a box or accept another position name, edit the names below. Names, photos and voice sections
# always come from the Members records.
ORG_PRIEST = "Parish Priest"                       # shown in the tree, but never counted as a choir member
ORG_TOP = [
    ("priest", "Parish Priest", ("Parish Priest",)),
    ("director", "Choir Director", ("Choir Director",)),
]
ORG_PRESIDENT = ("president", "President", ("President",))
ORG_EXECUTIVE = [
    ("ivp", "Internal Vice President", ("Internal Vice President", "Vice President")),
    ("secretary", "Secretary", ("Secretary", "Internal Secretary")),
    ("treasurer", "Treasurer", ("Treasurer",)),
]
ORG_APPOINTED = [
    ("evp", "External Vice President", ("External Vice President",)),
    ("under", "Undersecretary", ("Undersecretary",)),
    ("pro1", "Public Relations Officer 1", ("Public Relations Officer 1", "Public Relation Officer 1")),
    ("pro2", "Public Relations Officer 2", ("Public Relations Officer 2", "Public Relation Officer 2")),
    ("doc", "Documentation Staff", ("Documentation Staff",)),
    ("tech", "Technical Staff", ("Technical Staff",)),
    ("prop", "Property Custodian", ("Property Custodian",)),
]
ORG_ORDINARY = "Member"                            # ordinary members: grouped by voice section in the tree

app = Flask(__name__)

_secret = os.environ.get("SECRET_KEY")
if not _secret or _secret.startswith("change-me"):
    # Fallback so the app still runs. Sessions reset whenever the server restarts.
    _secret = secrets.token_hex(32)
    print("WARNING: SECRET_KEY is not set in .env. Using a temporary key.")

app.config.update(
    SECRET_KEY=_secret,
    DATABASE=str(BASE_DIR / os.environ.get("DATABASE_PATH", "instance/jubilate_deo_chorale.db")),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    MAX_CONTENT_LENGTH=max(FILE_MAX_BYTES, MUSIC_MAX_BYTES) + 1024 * 1024,   # reject bigger requests (photos 2 MB, choir files 15 MB, music files MUSIC_MAX_MB; each checked separately)
)
# Profile photos live next to the database, NOT in the public static folder.
app.config["MEMBER_PHOTO_DIR"] = str(Path(app.config["DATABASE"]).parent / "uploads" / "member_photos")
# Shared choir files live next to the database too, never in the public static folder.
app.config["CHOIR_FILE_DIR"] = str(Path(app.config["DATABASE"]).parent / "uploads" / "choir_files")


# ---------------------------------------------------------------- database --

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")   # wait up to 5 seconds if the database is briefly busy
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(app.config["DATABASE"])
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name     TEXT    NOT NULL,
            email         TEXT    NOT NULL UNIQUE,
            password_hash TEXT    NOT NULL,
            role          TEXT    NOT NULL DEFAULT 'admin',
            created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- A record of administrative changes. The dashboard's "Recent activity"
        -- list reads from here. Add rows with log_activity() (see below).
        CREATE TABLE IF NOT EXISTS activity_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            action        TEXT    NOT NULL,
            detail        TEXT    NOT NULL DEFAULT '',
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_type   TEXT,
            target_id     INTEGER,
            created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_activity_log_recent
            ON activity_log (created_at DESC, id DESC);

        -- Voice sections and choir positions are rows, not fixed lists, so they can be
        -- renamed, reordered or retired (is_active = 0) later without touching code.
        -- [VOICE SECTION] the table that stores the voice sections (one row each). Its names are kept in step with DEFAULT_VOICE_SECTIONS by:  python manage_choices.py sync
        CREATE TABLE IF NOT EXISTS voice_sections (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            is_active   INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        -- [CHOIR POSITION] the table that stores the choir positions (one row each). Its names are kept in step with DEFAULT_CHOIR_POSITIONS by:  python manage_choices.py sync
        CREATE TABLE IF NOT EXISTS choir_positions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            is_active   INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- Choir members. These are NOT user accounts: a member does not need to sign in,
        -- and a choir position gives no access to the system. Members are never deleted;
        -- status = 'inactive' retires them. Future attendance / schedule tables should
        -- reference members(id) so their history is kept.
        CREATE TABLE IF NOT EXISTS members (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            member_code      TEXT    NOT NULL UNIQUE,
            first_name       TEXT    NOT NULL,
            middle_name      TEXT    NOT NULL DEFAULT '',
            last_name        TEXT    NOT NULL,
            email            TEXT    NOT NULL DEFAULT '',
            phone            TEXT    NOT NULL DEFAULT '',
            photo_filename   TEXT,
            -- [VOICE SECTION] every member has exactly one voice section
            voice_section_id INTEGER NOT NULL REFERENCES voice_sections(id),
            -- [CHOIR POSITION] every member has exactly one choir position
            position_id      INTEGER NOT NULL REFERENCES choir_positions(id),
            date_joined      TEXT    NOT NULL,
            -- [MEMBERSHIP STATUS] the database only accepts the three codes in the CHECK rule below. They must match MEMBER_STATUSES near the top of this file.
            status           TEXT    NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'temporarily_inactive', 'inactive')),
            notes            TEXT    NOT NULL DEFAULT '',
            created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
            updated_by       INTEGER REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_members_name
            ON members (last_name COLLATE NOCASE, first_name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_members_status   ON members (status);
        CREATE INDEX IF NOT EXISTS idx_members_voice    ON members (voice_section_id);
        CREATE INDEX IF NOT EXISTS idx_members_position ON members (position_id);
        CREATE INDEX IF NOT EXISTS idx_members_email    ON members (email COLLATE NOCASE);

        -- Announcements for the member dashboard. They are saved as 'queued' and published one at a
        -- time (see process_announcement_queue). audience_voice_id empty = general (everyone).
        -- is_active = 1 means published and still showing (the admin dashboard counts these).
        CREATE TABLE IF NOT EXISTS announcements (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            title             TEXT    NOT NULL,
            body              TEXT    NOT NULL,
            audience_voice_id INTEGER REFERENCES voice_sections(id),
            author_user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
            author_name       TEXT    NOT NULL DEFAULT '',
            status            TEXT    NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'published')),
            is_active         INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            published_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_announcements_feed ON announcements (status, is_active, published_at);

        -- Tasks assigned to a member (one row per member), ticked as done by the secretary.
        CREATE TABLE IF NOT EXISTS tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id    INTEGER NOT NULL REFERENCES members(id),
            title        TEXT    NOT NULL,
            details      TEXT    NOT NULL DEFAULT '',
            due_date     TEXT    NOT NULL,
            completed_at TEXT,
            completed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_member ON tasks (member_id, due_date);

        -- Attendance: one session per rehearsal/Mass, one record per member per session.
        CREATE TABLE IF NOT EXISTS attendance_sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            session_date TEXT NOT NULL,
            created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS attendance_records (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
            member_id  INTEGER NOT NULL REFERENCES members(id),
            status     TEXT NOT NULL CHECK (status IN ('present', 'late', 'excused', 'absent')),
            marked_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
            marked_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (session_id, member_id)
        );
        CREATE INDEX IF NOT EXISTS idx_attendance_member ON attendance_records (member_id);

        -- [SCHEDULE] Activities (rehearsals, Masses, meetings...). Names match what the dashboard already reads.
        -- uuid / updated_at / version are there so a future offline copy can sync safely.
        -- Activities are never deleted: "cancelled" keeps the record (and its attendance) intact.
        CREATE TABLE IF NOT EXISTS activities (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid          TEXT    NOT NULL UNIQUE,
            title         TEXT    NOT NULL,
            category      TEXT    NOT NULL DEFAULT 'other'
                          CHECK (category IN ('rehearsal', 'liturgical', 'meeting', 'formation', 'fellowship', 'parish', 'other')),
            activity_date TEXT    NOT NULL,               -- YYYY-MM-DD
            start_time    TEXT    NOT NULL DEFAULT '',    -- HH:MM
            location      TEXT    NOT NULL DEFAULT '',    -- the venue
            instructions  TEXT    NOT NULL DEFAULT '',
            status        TEXT    NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'cancelled')),
            cancel_reason TEXT    NOT NULL DEFAULT '',
            cancelled_at  TEXT,
            version       INTEGER NOT NULL DEFAULT 1,     -- goes up by 1 on every change
            created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
            updated_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_activities_date ON activities (activity_date, start_time);
        CREATE INDEX IF NOT EXISTS idx_activities_updated ON activities (updated_at);
        -- Remembers permanently deleted activities (only their uuid) so a future offline copy can remove them too.
        CREATE TABLE IF NOT EXISTS activity_deletions (
            uuid       TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_activity_deletions_at ON activity_deletions (deleted_at);
        -- Who an activity is for. No rows in either table = the whole choir.
        CREATE TABLE IF NOT EXISTS activity_voices (
            activity_id      INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
            voice_section_id INTEGER NOT NULL REFERENCES voice_sections(id),
            PRIMARY KEY (activity_id, voice_section_id)
        );
        CREATE TABLE IF NOT EXISTS activity_members (
            activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
            member_id   INTEGER NOT NULL REFERENCES members(id),
            PRIMARY KEY (activity_id, member_id)
        );

        -- Treasurer's report: money is stored in centavos (whole numbers) so there are no rounding errors.
        CREATE TABLE IF NOT EXISTS treasurer_reports (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date   TEXT    NOT NULL,          -- "as of" date
            money_in      INTEGER NOT NULL DEFAULT 0,
            money_out     INTEGER NOT NULL DEFAULT 0,
            balance       INTEGER NOT NULL DEFAULT 0,
            notes         TEXT    NOT NULL DEFAULT '',
            posted_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            posted_name   TEXT    NOT NULL DEFAULT '',
            created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_treasurer_date ON treasurer_reports (report_date);

        -- Wrong sign-in attempts, used to slow down password guessing.
        CREATE TABLE IF NOT EXISTS login_attempts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier   TEXT NOT NULL,
            ip           TEXT NOT NULL DEFAULT '',
            attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts (identifier, attempted_at);
        """
    )
    # [CHOIR FILES] Shared files. voice_section_id empty = General Choir Files, otherwise that voice section's files.
    # The files themselves are saved in instance/uploads/choir_files under a random name (stored_name).
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS choir_files (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            voice_section_id INTEGER REFERENCES voice_sections(id) ON DELETE SET NULL,
            title            TEXT    NOT NULL,
            description      TEXT    NOT NULL DEFAULT '',
            original_name    TEXT    NOT NULL,
            stored_name      TEXT    NOT NULL UNIQUE,
            size_bytes       INTEGER NOT NULL,
            version          INTEGER NOT NULL DEFAULT 1,
            uploaded_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
            uploader_name    TEXT    NOT NULL DEFAULT '',
            created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_choir_files_voice ON choir_files (voice_section_id)")
    # [MUSIC LIBRARY] One row per file: its details. The file itself is kept by music_storage.py under stored_name.
    # voice_section_id empty = "General". Archiving hides a file (is_archived = 1); nothing is deleted.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS music_files (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            title            TEXT    NOT NULL,
            category         TEXT    NOT NULL,
            voice_section_id INTEGER REFERENCES voice_sections(id) ON DELETE SET NULL,
            composer         TEXT    NOT NULL DEFAULT '',
            notes            TEXT    NOT NULL DEFAULT '',
            original_name    TEXT    NOT NULL,
            stored_name      TEXT    NOT NULL UNIQUE,
            ext              TEXT    NOT NULL,
            size_bytes       INTEGER NOT NULL,
            version          INTEGER NOT NULL DEFAULT 1,
            is_archived      INTEGER NOT NULL DEFAULT 0,
            archived_at      TEXT,
            archived_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
            uploaded_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
            uploader_name    TEXT    NOT NULL DEFAULT '',
            created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_music_files_list ON music_files (is_archived, category, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_music_files_voice ON music_files (voice_section_id)")
    # Accounts created before the log existed already have a real creation time in
    # the users table, so record those once. Nothing is invented: every row comes
    # from an actual account. Safe to run on every start.
    # One-time clean-up: earlier versions wrongly listed viewer and member accounts here as
    # "Administrator account created". Those entries were never true, so they are removed.
    db.execute(
        "DELETE FROM activity_log WHERE action = 'account.created' AND target_type = 'user' "
        "AND target_id IN (SELECT id FROM users WHERE role <> 'admin')"
    )
    db.execute(
        """
        INSERT INTO activity_log (action, detail, actor_user_id, target_type, target_id, created_at)
        SELECT 'account.created', full_name, id, 'user', id, created_at
          FROM users
         WHERE role = 'admin'
           AND id NOT IN (SELECT target_id FROM activity_log
                           WHERE action IN ('account.created', 'user.created')
                             AND target_type = 'user'
                             AND target_id IS NOT NULL)
        """
    )
    # Member sign-in accounts live in the users table (role 'member'). Older databases get the
    # extra columns added here; nothing is removed or rewritten. Names come from this fixed list.
    user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    for column, definition in (
        ("username", "TEXT"),                                    # the Member ID, e.g. JDC-0001
        ("member_id", "INTEGER REFERENCES members(id)"),         # which member this login belongs to
        ("must_change_password", "INTEGER NOT NULL DEFAULT 0"),  # 1 = generated by an admin; to be changed at first sign-in (enforced when the member area is built)
        ("password_set_at", "TEXT"),                             # when the current password was generated
    ):
        if column not in user_columns:
            db.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
    member_columns_now = {row[1] for row in db.execute("PRAGMA table_info(members)")}
    for column, definition in (
        ("deleted_at", "TEXT"),                                          # set = the member is in the ARCHIVE
        ("deleted_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),  # who moved them there
        ("nickname", "TEXT NOT NULL DEFAULT ''"),                        # filled in by the member at first sign-in
        ("birthday", "TEXT NOT NULL DEFAULT ''"),                        # YYYY-MM-DD; the age is worked out from it
        ("fb_link", "TEXT NOT NULL DEFAULT ''"),                         # Facebook profile link
        ("join_reason", "TEXT NOT NULL DEFAULT ''"),                     # why they want to join the choir
        ("profile_completed_at", "TEXT"),                                # empty = the member still has to fill in the form
    ):
        if column not in member_columns_now:
            db.execute(f"ALTER TABLE members ADD COLUMN {column} {definition}")
    # [SCHEDULE] An attendance session can belong to one scheduled activity (older sessions simply have none).
    if "activity_id" not in {row[1] for row in db.execute("PRAGMA table_info(attendance_sessions)")}:
        db.execute("ALTER TABLE attendance_sessions ADD COLUMN activity_id INTEGER REFERENCES activities(id)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_activity ON attendance_sessions (activity_id) "
               "WHERE activity_id IS NOT NULL")
    # [ANNOUNCEMENT LINK] A scheduled activity can be created together with an announcement. The activity points at
    # its announcement. Existing rows simply get NULL (= not linked), so nothing is rewritten. The unique index means
    # one announcement can never own two events, even if a form is submitted twice. If an announcement row were ever
    # removed, the event stays and just loses its link (ON DELETE SET NULL). Retiring an announcement changes only
    # announcements.is_active and never touches the event.
    if "announcement_id" not in {row[1] for row in db.execute("PRAGMA table_info(activities)")}:
        db.execute("ALTER TABLE activities ADD COLUMN announcement_id INTEGER REFERENCES announcements(id) ON DELETE SET NULL")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_announcement ON activities (announcement_id) "
               "WHERE announcement_id IS NOT NULL")
    db.execute("CREATE INDEX IF NOT EXISTS idx_members_deleted ON members (deleted_at)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users (username)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_member ON users (member_id)")
    # [FORGOT PASSWORD] One-time reset links. Only a hash of each token is stored, never the token itself.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS password_resets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT    NOT NULL UNIQUE,
            expires_at TEXT    NOT NULL,
            used_at    TEXT,
            created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # [ACTION CENTER] Per-member permission OVERRIDES. A row exists only where an administrator made a choice that
    # differs from what the member's choir position gives by default. No row = "use the default".
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS member_permissions (
            member_id  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
            permission TEXT    NOT NULL,
            granted    INTEGER NOT NULL CHECK (granted IN (0, 1)),
            updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            updated_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (member_id, permission)
        )
        """
    )

    # [ORGANIZATIONAL LEADERS] People who belong in the organizational structure but are NOT choir members
    # (for example the Parish Priest). A separate table on purpose: they have no voice section, no membership
    # status, no Member ID and no login, so no member count, task list, attendance sheet or sign-in can ever
    # pick them up. position_id uses the same choir_positions list as members. deleted_at = removed from the tree.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS org_leaders (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name     TEXT    NOT NULL,
            middle_name    TEXT    NOT NULL DEFAULT '',
            last_name      TEXT    NOT NULL,
            photo_filename TEXT,
            position_id    INTEGER NOT NULL REFERENCES choir_positions(id),
            created_at     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            updated_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            deleted_at     TEXT,
            deleted_by     INTEGER REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_org_leaders_position ON org_leaders (position_id, deleted_at)")

    # [VOICE SECTION] / [CHOIR POSITION] starting choices: used ONLY to fill an empty table the very first time.
    # (An existing database is changed with:  python manage_choices.py sync)
    if db.execute("SELECT COUNT(*) FROM voice_sections").fetchone()[0] == 0:
        db.executemany("INSERT INTO voice_sections (name, sort_order) VALUES (?, ?)",
                       list(zip(DEFAULT_VOICE_SECTIONS, range(1, 100))))
    if db.execute("SELECT COUNT(*) FROM choir_positions").fetchone()[0] == 0:
        db.executemany("INSERT INTO choir_positions (name, sort_order) VALUES (?, ?)",
                       list(zip(DEFAULT_CHOIR_POSITIONS, range(1, 100))))
    # [ORGANIZATIONAL STRUCTURE] An existing database may not have every position the tree needs. Add only the
    # missing ones (a box that already has a matching position, for example "Vice President", is left alone).
    known = {row[0].lower() for row in db.execute("SELECT name FROM choir_positions")}
    next_order = db.execute("SELECT COALESCE(MAX(sort_order), 0) FROM choir_positions").fetchone()[0]
    for _key, _label, names in ORG_TOP + [ORG_PRESIDENT] + ORG_EXECUTIVE + ORG_APPOINTED:
        if not any(name.lower() in known for name in names):
            next_order += 1
            db.execute("INSERT INTO choir_positions (name, sort_order) VALUES (?, ?)", (names[0], next_order))
            known.add(names[0].lower())
    db.commit()
    db.close()


def admin_exists():
    row = get_db().execute("SELECT 1 FROM users LIMIT 1").fetchone()
    return row is not None


# ---------------------------------------------------------- security helpers --

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.before_request
def protect_and_route():
    # Load the signed-in user (only these four fields: never the password hash).
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT id, full_name, email, role, member_id, must_change_password FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if g.user is not None and g.user["member_id"] is not None \
                and member_sign_in_problem(get_db(), g.user["member_id"]):
            g.user = None          # a member who was deactivated (or member sign-in is switched off)
        if g.user is None:
            session.clear()

    # A signed-in member's own record (name, voice section, position, photo) for the member area.
    g.member = None
    if g.user is not None and g.user["member_id"] is not None:
        g.member = get_db().execute(
            "SELECT m.id, m.member_code, m.first_name, m.middle_name, m.last_name, m.photo_filename, "
            "m.profile_completed_at, m.birthday, m.voice_section_id, vs.name AS voice_name, cp.name AS position_name "
            "FROM members m JOIN voice_sections vs ON vs.id = m.voice_section_id "
            "JOIN choir_positions cp ON cp.id = m.position_id WHERE m.id = ?", (g.user["member_id"],)).fetchone()

    # CSRF check for every POST. Both sides are compared as bytes so an odd token
    # (for example with accented letters) gives a clean 400 instead of a crash.
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("_csrf", "")
        if not sent or not secrets.compare_digest(sent.encode("utf-8"), expected.encode("utf-8")):
            abort(400, "Your session expired. Go back, refresh the page, and try again.")

    # First sign-in of a member: the given password is temporary, so choose a new one first.
    if (FORCE_PASSWORD_CHANGE and g.user is not None and g.user["member_id"] is not None
            and g.user["must_change_password"]
            and request.endpoint not in ("member_password", "member_my_photo", "logout", "static")):
        return redirect(url_for("member_password"))

    # After the password step, a member must fill in the "About you" form once before using the member area.
    if (g.member is not None and profile_incomplete(g.member)
            and request.endpoint not in ("member_profile_setup", "member_password", "member_my_photo", "member_service_worker",
                                     "logout", "static")):
        return redirect(url_for("member_profile_setup"))

    # First run: send everyone to create the administrator account.
    if request.endpoint not in ("setup_admin", "static") and not admin_exists():
        return redirect(url_for("setup_admin"))


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Sign in to continue.", "info")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# ----------------------------------------------------- permissions and errors --

def user_can(permission):
    """True if the signed-in user's role grants this permission."""
    user = getattr(g, "user", None)
    return user is not None and permission in ROLE_PERMISSIONS.get(user["role"], set())


def permission_required(permission):
    """Protect a route ON THE SERVER. Hiding a button is never enough on its own.

    Not signed in -> sent to the login page.  Signed in without the permission -> 403.
    """
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                flash("Sign in to continue.", "info")
                return redirect(url_for("login"))
            if not user_can(permission):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


ERROR_PAGES = {
    400: ("Request not accepted", None),
    403: ("You don't have access to this page",
          "Your account does not have permission to do that. Ask an administrator if you think this is a mistake."),
    404: ("Page not found", "The page you are looking for doesn't exist or may have moved."),
    413: ("File too large", f"That upload is too big. Profile photos must be 2 MB or smaller, choir files 15 MB or smaller and music files {MUSIC_MAX_MB} MB or smaller."),
    503: ("File storage unavailable", "The music file storage could not be reached just now. Please try again in a moment."),
}


def render_error(error):
    title, message = ERROR_PAGES.get(error.code, ("Something went wrong", None))
    return render_template("error.html", code=error.code, title=title,
                           message=message or error.description), error.code


for _code in ERROR_PAGES:
    app.register_error_handler(_code, render_error)


@app.template_filter("pretty_date")
def pretty_date_filter(value):
    """'2026-10-17' -> 'Oct 17, 2026'."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return value or ""
    return f"{day:%b} {day.day}, {day.year}"


@app.template_filter("peso")
def peso_filter(centavos):
    """123456 -> '₱1,234.56' (money is stored in centavos)."""
    try:
        return f"\u20b1{int(centavos) / 100:,.2f}"
    except (TypeError, ValueError):
        return ""


@app.template_filter("status_label")
def status_label_filter(value):
    return MEMBER_STATUSES.get(value, value or "")


@app.template_filter("pretty_stamp")
def pretty_stamp_filter(value):
    return format_stamp(value)


@app.context_processor
def inject_globals():
    logo_file = BASE_DIR / "static" / "images" / "logo.png"
    return {
        "app_name": APP_NAME,
        "tagline": TAGLINE,
        "csrf_token": csrf_token,
        "has_logo": logo_file.exists(),
        "current_year": datetime.now().year,
        "can": user_can,   # templates: {% if can('members.manage') %}
        "me": getattr(g, "member", None),         # the signed-in member's own record (member area)
        "member_password_change_enabled": MEMBER_PASSWORD_CHANGE_ENABLED,
        "member_can": member_can,                 # templates: {% if member_can('tasks.manage') %}
    }


# ------------------------------------------------------------ dashboard data --
#
# The dashboard only ever shows REAL data. When a feature has not been built yet
# (its table does not exist), the dashboard shows an empty state instead of a
# number. These are the tables it will read once each feature is added:
#
#   members         needs: status                    active members  = status = 'active'
#   activities      needs: title, activity_date      upcoming        = activity_date >= today
#                   optional: start_time, location   (activity_date as YYYY-MM-DD, start_time as HH:MM)
#   voice_sections  needs: (any table)               voice sections  = number of rows
#   announcements   needs: is_active                 active          = is_active = 1
#
# Table and column names below are fixed text written in this file, never user input.

ACTIVITY_LABELS = {
    "leader.created": "Organizational leader added",
    "leader.updated": "Organizational leader updated",
    "leader.removed": "Organizational leader removed",
    "leader.restored": "Organizational leader restored",
    "schedule.deleted": "Schedule deleted",
    "schedule.cleared": "Schedule cleared",
    "schedule.created": "Schedule added",
    "schedule.updated": "Schedule updated",
    "schedule.cancelled": "Schedule cancelled",
    "schedule.restored": "Schedule restored",
    "report.posted": "Treasurer's report posted",
    "report.removed": "Treasurer's report removed",
    "account.created": "Administrator account created",
    "user.created": "User account created",
    "member.created": "Member added",
    "member.updated": "Member details updated",
    "member.profile_completed": "Member filled in their details",
    "member.profile_updated": "Member updated their details",
    "member.deactivated": "Member deactivated",
    "member.restored": "Member restored",
    "member.status_changed": "Member status changed",
    "members.exported": "Members exported to CSV",
    "member.login_created": "Member login created",
    "member.password_reset": "Member password reset",
    "member.deleted": "Member moved to archive",
    "member.undeleted": "Member restored from archive",
    "member.purged": "Member permanently deleted",
    "member.password_changed": "Member chose a new password",
    "announcement.posted": "Announcement posted",
    "announcement.retired": "Announcement retired",
    "announcement.restored": "Announcement restored",
    "task.assigned": "Task assigned",
    "attendance.recorded": "Attendance recorded",
    "user.password_reset": "Account password reset by email link",
    "roster.reset": "Roster reset (all members cleared, Member IDs restart at JDC-0001)",
    "member.access_changed": "Member access changed (Action Center)",
    "member.access_reset": "Member access reset to position defaults",
    "music.uploaded": "Music file uploaded",
    "music.updated": "Music file updated",
    "music.archived": "Music file archived",
    "music.restored": "Music file restored",
}

# Quick actions. "endpoint" is the name of the Flask view function that will handle
# the page. The button becomes a real link automatically the day a route with that
# function name exists; until then it is shown as "Coming soon", never a broken link.
QUICK_ACTIONS = [
    {"label": "Add a member",          "hint": "Register a new choir member",  "endpoint": "members_new",
     "permission": "members.manage"},
    {"label": "Create a schedule",     "hint": "Plan a rehearsal or Mass",     "endpoint": "schedule_new"},
    {"label": "Post an announcement",  "hint": "Share news with the choir",    "endpoint": "announcements_new",
     "permission": "announcements.manage"},
    {"label": "Upload music",          "hint": "Add scores and recordings",    "endpoint": "music_upload",
     "permission": "music.manage"},
]


def log_activity(db, action, detail="", actor_user_id=None, target_type=None, target_id=None):
    """Record an administrative change for the dashboard's Recent activity list.

    Call it from any route that changes data, BEFORE db.commit(), so the change
    and its log entry are saved together. Add a friendly name for new action
    codes in ACTIVITY_LABELS.
    """
    db.execute(
        "INSERT INTO activity_log (action, detail, actor_user_id, target_type, target_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (action, detail, actor_user_id, target_type, target_id),
    )


def table_columns(db, table):
    """Column names of a table, or an empty set if the table does not exist yet."""
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if not exists:
        return set()
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def count_rows(db, table, where="", needs=()):
    """Number of rows, or None when the feature's table is not set up yet."""
    columns = table_columns(db, table)
    if not columns or not set(needs) <= columns:
        return None
    sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else "")
    try:
        return db.execute(sql).fetchone()[0]
    except sqlite3.Error:
        return None


def format_stamp(value):
    """'2026-10-17 07:42:00' (stored as UTC) -> 'Oct 17, 2026 · 3:42 PM' in local time."""
    try:
        moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
    except (TypeError, ValueError):
        return value or ""
    return f"{moment:%b} {moment.day}, {moment.year} \u00b7 " + moment.strftime("%I:%M %p").lstrip("0")


def describe_date(value):
    """'2026-10-17' -> {'month': 'OCT', 'day': '17', 'label': 'Sat, Oct 17'}."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return {"month": "", "day": "", "label": value or ""}
    label = f"{day:%a}, {day:%b} {day.day}"
    if day.year != datetime.now().year:
        label += f", {day.year}"
    return {"month": f"{day:%b}".upper(), "day": str(day.day), "label": label}


def format_time(value):
    """'19:30' -> '7:30 PM'. Returns '' when there is no time."""
    if not value:
        return ""
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, pattern).strftime("%I:%M %p").lstrip("0")
        except ValueError:
            continue
    return str(value)


def upcoming_activities(db, limit=5):
    """(total, items) for activities from today onward, or (None, None) if not set up."""
    columns = table_columns(db, "activities")
    if not {"title", "activity_date"} <= columns:
        return None, None
    has_time, has_place = "start_time" in columns, "location" in columns
    select = "title, activity_date" + (", start_time" if has_time else "") + (", location" if has_place else "")
    order = "activity_date" + (", start_time" if has_time else "")
    today = datetime.now().strftime("%Y-%m-%d")
    live = " AND status = 'scheduled'" if "status" in columns else ""      # cancelled activities are not "upcoming"
    try:
        total = db.execute(f"SELECT COUNT(*) FROM activities WHERE activity_date >= ?{live}", (today,)).fetchone()[0]
        rows = db.execute(
            f"SELECT {select} FROM activities WHERE activity_date >= ?{live} ORDER BY {order} LIMIT ?",
            (today, limit),
        ).fetchall()
    except sqlite3.Error:
        return None, None
    items = []
    for row in rows:
        when = describe_date(row["activity_date"])
        items.append({
            "title": row["title"],
            "month": when["month"],
            "day": when["day"],
            "date_label": when["label"],
            "time": format_time(row["start_time"]) if has_time else "",
            "location": (row["location"] or "") if has_place else "",
        })
    return total, items


def recent_activity(db, limit=6):
    """Latest recorded administrative changes, newest first."""
    rows = db.execute(
        """
        SELECT l.action, l.detail, l.created_at, u.full_name AS actor
          FROM activity_log l
          LEFT JOIN users u ON u.id = l.actor_user_id
         ORDER BY l.created_at DESC, l.id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "label": ACTIVITY_LABELS.get(row["action"], row["action"].replace(".", " ").replace("_", " ").capitalize()),
            "detail": row["detail"],
            "actor": row["actor"] or "",
            "when": format_stamp(row["created_at"]),
        }
        for row in rows
    ]


def summary_card(label, value, ready_note, empty_note, missing_note):
    """One summary card. state: 'missing' (feature not built), 'empty' (0), or 'ready'."""
    if value is None:
        state, note = "missing", missing_note
    elif value == 0:
        state, note = "empty", empty_note
    else:
        state, note = "ready", ready_note
    return {"label": label, "value": value, "state": state, "note": note}


def quick_actions():
    """Quick actions, linked only when the matching route really exists."""
    result = []
    for action in QUICK_ACTIONS:
        url, tag = None, "Coming soon"
        if action["endpoint"] in app.view_functions:
            needed = action.get("permission")
            if needed and not user_can(needed):
                tag = "No access"            # the page exists, but this user's role may not open it
            else:
                try:
                    url = url_for(action["endpoint"])
                except BuildError:
                    url = None
        result.append({**action, "url": url, "tag": tag})
    return result


# ------------------------------------------------------------------ routes --

def home_endpoint(role):
    """Where a role lands after signing in (None = nowhere yet)."""
    permissions = ROLE_PERMISSIONS.get(role, set())
    if "dashboard.view" in permissions:
        return "dashboard"
    if "member.home" in permissions:
        return "member_home"
    return None


@app.route("/")
def index():
    return redirect(url_for((home_endpoint(g.user["role"]) if g.user else None) or "login"))


@app.route("/setup", methods=["GET", "POST"])
def setup_admin():
    if admin_exists():
        flash("Setup is already complete. Sign in instead.", "info")
        return redirect(url_for("login"))

    form = {"full_name": "", "email": ""}
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        form.update(full_name=full_name, email=email)

        error = None
        if not full_name:
            error = "Enter your full name."
        elif "@" not in email or "." not in email.split("@")[-1]:
            error = "Enter a valid email address."
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f"Use a password with at least {MIN_PASSWORD_LENGTH} characters."
        elif password != confirm:
            error = "The two passwords do not match."

        if error:
            flash(error, "error")
        else:
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users (full_name, email, password_hash, role) VALUES (?, ?, ?, 'admin')",
                (full_name, email, generate_password_hash(password)),
            )
            new_id = cursor.lastrowid
            log_activity(db, "account.created", detail=full_name,
                         actor_user_id=new_id, target_type="user", target_id=new_id)
            db.commit()
            flash("Administrator account created. Sign in to begin.", "success")
            return redirect(url_for("login"))

    return render_template("setup_admin.html", form=form, min_length=MIN_PASSWORD_LENGTH)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user and home_endpoint(g.user["role"]):
        return redirect(url_for(home_endpoint(g.user["role"])))

    identifier = ""
    if request.method == "POST":
        identifier = " ".join((request.form.get("identifier") or request.form.get("email") or "").split())
        user, error = authenticate(get_db(), identifier, request.form.get("password", ""))
        if user:
            sign_in(user)
            return redirect(url_for(home_endpoint(user["role"])))
        flash(error, "error")

    return render_template("login.html", identifier=identifier)


# ---------------------------------------------------------- forgot password --
# [FORGOT PASSWORD] For STAFF accounts (administrators and viewers) that sign in with an email.
# Choir members sign in with a Member ID and have no real email: an administrator generates a new
# password for them from the member's page.
#
# How it works: the person enters their email -> if an account exists, a one-time link is
# emailed (valid RESET_LINK_MINUTES, usable once) -> the link opens a page to choose a new password.
#
# Safe by design: the page answers the same way whether or not the email exists (so nobody can
# discover which emails are registered), only a hash of the token is stored, a new request
# cancels older links, and requests are limited per account.
#
# EMAIL SETTINGS go in your .env file (see SMTP_* below). Without them nothing can be emailed, so
# the link is printed in the terminal where the app runs. That is fine on your own computer but
# useless for other people: set the SMTP_* values before giving others access.

RESET_LINK_MINUTES = 30
RESET_MAX_PER_HOUR = 3          # reset emails per account per hour


def smtp_settings():
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587") or 587),
        "user": os.environ.get("SMTP_USER", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "sender": os.environ.get("SMTP_FROM", "").strip() or os.environ.get("SMTP_USER", "").strip(),
        "ssl": os.environ.get("SMTP_SSL", "0") == "1",     # 1 = port 465 style, otherwise STARTTLS
    }


def send_reset_email(to_email, full_name, link):
    """Send the reset email (runs in a background thread so the page answers at the same speed either way)."""
    settings = smtp_settings()
    if settings is None:
        print("\n[FORGOT PASSWORD] Email is not set up (no SMTP_HOST in .env). Reset link for "
              f"{to_email}:\n  {link}\n")
        return
    message = EmailMessage()
    message["Subject"] = f"Reset your {APP_NAME} password"
    message["From"] = settings["sender"]
    message["To"] = to_email
    message.set_content(
        f"Hello {full_name},\n\n"
        f"Someone asked to reset the password for your {APP_NAME} account. "
        f"Open this link within {RESET_LINK_MINUTES} minutes to choose a new password:\n\n{link}\n\n"
        "If you did not ask for this, you can ignore this email. Your password will not change.\n"
    )
    try:
        if settings["ssl"]:
            server = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=15)
        else:
            server = smtplib.SMTP(settings["host"], settings["port"], timeout=15)
        with server:
            if not settings["ssl"]:
                server.starttls()
            if settings["user"]:
                server.login(settings["user"], settings["password"])
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        print(f"[FORGOT PASSWORD] The email to {to_email} could not be sent: {error}")


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def find_valid_reset(db, token):
    """The reset row (with its user) for a token that exists, is unused and has not expired."""
    return db.execute(
        "SELECT r.id, r.user_id, u.full_name, u.email FROM password_resets r JOIN users u ON u.id = r.user_id "
        "WHERE r.token_hash = ? AND r.used_at IS NULL AND r.expires_at > ? AND u.member_id IS NULL AND u.role <> 'member'",
        (token_hash(token), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if g.user and home_endpoint(g.user["role"]):
        return redirect(url_for(home_endpoint(g.user["role"])))
    email = ""
    if request.method == "POST":
        db = get_db()
        email = " ".join((request.form.get("email") or "").split()).lower()
        if not EMAIL_RE.match(email) or len(email) > 254:
            flash("Enter the email address you sign in with.", "error")
            return render_template("forgot_password.html", email=email)
        user = db.execute("SELECT id, full_name, email FROM users WHERE email = ? AND member_id IS NULL "
                          "AND role <> 'member'", (email,)).fetchone()
        if user:
            hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            recent = db.execute("SELECT COUNT(*) FROM password_resets WHERE user_id = ? AND created_at > ?",
                                (user["id"], hour_ago)).fetchone()[0]
            if recent < RESET_MAX_PER_HOUR:
                token = secrets.token_urlsafe(32)
                now = datetime.now(timezone.utc)
                db.execute("UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                           (now.strftime("%Y-%m-%d %H:%M:%S"), user["id"]))      # cancel older links
                db.execute("INSERT INTO password_resets (user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                           (user["id"], token_hash(token),
                            (now + timedelta(minutes=RESET_LINK_MINUTES)).strftime("%Y-%m-%d %H:%M:%S"),
                            now.strftime("%Y-%m-%d %H:%M:%S")))
                db.commit()
                base = os.environ.get("APP_BASE_URL", "").rstrip("/")
                path = url_for("reset_password", token=token)
                link = (base + path) if base else url_for("reset_password", token=token, _external=True)
                threading.Thread(target=send_reset_email, args=(user["email"], user["full_name"], link),
                                 daemon=True).start()
        # Same answer in every case, so nobody can learn which emails have accounts.
        flash("If that email belongs to an account, a reset link has been sent. It works for "
              f"{RESET_LINK_MINUTES} minutes. Check your spam folder too.", "info")
        return redirect(url_for("login"))
    return render_template("forgot_password.html", email=email)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if g.user and home_endpoint(g.user["role"]):
        return redirect(url_for(home_endpoint(g.user["role"])))
    db = get_db()
    reset = find_valid_reset(db, token) if len(token) < 200 else None
    if reset is None:
        flash("That reset link is not valid any more. Request a new one.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < MIN_PASSWORD_LENGTH:
            flash(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.", "error")
        elif password != request.form.get("confirm_password", ""):
            flash("The two passwords do not match.", "error")
        else:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("UPDATE users SET password_hash = ?, password_set_at = CURRENT_TIMESTAMP WHERE id = ?",
                       (generate_password_hash(password), reset["user_id"]))
            db.execute("UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                       (now, reset["user_id"]))                                # this link and any other
            # They may be locked out after wrong tries; a successful reset lifts that.
            db.execute("DELETE FROM login_attempts WHERE identifier = ?", (reset["email"].lower(),))
            log_activity(db, "user.password_reset", detail=reset["full_name"],
                         actor_user_id=reset["user_id"], target_type="user", target_id=reset["user_id"])
            db.commit()
            session.clear()
            flash("Your password was changed. You can sign in now.", "success")
            return redirect(url_for("login"))
    return render_template("reset_password.html", token=token, min_length=MIN_PASSWORD_LENGTH)


@app.route("/logout", methods=["POST"])
def logout():
    came_from_member_area = g.user is not None and g.user["member_id"] is not None
    session.clear()
    flash("You have signed out.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@permission_required("dashboard.view")
def dashboard():
    db = get_db()
    today = datetime.now().strftime("%A, %B %d, %Y").replace(" 0", " ")

    upcoming_total, upcoming = upcoming_activities(db)
    cards = [
        summary_card("Active members",
                     count_rows(db, "members", "status = 'active' AND deleted_at IS NULL", needs=("status", "deleted_at")),
                     "Currently marked active", "No active members yet",
                     "Member directory not set up yet"),
        summary_card("Upcoming activities", upcoming_total,
                     "From today onward", "Nothing scheduled yet",
                     "Schedule not set up yet"),
        summary_card("Voice sections",
                     count_rows(db, "voice_sections", "is_active = 1", needs=("is_active",)),
                     "Sections in the choir", "No voice sections yet",
                     "Voice sections not set up yet"),
        summary_card("Active announcements",
                     count_rows(db, "announcements", "is_active = 1", needs=("is_active",)),
                     "Currently showing", "No active announcements",
                     "Announcements not set up yet"),
    ]

    return render_template(
        "dashboard.html",
        today=today,
        cards=cards,
        upcoming=upcoming,
        upcoming_total=upcoming_total,
        recent=recent_activity(db),
        actions=quick_actions(),
    )


# ------------------------------------------------------------------ members --
#
# Members module. Rules that hold for everything below:
#   * Every route checks permission ON THE SERVER with permission_required().
#   * Members are never deleted. "Deactivate" only changes the status, so the future
#     attendance and schedule tables can keep pointing at members.id forever.
#   * Private fields (email, phone, notes) are only READ from the database when the
#     signed-in user's role allows it, so they cannot leak by accident.
#   * Every change is recorded with log_activity() for the dashboard's Recent activity.

MEMBER_JOINS = (
    "FROM members m "
    "JOIN voice_sections vs ON vs.id = m.voice_section_id "
    "JOIN choir_positions cp ON cp.id = m.position_id"
)


def member_columns(can_private, can_notes):
    """Columns to read. Private ones are added only when the user may see them."""
    cols = [
        "m.id", "m.member_code", "m.first_name", "m.middle_name", "m.last_name",
        "m.photo_filename", "m.voice_section_id", "m.position_id", "m.date_joined",
        "m.status", "m.created_at", "m.updated_at", "m.created_by", "m.updated_by",
        "m.deleted_at", "m.deleted_by",
        "vs.name AS voice_name", "cp.name AS position_name",
    ]
    if can_private:
        cols += ["m.email", "m.phone", "m.nickname", "m.fb_link", "m.join_reason"]
    if can_notes:
        cols.append("m.notes")
    return ", ".join(cols)


def collapse(value):
    """Trim and squeeze repeated spaces."""
    return " ".join((value or "").split())


def has_letters_ok(text):
    """True if the text contains at least one letter and no control characters."""
    return any(char.isalpha() for char in text) and not has_control_chars(text)


def has_control_chars(text, allow_newlines=False):
    for char in text:
        if ord(char) == 127 or (ord(char) < 32 and not (allow_newlines and char in "\n\t")):
            return True
    return False


def member_full_name(row):
    return " ".join(part for part in (row["first_name"], row["middle_name"], row["last_name"]) if part)


def photo_file_ok(name):
    """[DEFAULT AVATAR] True only when `name` is a well-formed stored photo name AND that file really exists on disk.
    A missing, empty, null, malformed or deleted photo gives False, so pages show the default avatar instead of a
    broken image. (A photo that exists but cannot be decoded by the browser is caught by app.js.)"""
    if not name or not isinstance(name, str) or not PHOTO_NAME_RE.match(name):
        return False
    try:
        return (Path(app.config["MEMBER_PHOTO_DIR"]) / name).is_file()
    except OSError:
        return False


app.add_template_global(photo_file_ok, "photo_file_ok")      # templates: {% if photo_file_ok(me.photo_filename) %}


def present_member(row):
    """A plain dict for templates, with display helpers added."""
    data = dict(row)
    data["display_name"] = member_full_name(row)
    data["initials"] = ((row["first_name"][:1] + row["last_name"][:1]) or "?").upper()
    data["has_photo"] = bool(row["photo_filename"])
    data["photo_ok"] = photo_file_ok(row["photo_filename"])      # the file is really there (used for the default avatar)
    return data


# [VOICE SECTION] and [CHOIR POSITION]: reads both lists from the database. The Add/Edit form and the filters all use this.
def load_choices(db):
    voices = db.execute("SELECT id, name, is_active FROM voice_sections ORDER BY sort_order, name").fetchall()
    positions = db.execute("SELECT id, name, is_active FROM choir_positions ORDER BY sort_order, name").fetchall()
    return voices, positions


def fetch_member(db, member_id, can_private, can_notes, scope="active"):
    """One member, or a 404. scope="active" hides archived members; "archived" finds only them."""
    archived = "m.deleted_at IS NOT NULL" if scope == "archived" else "m.deleted_at IS NULL"
    row = db.execute(
        f"SELECT {member_columns(can_private, can_notes)} {MEMBER_JOINS} WHERE m.id = ? AND {archived}",
        (member_id,),
    ).fetchone()
    if row is None:
        abort(404)
    return row


def safe_members_url(value):
    """Only allow redirects back to our own Members pages (no open redirects)."""
    fallback = url_for("members_list")
    if not value or any(ch in value for ch in ("\\", "\n", "\r")):
        return fallback
    parts = urlparse(value)
    if parts.scheme or parts.netloc:
        return fallback
    if parts.path != "/members" and not parts.path.startswith("/members/"):
        return fallback
    return value


# ---- profile photos -------------------------------------------------------------
# Photos are checked by their real file signature (not the file name), limited to
# 2 MB, stored under a random name OUTSIDE the public static folder, and shown only
# through member_photo(), which requires sign-in and permission.

def detect_image_type(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save_photo(data, extension):
    folder = Path(app.config["MEMBER_PHOTO_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{extension}"
    (folder / name).write_bytes(data)
    return name


def delete_photo(name):
    if name and PHOTO_NAME_RE.match(name):
        try:
            (Path(app.config["MEMBER_PHOTO_DIR"]) / name).unlink()
        except FileNotFoundError:
            pass


# ---- reading and validating the form ----------------------------------------------

def check_name(errors, key, label, value, required):
    if not value:
        if required:
            errors[key] = f"{label} is required."
    elif len(value) > NAME_MAX:
        errors[key] = f"{label} must be {NAME_MAX} characters or fewer."
    elif has_control_chars(value):
        errors[key] = f"{label} contains characters that are not allowed."
    elif not any(char.isalpha() for char in value):
        errors[key] = f"{label} must contain letters."


def read_member_form(db, existing, can_private, can_notes):
    """Validate the submitted member form.

    Returns (values, clean, errors, photo):
      values - what the person typed, to show again if something is wrong
      clean  - validated, typed values (only meaningful when errors is empty)
      errors - {field: message}
      photo  - (bytes, extension) for a new photo, or None
    """
    form = request.form
    errors = {}

    def text(name):
        return collapse(form.get(name))

    values = {
        "first_name": text("first_name"),
        "middle_name": text("middle_name"),
        "last_name": text("last_name"),
        "voice_section_id": form.get("voice_section_id", "").strip(),
        "position_id": form.get("position_id", "").strip(),
        "date_joined": form.get("date_joined", "").strip(),
        "status": form.get("status", "").strip(),
        # Email and contact number are NOT typed by administrators any more: members fill them in
        # themselves. Whatever is already stored is kept untouched here.
        "email": existing["email"] if existing else "",
        "phone": existing["phone"] if existing else "",
        "notes": (form.get("notes", "").replace("\r\n", "\n").strip() if can_notes
                  else (existing["notes"] if existing else "")),
    }
    clean = dict(values)

    check_name(errors, "first_name", "First name", values["first_name"], True)
    check_name(errors, "middle_name", "Middle name", values["middle_name"], False)
    check_name(errors, "last_name", "Last name", values["last_name"], True)

    voices, positions = load_choices(db)
    # [VOICE SECTION] and [CHOIR POSITION]: the submitted choice must be a real one from the database
    # (an active one, or the one the member already has).
    for key, label, options, current in (
        ("voice_section_id", "voice section", voices, existing["voice_section_id"] if existing else None),
        ("position_id", "choir position", positions, existing["position_id"] if existing else None),
    ):
        raw = values[key]
        allowed = {row["id"] for row in options if row["is_active"]}
        if current is not None:
            allowed.add(current)          # a member may keep a section/position that was retired later
        if not raw:
            errors[key] = f"Choose a {label}."
        elif not re.fullmatch(r"[0-9]{1,9}", raw) or int(raw) not in allowed:
            errors[key] = f"Choose a valid {label}."
        else:
            clean[key] = int(raw)

    raw_date = values["date_joined"]
    if not raw_date:
        errors["date_joined"] = "Date joined is required."
    else:
        try:
            joined = datetime.strptime(raw_date, "%Y-%m-%d").date()
            if joined > date.today():
                errors["date_joined"] = "Date joined can't be in the future."
            elif joined < date(1900, 1, 1):
                errors["date_joined"] = "Enter a date from 1900 onward."
        except ValueError:
            errors["date_joined"] = "Enter a valid date."

    # [MEMBERSHIP STATUS]: the submitted status must be one of MEMBER_STATUSES (top of this file).
    if values["status"] not in MEMBER_STATUSES:
        errors["status"] = "Choose a membership status."

    if can_notes:
        if len(values["notes"]) > NOTES_MAX:
            errors["notes"] = f"Notes must be {NOTES_MAX} characters or fewer."
        elif has_control_chars(values["notes"], allow_newlines=True):
            errors["notes"] = "Notes contain characters that are not allowed."

    photo = None
    upload = request.files.get("photo")
    if upload and upload.filename:
        data = upload.read(PHOTO_MAX_BYTES + 1)
        kind = detect_image_type(data) if data else None
        if len(data) > PHOTO_MAX_BYTES:
            errors["photo"] = "The photo must be 2 MB or smaller."
        elif kind is None:
            errors["photo"] = "Upload a JPG, PNG or WebP image."
        else:
            photo = (data, kind)

    return values, clean, errors, photo


def find_possible_duplicates(db, clean, exclude_id=None):
    """Members that may be the same person: same name, email or contact number."""
    rows = db.execute(
        """
        SELECT id, member_code, first_name, middle_name, last_name, email, phone, deleted_at
          FROM members
         WHERE (? IS NULL OR id <> ?)
           AND ( (LOWER(first_name) = LOWER(?) AND LOWER(last_name) = LOWER(?))
              OR (? <> '' AND LOWER(email) = LOWER(?))
              OR (? <> '' AND phone = ?) )
         ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE
         LIMIT 5
        """,
        (exclude_id, exclude_id, clean["first_name"], clean["last_name"],
         clean["email"], clean["email"], clean["phone"], clean["phone"]),
    ).fetchall()
    found = []
    for row in rows:
        reasons = []
        if row["first_name"].lower() == clean["first_name"].lower() and row["last_name"].lower() == clean["last_name"].lower():
            reasons.append("same name")
        if clean["email"] and row["email"].lower() == clean["email"].lower():
            reasons.append("same email")
        if clean["phone"] and row["phone"] == clean["phone"]:
            reasons.append("same contact number")
        found.append({"id": row["id"], "code": row["member_code"], "archived": row["deleted_at"] is not None,
                      "name": member_full_name(row), "reasons": ", ".join(reasons)})
    # A non-member leader with the same name is the same person: warn here too, so nobody is recorded twice.
    for row in db.execute(
            "SELECT id, first_name, middle_name, last_name FROM org_leaders WHERE deleted_at IS NULL "
            "AND LOWER(first_name) = LOWER(?) AND LOWER(last_name) = LOWER(?) LIMIT 3",
            (clean["first_name"], clean["last_name"])):
        found.append({"id": LEADER_ID_OFFSET + row["id"], "code": "Organizational leader", "archived": False,
                      "name": member_full_name(row), "reasons": "same name"})
    return found


# [MEMBERSHIP STATUS]: writes a status change into the member's history.
def log_status_change(db, member_id, name, old, new):
    if new == "inactive":
        action = "member.deactivated"
    elif new == "active" and old != "active":
        action = "member.restored"
    else:
        action = "member.status_changed"
    log_activity(db, action, detail=f"{name} ({MEMBER_STATUSES[old]} to {MEMBER_STATUSES[new]})",
                 actor_user_id=g.user["id"], target_type="member", target_id=member_id)


# ---- list: search, filters, pagination ---------------------------------------------

def like_escape(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def name_search_clauses(q, can_private):
    """Search words -> safe SQL conditions. Every word must match somewhere."""
    clauses, params = [], []
    for token in q.split()[:6]:
        like = "%" + like_escape(token) + "%"
        fields = ["m.first_name", "m.middle_name", "m.last_name", "m.member_code"]
        if can_private:                       # searching email/phone would leak them, so only for allowed users
            fields += ["m.email", "m.phone"]
        clauses.append("(" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields) + ")")
        params += [like] * len(fields)
    return clauses, params


def member_filters(db, args, can_private):
    """Turn the search box and the three filters into a safe, parameterised WHERE."""
    q = collapse(args.get("q"))[:80]
    voice = args.get("voice", "").strip()
    position = args.get("position", "").strip()
    status = args.get("status", "").strip()

    # [VOICE SECTION] / [CHOIR POSITION] / [MEMBERSHIP STATUS] filters on the Members page:
    # a value that is not a real choice is simply ignored.
    if not (re.fullmatch(r"[0-9]{1,9}", voice) and db.execute("SELECT 1 FROM voice_sections WHERE id = ?", (int(voice),)).fetchone()):
        voice = ""
    if not (re.fullmatch(r"[0-9]{1,9}", position) and db.execute("SELECT 1 FROM choir_positions WHERE id = ?", (int(position),)).fetchone()):
        position = ""
    if status not in MEMBER_STATUSES:
        status = ""

    clauses, params = name_search_clauses(q, can_private)
    clauses.insert(0, "m.deleted_at IS NULL")      # deleted members live in the archive, never in the roster
    if voice:
        clauses.append("m.voice_section_id = ?")
        params.append(int(voice))
    if position:
        clauses.append("m.position_id = ?")
        params.append(int(position))
    if status:
        clauses.append("m.status = ?")
        params.append(status)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params, {"q": q, "voice": voice, "position": position, "status": status}


def build_pager(page, pages, link):
    numbers = sorted({1, pages, page - 1, page, page + 1} & set(range(1, pages + 1)))
    items, previous = [], 0
    for number in numbers:
        if previous and number - previous > 1:
            items.append({"gap": True})
        items.append({"gap": False, "n": number, "url": link(number), "current": number == page})
        previous = number
    return items


ORDER_BY_NAME = "ORDER BY m.last_name COLLATE NOCASE, m.first_name COLLATE NOCASE, m.id"


# [ORGANIZATIONAL STRUCTURE] Builds the tree from the Members records. Nothing is typed in by hand.
# Only active, non-archived members appear. Every member lands in exactly ONE place (the box for their position,
# their voice section if they are an ordinary member, or "Other positions"), so nobody is counted twice.
# The SAME function feeds the administrator Members page and the member-area Members page, so the two can never
# disagree. Only the links differ: member_area=True uses member-area routes and the member's own permissions.
def build_org_structure(db, member_area=False):
    # Where a name and a photo lead. None = no link / no photo (initials are shown instead).
    if member_area:
        may_open = member_can("panel.directory")        # the same right that guards the profile and photo routes
        def link_for(person_id, is_leader):
            return url_for("choir_member_view", member_id=person_id) if may_open and not is_leader else None
        def photo_for(person_id):
            return url_for("choir_member_photo", member_id=person_id) if may_open else None
    else:
        def link_for(person_id, is_leader):
            return url_for("member_detail", member_id=person_id)
        def photo_for(person_id):
            return url_for("member_photo", member_id=person_id)

    voices = db.execute("SELECT id, name, is_active FROM voice_sections ORDER BY sort_order, name").fetchall()
    rows = db.execute(
        "SELECT m.id, m.first_name, m.middle_name, m.last_name, m.photo_filename, m.voice_section_id, "
        "cp.name AS position_name FROM members m "
        "JOIN choir_positions cp ON cp.id = m.position_id "
        f"WHERE m.deleted_at IS NULL AND m.status = 'active' {ORDER_BY_NAME}"
    ).fetchall()

    slot_for = {}                                    # position name (lower case) -> the box it fills

    def box(definition):
        key, label, names = definition
        slot = {"key": key, "label": label, "people": []}
        for name in names:
            slot_for.setdefault(name.lower(), slot)
        return slot

    top = [box(d) for d in ORG_TOP]
    president = box(ORG_PRESIDENT)
    executive = [box(d) for d in ORG_EXECUTIVE]
    appointed = [box(d) for d in ORG_APPOINTED]
    sections = {v["id"]: {"id": v["id"], "name": v["name"], "active": bool(v["is_active"]), "people": []} for v in voices}
    others = {}                                      # position name -> people with a position the tree has no box for

    seen, choir_total = set(), 0
    for row in rows:
        if row["id"] in seen:                        # safety net: one person is never placed twice
            continue
        seen.add(row["id"])
        position = row["position_name"].lower()
        person = {"id": row["id"], "name": f"{row['first_name']} {row['last_name']}".strip(),
                  "full_name": member_full_name(row), "has_photo": bool(row["photo_filename"]),
                  "initials": ((row["first_name"][:1] + row["last_name"][:1]) or "?").upper(),
                  "photo_url": photo_for(row["id"]) if photo_file_ok(row["photo_filename"]) else None,
                  "profile_url": link_for(row["id"], False)}
        if position != ORG_PRIEST.lower():
            choir_total += 1                         # the Parish Priest is shown, never counted
        if position in slot_for:
            slot_for[position]["people"].append(person)
        elif position == ORG_ORDINARY.lower():
            sections[row["voice_section_id"]]["people"].append(person)
        else:
            others.setdefault(row["position_name"], []).append(person)

    # [ORGANIZATIONAL LEADERS] Non-member leaders fill the box for their position, exactly like members, but they
    # are NOT added to choir_total (they are not choir members) and they never appear in a voice section.
    for leader in db.execute(
            "SELECT l.id, l.first_name, l.middle_name, l.last_name, l.photo_filename, cp.name AS position_name "
            "FROM org_leaders l JOIN choir_positions cp ON cp.id = l.position_id WHERE l.deleted_at IS NULL "
            "ORDER BY l.last_name COLLATE NOCASE, l.first_name COLLATE NOCASE, l.id").fetchall():
        shown_id = LEADER_ID_OFFSET + leader["id"]
        person = {"id": shown_id, "kind": "leader", "name": f"{leader['first_name']} {leader['last_name']}".strip(),
                  "full_name": member_full_name(leader), "has_photo": bool(leader["photo_filename"]),
                  "initials": ((leader["first_name"][:1] + leader["last_name"][:1]) or "?").upper(),
                  "photo_url": photo_for(shown_id) if photo_file_ok(leader["photo_filename"]) else None,
                  "profile_url": link_for(shown_id, True)}
        position = leader["position_name"].lower()
        if position in slot_for:
            slot_for[position]["people"].append(person)
        else:
            others.setdefault(leader["position_name"], []).append(person)

    section_list = [s_ for s_ in sections.values() if s_["active"] or s_["people"]]
    boxes = top + [president] + executive + appointed
    return {
        "top": top, "president": president, "executive": executive, "appointed": appointed,
        "sections": section_list,
        "others": [{"key": "other", "label": name, "people": people} for name, people in sorted(others.items())],
        "choir_total": choir_total,
        "ordinary_total": sum(len(s_["people"]) for s_ in section_list),
        "vacant": sum(1 for b in boxes if not b["people"]),
    }


@app.route("/members")
@permission_required("members.view")
def members_list():
    db = get_db()
    can_private = user_can("members.private")
    where, params, filters = member_filters(db, request.args, can_private)

    found = db.execute(f"SELECT COUNT(*) FROM members m {where}", params).fetchone()[0]
    pages = max(1, -(-found // PAGE_SIZE))
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = min(max(page, 1), pages)

    rows = db.execute(
        f"SELECT {member_columns(False, False)} {MEMBER_JOINS} {where} {ORDER_BY_NAME} LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()

    # [MEMBERSHIP STATUS]: counts for the Total / Active / Inactive summary cards (archived members are not counted).
    # The Parish Priest is not a choir member, so he is left out of these counts (he stays in the roster below).
    stats = db.execute(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(m.status = 'active'), 0) AS active,"
        " COALESCE(SUM(m.status = 'temporarily_inactive'), 0) AS temp,"
        " COALESCE(SUM(m.status = 'inactive'), 0) AS inactive "
        "FROM members m JOIN choir_positions cp ON cp.id = m.position_id "
        "WHERE m.deleted_at IS NULL AND cp.name <> ? COLLATE NOCASE", (ORG_PRIEST,)
    ).fetchone()
    archived_count = (db.execute("SELECT COUNT(*) FROM members WHERE deleted_at IS NOT NULL").fetchone()[0]
                      if user_can("members.archive") else 0)
    # [VOICE SECTION]: the number on the Voice sections summary card (sections in use).
    sections = db.execute("SELECT COUNT(*) FROM voice_sections WHERE is_active = 1").fetchone()[0]
    away = stats["temp"] + stats["inactive"]
    cards = [
        summary_card("Total members", stats["total"], "Every record, any status", "No members yet", ""),
        summary_card("Active members", stats["active"], "Currently marked active", "No active members", ""),
        summary_card("Inactive members", away,
                     f"Includes {stats['temp']} temporarily inactive" if stats["temp"] else "Not currently singing",
                     "No inactive members", ""),
        summary_card("Voice sections", sections, "Available to choose from", "No voice sections", ""),
    ]

    active_filters = {key: value for key, value in filters.items() if value}
    voices, positions = load_choices(db)
    first_item = (page - 1) * PAGE_SIZE + 1 if found else 0
    return render_template(
        "members.html",
        org=build_org_structure(db),
        cards=cards,
        members=[present_member(row) for row in rows],
        filters=filters,
        has_filters=bool(active_filters),
        voices=voices,
        positions=positions,
        statuses=MEMBER_STATUSES,
        total_members=stats["total"],
        archived_count=archived_count,
        found=found,
        first_item=first_item,
        last_item=first_item + len(rows) - 1 if found else 0,
        page=page,
        pages=pages,
        pager=build_pager(page, pages, lambda n: url_for("members_list", **{**active_filters, "page": n})),
        prev_url=url_for("members_list", **{**active_filters, "page": page - 1}) if page > 1 else None,
        next_url=url_for("members_list", **{**active_filters, "page": page + 1}) if page < pages else None,
    )


# ---- add / view / edit / status / photo / export ---------------------------------

def render_member_form(db, mode, values, errors, member=None, duplicates=None, leader=None):
    """The Add person / Edit form. A choir member and a non-member leader share one page (see RECORD_TYPES)."""
    voices, positions = load_choices(db)
    current_voice = member["voice_section_id"] if member else None
    current_position = member["position_id"] if member else (leader["position_id"] if leader else None)
    record_type = "leader" if leader else (values.get("record_type") if values.get("record_type") in RECORD_TYPES else "member")
    return render_template(
        "member_form.html",
        mode=mode,
        record_type=record_type,
        values=values,
        errors=errors,
        member=present_member(member) if member else None,
        leader=present_leader(leader) if leader else None,
        voices=[v for v in voices if v["is_active"] or v["id"] == current_voice],
        positions=[{**dict(p), "ordinary": p["name"].lower() == ORG_ORDINARY.lower()}
                   for p in positions if p["is_active"] or p["id"] == current_position],
        statuses=MEMBER_STATUSES,
        duplicates=duplicates or [],
        today=date.today().isoformat(),
        photo_max_mb=PHOTO_MAX_BYTES // (1024 * 1024),
    )


@app.route("/members/new", methods=["GET", "POST"])
@permission_required("members.manage")
def members_new():
    db = get_db()
    can_private, can_notes = user_can("members.private"), user_can("members.notes")

    if request.method == "GET":
        default_position = db.execute(
            "SELECT id FROM choir_positions WHERE name = 'Member' AND is_active = 1"
        ).fetchone()
        values = {
            "first_name": "", "middle_name": "", "last_name": "", "email": "", "phone": "",
            "voice_section_id": "", "position_id": str(default_position["id"]) if default_position else "",
            "date_joined": date.today().isoformat(), "status": "active", "notes": "",
            "record_type": "leader" if request.args.get("type") == "leader" else "member",
        }
        return render_member_form(db, "new", values, {})

    if request.form.get("record_type") == "leader":          # a non-member organizational leader
        return save_new_leader(db)

    values, clean, errors, photo = read_member_form(db, None, can_private, can_notes)
    if errors:
        flash("Please fix the highlighted fields and try again.", "error")
        return render_member_form(db, "new", values, errors)

    duplicates = find_possible_duplicates(db, clean)
    if duplicates and not request.form.get("confirm_duplicate"):
        return render_member_form(db, "new", values, {}, duplicates=duplicates)

    photo_name = save_photo(*photo) if photo else None
    name = member_full_name(clean)
    password = None
    try:
        cursor = db.execute(
            "INSERT INTO members (member_code, first_name, middle_name, last_name, email, phone,"
            " photo_filename, voice_section_id, position_id, date_joined, status, notes, created_by, updated_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"PENDING-{uuid.uuid4().hex}", clean["first_name"], clean["middle_name"], clean["last_name"],
             clean["email"], clean["phone"], photo_name, clean["voice_section_id"], clean["position_id"],
             clean["date_joined"], clean["status"], clean["notes"], g.user["id"], g.user["id"]),
        )
        member_id = cursor.lastrowid
        # The Member ID comes from the record's own unique number, so it can never repeat.
        code = f"JDC-{member_id:04d}"
        db.execute("UPDATE members SET member_code = ? WHERE id = ?", (code, member_id))
        log_activity(db, "member.created", detail=name, actor_user_id=g.user["id"],
                     target_type="member", target_id=member_id)
        if user_can("members.credentials"):
            # Sign-in account: username = Member ID, password = generated now (shown once).
            password, _created = set_member_password(db, member_id, code, name)
            log_activity(db, "member.login_created", detail=name, actor_user_id=g.user["id"],
                         target_type="member", target_id=member_id)
        db.commit()
    except sqlite3.Error:
        db.rollback()
        delete_photo(photo_name)
        flash("The member could not be saved. Please try again.", "error")
        return render_member_form(db, "new", values, {})

    if password:
        remember_credentials(member_id, code, password)
    flash(f"{name} was added as {code}.", "success")
    return redirect(url_for("member_detail", member_id=member_id))


# ------------------------------------------------ non-member organizational leaders --
# [ORGANIZATIONAL LEADERS] People such as the Parish Priest who belong in the organizational structure but are
# not choir members. They live in their own table (org_leaders), so the member counts, the roster, tasks,
# attendance and member sign-in never see them. Everything here is for administrators who may manage members.

def present_leader(row):
    data = dict(row)
    data["display_name"] = member_full_name(row)
    data["initials"] = ((row["first_name"][:1] + row["last_name"][:1]) or "?").upper()
    data["has_photo"] = bool(row["photo_filename"])
    data["photo_ok"] = photo_file_ok(row["photo_filename"])
    return data


def fetch_leader(db, leader_id, removed=False):
    row = db.execute(
        "SELECT l.*, cp.name AS position_name FROM org_leaders l JOIN choir_positions cp ON cp.id = l.position_id "
        f"WHERE l.id = ? AND l.deleted_at IS {'NOT ' if removed else ''}NULL", (leader_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def read_leader_form(db, existing):
    """Validate the leader form. Returns (values, clean, errors, photo) like read_member_form."""
    form, errors = request.form, {}
    values = {"record_type": "leader", "first_name": collapse(form.get("first_name")),
              "middle_name": collapse(form.get("middle_name")), "last_name": collapse(form.get("last_name")),
              "position_id": form.get("position_id", "").strip()}
    clean = dict(values)
    check_name(errors, "first_name", "First name", values["first_name"], True)
    check_name(errors, "middle_name", "Middle name", values["middle_name"], False)
    check_name(errors, "last_name", "Last name", values["last_name"], True)

    _voices, positions = load_choices(db)
    allowed = {row["id"]: row["name"] for row in positions if row["is_active"]}
    if existing is not None:
        allowed.setdefault(existing["position_id"], existing["position_name"])    # may keep a position retired later
    raw = values["position_id"]
    if not raw:
        errors["position_id"] = "Choose an organizational position."
    elif not re.fullmatch(r"[0-9]{1,9}", raw) or int(raw) not in allowed:
        errors["position_id"] = "Choose a valid organizational position."
    elif allowed[int(raw)].lower() == ORG_ORDINARY.lower():
        errors["position_id"] = "Ordinary Member is for choir members. Choose Choir Member as the record type instead."
    else:
        clean["position_id"] = int(raw)

    photo = None
    upload = request.files.get("photo")
    if upload and upload.filename:
        data = upload.read(PHOTO_MAX_BYTES + 1)
        kind = detect_image_type(data) if data else None
        if len(data) > PHOTO_MAX_BYTES:
            errors["photo"] = "The photo must be 2 MB or smaller."
        elif kind is None:
            errors["photo"] = "Upload a JPG, PNG or WebP image."
        else:
            photo = (data, kind)
    return values, clean, errors, photo


def find_leader_duplicates(db, clean, exclude_id=None):
    """Leaders or choir members with the same name, so the same person is not recorded twice."""
    found = []
    for row in db.execute(
            "SELECT id, first_name, middle_name, last_name FROM org_leaders WHERE deleted_at IS NULL "
            "AND (? IS NULL OR id <> ?) AND LOWER(first_name) = LOWER(?) AND LOWER(last_name) = LOWER(?) LIMIT 3",
            (exclude_id, exclude_id, clean["first_name"], clean["last_name"])):
        found.append({"id": LEADER_ID_OFFSET + row["id"], "code": "Organizational leader", "archived": False,
                      "name": member_full_name(row), "reasons": "same name"})
    for row in db.execute(
            "SELECT id, member_code, first_name, middle_name, last_name, deleted_at FROM members "
            "WHERE LOWER(first_name) = LOWER(?) AND LOWER(last_name) = LOWER(?) LIMIT 3",
            (clean["first_name"], clean["last_name"])):
        found.append({"id": row["id"], "code": row["member_code"], "archived": row["deleted_at"] is not None,
                      "name": member_full_name(row), "reasons": "same name (already a choir member)"})
    return found


def save_new_leader(db):
    values, clean, errors, photo = read_leader_form(db, None)
    if errors:
        flash("Please fix the highlighted fields and try again.", "error")
        return render_member_form(db, "new", values, errors)
    duplicates = find_leader_duplicates(db, clean)
    if duplicates and not request.form.get("confirm_duplicate"):
        return render_member_form(db, "new", values, {}, duplicates=duplicates)

    photo_name = save_photo(*photo) if photo else None
    name = member_full_name(clean)
    position = db.execute("SELECT name FROM choir_positions WHERE id = ?", (clean["position_id"],)).fetchone()["name"]
    try:
        cursor = db.execute(
            "INSERT INTO org_leaders (first_name, middle_name, last_name, photo_filename, position_id, created_by, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (clean["first_name"], clean["middle_name"], clean["last_name"], photo_name, clean["position_id"],
             g.user["id"], g.user["id"]))
        log_activity(db, "leader.created", detail=f"{name} ({position})", actor_user_id=g.user["id"],
                     target_type="leader", target_id=cursor.lastrowid)
        db.commit()
    except sqlite3.Error:
        db.rollback()
        delete_photo(photo_name)
        flash("The person could not be saved. Please try again.", "error")
        return render_member_form(db, "new", values, {})
    flash(f"{name} was added to the organizational structure as {position}. Not counted as a choir member.", "success")
    return redirect(url_for("leaders_list"))


@app.route("/members/leaders")
@permission_required("members.view")
def leaders_list():
    db = get_db()
    base = ("SELECT l.*, cp.name AS position_name FROM org_leaders l JOIN choir_positions cp ON cp.id = l.position_id "
            "WHERE l.deleted_at IS {} NULL ORDER BY l.last_name COLLATE NOCASE, l.first_name COLLATE NOCASE, l.id")
    return render_template(
        "leaders.html",
        leaders=[present_leader(r) for r in db.execute(base.format("")).fetchall()],
        removed=[present_leader(r) for r in db.execute(base.format("NOT")).fetchall()] if user_can("members.manage") else [])


@app.route("/members/leaders/<int(max=2147483647):leader_id>/edit", methods=["GET", "POST"])
@permission_required("members.manage")
def leader_edit(leader_id):
    db = get_db()
    leader = fetch_leader(db, leader_id)
    if request.method == "GET":
        values = {"record_type": "leader", "first_name": leader["first_name"], "middle_name": leader["middle_name"],
                  "last_name": leader["last_name"], "position_id": str(leader["position_id"])}
        return render_member_form(db, "edit", values, {}, leader=leader)

    values, clean, errors, photo = read_leader_form(db, leader)
    remove_photo = bool(request.form.get("remove_photo"))
    if errors:
        flash("Please fix the highlighted fields and try again.", "error")
        return render_member_form(db, "edit", values, errors, leader=leader)

    name_changed = any(clean[k] != leader[k] for k in ("first_name", "middle_name", "last_name"))
    changes = (["name"] if name_changed else []) + (["position"] if clean["position_id"] != leader["position_id"] else []) \
        + (["photo"] if photo or (remove_photo and leader["photo_filename"]) else [])
    if not changes:
        flash("No changes to save.", "info")
        return redirect(url_for("leaders_list"))
    if name_changed:
        duplicates = find_leader_duplicates(db, clean, exclude_id=leader_id)
        if duplicates and not request.form.get("confirm_duplicate"):
            return render_member_form(db, "edit", values, {}, duplicates=duplicates, leader=leader)

    old_photo = leader["photo_filename"]
    new_photo = save_photo(*photo) if photo else (None if remove_photo else old_photo)
    name = member_full_name(clean)
    try:
        db.execute("UPDATE org_leaders SET first_name = ?, middle_name = ?, last_name = ?, position_id = ?, "
                   "photo_filename = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
                   (clean["first_name"], clean["middle_name"], clean["last_name"], clean["position_id"], new_photo,
                    g.user["id"], leader_id))
        log_activity(db, "leader.updated", detail=f"{name} ({', '.join(changes)})", actor_user_id=g.user["id"],
                     target_type="leader", target_id=leader_id)
        db.commit()
    except sqlite3.Error:
        db.rollback()
        if photo:
            delete_photo(new_photo)
        flash("The changes could not be saved. Please try again.", "error")
        return render_member_form(db, "edit", values, {}, leader=leader)
    if new_photo != old_photo:
        delete_photo(old_photo)                                               # only after the commit
    flash(f"{name} was updated. The organizational structure shows the new details.", "success")
    return redirect(url_for("leaders_list"))


@app.route("/members/leaders/<int(max=2147483647):leader_id>/remove", methods=["POST"])
@permission_required("members.manage")
def leader_remove(leader_id):
    """Take a leader out of the organizational structure. The record is kept and can be restored."""
    db = get_db()
    leader = fetch_leader(db, leader_id)
    name = member_full_name(leader)
    db.execute("UPDATE org_leaders SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ? WHERE id = ?", (g.user["id"], leader_id))
    log_activity(db, "leader.removed", detail=f"{name} ({leader['position_name']})", actor_user_id=g.user["id"],
                 target_type="leader", target_id=leader_id)
    db.commit()
    flash(f"{name} was removed from the organizational structure. You can restore them below.", "success")
    return redirect(url_for("leaders_list"))


@app.route("/members/leaders/<int(max=2147483647):leader_id>/restore", methods=["POST"])
@permission_required("members.manage")
def leader_restore(leader_id):
    db = get_db()
    leader = fetch_leader(db, leader_id, removed=True)
    name = member_full_name(leader)
    db.execute("UPDATE org_leaders SET deleted_at = NULL, deleted_by = NULL WHERE id = ?", (leader_id,))
    log_activity(db, "leader.restored", detail=f"{name} ({leader['position_name']})", actor_user_id=g.user["id"],
                 target_type="leader", target_id=leader_id)
    db.commit()
    flash(f"{name} is back in the organizational structure.", "success")
    return redirect(url_for("leaders_list"))


@app.route("/members/leaders/<int(max=2147483647):leader_id>/photo")
@permission_required("members.view")
def leader_photo(leader_id):
    row = get_db().execute("SELECT photo_filename FROM org_leaders WHERE id = ?", (leader_id,)).fetchone()
    name = row["photo_filename"] if row else None
    if not name or not PHOTO_NAME_RE.match(name):
        abort(404)
    response = send_from_directory(app.config["MEMBER_PHOTO_DIR"], name, mimetype=PHOTO_MIME[name.rsplit(".", 1)[1]])
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@app.route("/members/<int(max=2147483647):member_id>")
@permission_required("members.view")
def member_detail(member_id):
    if member_id >= LEADER_ID_OFFSET:                       # a non-member leader: there is no member profile
        return redirect(url_for("leader_edit" if user_can("members.manage") else "leaders_list",
                                **({"leader_id": member_id - LEADER_ID_OFFSET} if user_can("members.manage") else {})))
    db = get_db()
    can_private, can_notes = user_can("members.private"), user_can("members.notes")
    member = fetch_member(db, member_id, can_private, can_notes)

    added_by = updated_by = ""
    if user_can("members.manage"):
        names = {row["id"]: row["full_name"] for row in db.execute(
            "SELECT id, full_name FROM users WHERE id IN (?, ?)", (member["created_by"], member["updated_by"]))}
        added_by, updated_by = names.get(member["created_by"], ""), names.get(member["updated_by"], "")

    profile = None                       # the member's own "About you" answers (private, like contact details)
    if can_private:
        own = db.execute("SELECT birthday, profile_completed_at FROM members WHERE id = ?", (member_id,)).fetchone()
        if own and own["profile_completed_at"]:
            born = parse_iso_date(own["birthday"])
            profile = {"full_name": member_full_name(member), "nickname": member["nickname"],
                       "birthday": own["birthday"], "age": age_on(own["birthday"]) if born else None,
                       "facebook_link": member["fb_link"], "reason": member["join_reason"]}

    account = credentials = None
    if user_can("members.credentials"):
        account = db.execute("SELECT username, password_set_at, must_change_password FROM users WHERE member_id = ?",
                             (member_id,)).fetchone()
        pending = session.get("new_credentials")
        if pending and pending.get("member_id") == member_id:
            credentials = session.pop("new_credentials")    # shown this one time only

    response = make_response(render_template(
        "member_detail.html", member=present_member(member), statuses=MEMBER_STATUSES,
        can_private=can_private, can_notes=can_notes, added_by=added_by, updated_by=updated_by,
        account=account, credentials=credentials, member_login_enabled=MEMBER_LOGIN_ENABLED, profile=profile,
        history=member_history(db, member_id) if user_can("members.manage") else []))
    if credentials:
        response.headers["Cache-Control"] = "no-store"      # never kept by the browser
    return response


@app.route("/members/<int(max=2147483647):member_id>/photo")
@permission_required("members.view")
def member_photo(member_id):
    if member_id >= LEADER_ID_OFFSET:                       # a non-member leader shown in the organizational tree
        return leader_photo(member_id - LEADER_ID_OFFSET)
    row = get_db().execute("SELECT photo_filename, deleted_at FROM members WHERE id = ?", (member_id,)).fetchone()
    if row and row["deleted_at"] and not user_can("members.archive"):
        abort(404)
    name = row["photo_filename"] if row else None
    if not name or not PHOTO_NAME_RE.match(name):
        abort(404)
    response = send_from_directory(app.config["MEMBER_PHOTO_DIR"], name,
                                   mimetype=PHOTO_MIME[name.rsplit(".", 1)[1]])
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@app.route("/members/<int(max=2147483647):member_id>/edit", methods=["GET", "POST"])
@permission_required("members.manage")
def member_edit(member_id):
    db = get_db()
    can_private, can_notes = user_can("members.private"), user_can("members.notes")
    member = fetch_member(db, member_id, can_private, can_notes)

    if request.method == "GET":
        values = {key: member[key] for key in (
            "first_name", "middle_name", "last_name", "date_joined", "status")}
        values.update(voice_section_id=str(member["voice_section_id"]), position_id=str(member["position_id"]),
                      email=member["email"] if can_private else "", phone=member["phone"] if can_private else "",
                      notes=member["notes"] if can_notes else "")
        return render_member_form(db, "edit", values, {}, member=member)

    values, clean, errors, photo = read_member_form(db, member, can_private, can_notes)
    remove_photo = bool(request.form.get("remove_photo"))
    if errors:
        flash("Please fix the highlighted fields and try again.", "error")
        return render_member_form(db, "edit", values, errors, member=member)

    changes = []
    if any(clean[key] != member[key] for key in ("first_name", "middle_name", "last_name")):
        changes.append("name")
    if can_private and (clean["email"] != member["email"] or clean["phone"] != member["phone"]):
        changes.append("contact details")
    if clean["voice_section_id"] != member["voice_section_id"]:
        changes.append("voice section")
    if clean["position_id"] != member["position_id"]:
        changes.append("choir position")
    if clean["date_joined"] != member["date_joined"]:
        changes.append("date joined")
    if can_notes and clean["notes"] != member["notes"]:
        changes.append("notes")
    photo_changed = bool(photo) or (remove_photo and bool(member["photo_filename"]))
    if photo_changed:
        changes.append("photo")
    status_changed = clean["status"] != member["status"]

    if not changes and not status_changed:
        flash("No changes to save.", "info")
        return redirect(url_for("member_detail", member_id=member_id))

    # Warn about a possible duplicate only when name, email or number actually changed.
    identity_changed = "name" in changes or "contact details" in changes
    if identity_changed:
        duplicates = find_possible_duplicates(db, clean, exclude_id=member_id)
        if duplicates and not request.form.get("confirm_duplicate"):
            return render_member_form(db, "edit", values, {}, member=member, duplicates=duplicates)

    old_photo = member["photo_filename"]
    new_photo = save_photo(*photo) if photo else (None if remove_photo else old_photo)
    name = member_full_name(clean)
    sets = {
        "first_name": clean["first_name"], "middle_name": clean["middle_name"], "last_name": clean["last_name"],
        "voice_section_id": clean["voice_section_id"], "position_id": clean["position_id"],
        "date_joined": clean["date_joined"], "status": clean["status"], "photo_filename": new_photo,
    }
    if can_private:
        sets.update(email=clean["email"], phone=clean["phone"])
    if can_notes:
        sets["notes"] = clean["notes"]
    try:
        # Column names come from the fixed dictionary above, never from the request.
        assignments = ", ".join(f"{column} = ?" for column in sets)
        db.execute(
            f"UPDATE members SET {assignments}, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
            (*sets.values(), g.user["id"], member_id),
        )
        if changes:
            log_activity(db, "member.updated", detail=f"{name} ({', '.join(changes)})",
                         actor_user_id=g.user["id"], target_type="member", target_id=member_id)
        if status_changed:
            log_status_change(db, member_id, name, member["status"], clean["status"])
        if "name" in changes:
            db.execute("UPDATE users SET full_name = ? WHERE member_id = ?", (name, member_id))
        db.commit()
    except sqlite3.Error:
        db.rollback()
        if photo:
            delete_photo(new_photo)
        flash("The changes could not be saved. Please try again.", "error")
        return render_member_form(db, "edit", values, {}, member=member)

    if photo_changed:
        delete_photo(old_photo)
    flash(f"{name} was updated.", "success")
    return redirect(url_for("member_detail", member_id=member_id))


# [MEMBERSHIP STATUS]: the Deactivate, Restore and Mark temporarily inactive buttons send their request here.
@app.route("/members/<int(max=2147483647):member_id>/status", methods=["POST"])
@permission_required("members.manage")
def member_set_status(member_id):
    new_status = request.form.get("status", "")
    if new_status not in MEMBER_STATUSES:
        abort(400, "That membership status is not valid.")
    db = get_db()
    member = fetch_member(db, member_id, False, False)
    name = member_full_name(member)
    if member["status"] == new_status:
        flash(f"{name} is already {MEMBER_STATUSES[new_status]}.", "info")
    else:
        db.execute("UPDATE members SET status = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
                   (new_status, g.user["id"], member_id))
        log_status_change(db, member_id, name, member["status"], new_status)
        db.commit()
        flash(f"{name} is now {MEMBER_STATUSES[new_status]}.", "success")
    return redirect(safe_members_url(request.form.get("return_to")))


# ------------------------------------------------------ member sign-in accounts --
#
# Each member gets a sign-in account in the users table:
#     username = the Member ID (JDC-0001)     role = 'member'     member_id = the member record
# The password is generated by the system (see generate_password) and shown to the
# administrator ONCE, so they can hand it to the member. Only a scrambled version
# (a hash) is stored, so it can never be looked up again: if it is lost, the
# administrator generates a new one. The 'member' role has almost no permissions,
# so a member account can never open the admin pages.

def generate_password():
    """A random password of PASSWORD_LENGTH characters: always letters AND numbers.

    Look-alike characters (0/O, 1/l/I) are left out so it can be read aloud or
    copied by hand without mistakes. Uses `secrets`, which is made for passwords.
    """
    alphabet = PASSWORD_LETTERS + PASSWORD_DIGITS
    while True:
        chars = [secrets.choice(alphabet) for _ in range(PASSWORD_LENGTH)]
        if any(c in PASSWORD_DIGITS for c in chars) and any(c in PASSWORD_LETTERS for c in chars):
            return "".join(chars)


def set_member_password(db, member_id, member_code, full_name):
    """Create the member's sign-in account, or give an existing one a NEW password.

    Returns (password, created). Does not commit: the caller commits together with the
    change it belongs to. The plain password is only returned here, never stored.
    """
    password = generate_password()
    password_hash = generate_password_hash(password)
    existing = db.execute("SELECT id FROM users WHERE member_id = ?", (member_id,)).fetchone()
    if existing:
        db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1, "
            "password_set_at = CURRENT_TIMESTAMP, full_name = ? WHERE id = ?",
            (password_hash, full_name, existing["id"]),
        )
        return password, False
    # users.email must be unique and not empty, but members may have no email (and a member
    # might share an email with an administrator). So member accounts get a reserved
    # placeholder that can never be used to sign in or receive mail (".invalid" is reserved).
    db.execute(
        "INSERT INTO users (full_name, email, password_hash, role, username, member_id, "
        "must_change_password, password_set_at) VALUES (?, ?, ?, 'member', ?, ?, 1, CURRENT_TIMESTAMP)",
        (full_name, f"{member_code.lower()}@members.invalid", password_hash, member_code, member_id),
    )
    return password, True


def remember_credentials(member_id, username, password):
    """Keep the new password just long enough to show it once on the next page."""
    session["new_credentials"] = {"member_id": member_id, "username": username, "password": password}


def member_sign_in_problem(db, member_id):
    """Why a member may not sign in right now, or None if they may."""
    row = db.execute("SELECT status, deleted_at FROM members WHERE id = ?", (member_id,)).fetchone()
    if row is None or row["deleted_at"]:
        return NO_ACCESS_MESSAGE                    # deleted members have no access, whatever else is set
    if not MEMBER_LOGIN_ENABLED:
        return "Member sign-in is not available yet. Please check with your choir administrator."
    if row["status"] == "inactive":
        return "This membership is not active. Please contact your choir administrator."
    return None


@app.route("/members/<int(max=2147483647):member_id>/login", methods=["POST"])
@permission_required("members.credentials")
def member_login_reset(member_id):
    """Create a member's login (if they have none) or generate a new password."""
    db = get_db()
    member = fetch_member(db, member_id, False, False)
    name = member_full_name(member)
    password, created = set_member_password(db, member_id, member["member_code"], name)
    log_activity(db, "member.login_created" if created else "member.password_reset", detail=name,
                 actor_user_id=g.user["id"], target_type="member", target_id=member_id)
    db.commit()
    remember_credentials(member_id, member["member_code"], password)
    flash(f"A password was generated for {name}." , "success")
    return redirect(url_for("member_detail", member_id=member_id))


# ------------------------------------------------- archive (deleted members) --
#
# "Delete" moves a member to the ARCHIVE. It does not erase anything:
#   * the member disappears from the roster, search, counts, duplicates-free lists and exports,
#   * their login stops working at once (even if they are signed in right now),
#   * their record, photo, history and login are KEPT, so an administrator can still see
#     everything, restore the member, or delete them permanently.
# Only "Delete permanently" (inside the archive, with a typed confirmation) erases data.

def member_history(db, member_id):
    """Everything recorded about one member, newest first (from the activity log)."""
    rows = db.execute(
        """
        SELECT l.action, l.detail, l.created_at, u.full_name AS actor
          FROM activity_log l
          LEFT JOIN users u ON u.id = l.actor_user_id
         WHERE l.target_type = 'member' AND l.target_id = ?
         ORDER BY l.created_at DESC, l.id DESC
        """,
        (member_id,),
    ).fetchall()
    return [
        {
            "label": ACTIVITY_LABELS.get(row["action"], row["action"].replace(".", " ").replace("_", " ").capitalize()),
            "detail": row["detail"],
            "actor": row["actor"] or "",
            "when": format_stamp(row["created_at"]),
        }
        for row in rows
    ]


@app.route("/members/<int(max=2147483647):member_id>/delete", methods=["POST"])
@permission_required("members.delete")
def member_delete(member_id):
    """Move a member to the archive (they can be restored later)."""
    db = get_db()
    member = fetch_member(db, member_id, False, False)       # 404 if missing or already archived
    name = member_full_name(member)
    db.execute("UPDATE members SET deleted_at = CURRENT_TIMESTAMP, deleted_by = ? WHERE id = ?",
               (g.user["id"], member_id))
    log_activity(db, "member.deleted", detail=name, actor_user_id=g.user["id"],
                 target_type="member", target_id=member_id)
    db.commit()
    flash(f"{name} was moved to the archive. You can restore them, or delete them permanently, from Archive.", "success")
    return redirect(url_for("members_list"))


@app.route("/members/archive")
@permission_required("members.archive")
def members_archive():
    db = get_db()
    q = collapse(request.args.get("q"))[:80]
    clauses, params = name_search_clauses(q, user_can("members.private"))
    where = "WHERE " + " AND ".join(["m.deleted_at IS NOT NULL"] + clauses)

    found = db.execute(f"SELECT COUNT(*) FROM members m {where}", params).fetchone()[0]
    total_archived = db.execute("SELECT COUNT(*) FROM members WHERE deleted_at IS NOT NULL").fetchone()[0]
    pages = max(1, -(-found // PAGE_SIZE))
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = min(max(page, 1), pages)

    rows = db.execute(
        f"SELECT {member_columns(False, False)}, du.full_name AS deleted_by_name "
        f"{MEMBER_JOINS} LEFT JOIN users du ON du.id = m.deleted_by {where} "
        "ORDER BY m.deleted_at DESC, m.id DESC LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()

    link = lambda n: url_for("members_archive", **{**({"q": q} if q else {}), "page": n})
    first_item = (page - 1) * PAGE_SIZE + 1 if found else 0
    return render_template(
        "members_archive.html",
        members=[present_member(row) | {"deleted_by_name": row["deleted_by_name"] or ""} for row in rows],
        q=q, found=found, total_archived=total_archived,
        first_item=first_item, last_item=first_item + len(rows) - 1 if found else 0,
        pages=pages, pager=build_pager(page, pages, link),
        prev_url=link(page - 1) if page > 1 else None,
        next_url=link(page + 1) if page < pages else None,
    )


@app.route("/members/archive/<int(max=2147483647):member_id>")
@permission_required("members.archive")
def member_archive_detail(member_id):
    """Everything about a deleted member: details, when added/deleted, and their full history."""
    db = get_db()
    can_private, can_notes = user_can("members.private"), user_can("members.notes")
    member = fetch_member(db, member_id, can_private, can_notes, scope="archived")
    ids = {member["created_by"], member["updated_by"], member["deleted_by"]} - {None}
    names = {}
    if ids:
        marks = ", ".join("?" for _ in ids)
        names = {row["id"]: row["full_name"] for row in db.execute(
            f"SELECT id, full_name FROM users WHERE id IN ({marks})", tuple(ids))}
    account = db.execute("SELECT username, password_set_at FROM users WHERE member_id = ?", (member_id,)).fetchone()
    return render_template(
        "member_archive_detail.html", member=present_member(member), statuses=MEMBER_STATUSES,
        can_private=can_private, can_notes=can_notes, account=account,
        added_by=names.get(member["created_by"], ""), updated_by=names.get(member["updated_by"], ""),
        deleted_by=names.get(member["deleted_by"], ""), history=member_history(db, member_id),
    )


@app.route("/members/archive/<int(max=2147483647):member_id>/restore", methods=["POST"])
@permission_required("members.archive")
def member_restore_archived(member_id):
    """Bring an archived member back. Their status and login work again as before."""
    db = get_db()
    member = fetch_member(db, member_id, False, False, scope="archived")
    name = member_full_name(member)
    db.execute("UPDATE members SET deleted_at = NULL, deleted_by = NULL WHERE id = ?", (member_id,))
    log_activity(db, "member.undeleted", detail=name, actor_user_id=g.user["id"],
                 target_type="member", target_id=member_id)
    db.commit()
    flash(f"{name} was restored from the archive.", "success")
    return redirect(url_for("member_detail", member_id=member_id))


@app.route("/members/archive/<int(max=2147483647):member_id>/purge", methods=["POST"])
@permission_required("members.purge")
def member_purge(member_id):
    """Erase an ARCHIVED member for good: record, photo and login. Cannot be undone.

    Safeguards: only archived members can be erased, and the Member ID must be typed.
    What is kept: a record that it happened (who, when, which Member ID). Earlier history
    entries keep their dates and the administrator who acted, but the member's name is
    replaced by the Member ID. Member IDs are never handed out again.
    """
    db = get_db()
    member = fetch_member(db, member_id, False, False, scope="archived")
    code = member["member_code"]
    if request.form.get("confirm_code", "").strip().upper() != code:
        flash(f"To delete permanently, type the Member ID ({code}) exactly. Nothing was deleted.", "error")
        return redirect(url_for("member_archive_detail", member_id=member_id) + "#purge")

    photo = db.execute("SELECT photo_filename FROM members WHERE id = ?", (member_id,)).fetchone()["photo_filename"]
    try:
        db.execute("UPDATE activity_log SET detail = ? WHERE target_type = 'member' AND target_id = ?",
                   (code, member_id))
        db.execute("UPDATE announcements SET author_name = ? WHERE author_user_id IN "
                   "(SELECT id FROM users WHERE member_id = ?)", (code, member_id))   # keep the post, drop the name
        db.execute("UPDATE choir_files SET uploader_name = ? WHERE uploaded_by IN "
                   "(SELECT id FROM users WHERE member_id = ?)", (code, member_id))
        db.execute("UPDATE music_files SET uploader_name = ? WHERE uploaded_by IN "
                   "(SELECT id FROM users WHERE member_id = ?)", (code, member_id))
        db.execute("DELETE FROM tasks WHERE member_id = ?", (member_id,))
        db.execute("DELETE FROM attendance_records WHERE member_id = ?", (member_id,))
        db.execute("DELETE FROM users WHERE member_id = ?", (member_id,))     # their login
        db.execute("DELETE FROM members WHERE id = ?", (member_id,))
        log_activity(db, "member.purged", detail=code, actor_user_id=g.user["id"],
                     target_type="member", target_id=member_id)
        db.commit()
    except sqlite3.Error:
        db.rollback()
        flash("The member could not be deleted. Nothing was changed.", "error")
        return redirect(url_for("member_archive_detail", member_id=member_id))
    delete_photo(photo)                                                       # only after the database commit
    flash(f"{code} was permanently deleted.", "success")
    return redirect(url_for("members_archive"))


# ------------------------------------------------------ manage voice sections / positions --
# [MANAGE CHOICES] Add, rename, retire and bring back voice sections and choir positions from the
# website, so nobody needs the command line. Retired choices are hidden from the forms but members who
# have them keep them. Only a choice nobody uses can be removed. Membership STATUSES are not managed
# here: they are fixed in the code.
#
# A choir position named in POSITION_CAPABILITIES (President, Choir Director, Internal Secretary...) or
# called "Member" cannot be renamed here: those names are used by the code to decide what the position
# may do, so renaming one would silently take its rights away.

CHOICE_KINDS = {
    "voice": {"table": "voice_sections", "noun": "voice section", "code": "voice_section",
              "member_column": "voice_section_id"},
    "position": {"table": "choir_positions", "noun": "choir position", "code": "choir_position",
                 "member_column": "position_id"},
}
CHOICE_NAME_MAX = 40


def protected_position_names():
    return {name.lower() for name in POSITION_CAPABILITIES} | {"member"}


def choice_rows(db, kind):
    # Table and column names come from the fixed CHOICE_KINDS list, never from the request.
    return db.execute(
        f"SELECT c.id, c.name, c.is_active, "
        f"(SELECT COUNT(*) FROM members m WHERE m.{kind['member_column']} = c.id)"
        + (" + (SELECT COUNT(*) FROM org_leaders l WHERE l.position_id = c.id)" if kind["table"] == "choir_positions" else "")
        + " AS people "
        f"FROM {kind['table']} c ORDER BY c.is_active DESC, c.sort_order, c.name"
    ).fetchall()


@app.route("/members/choices", methods=["GET", "POST"])
@permission_required("members.choices")
def members_choices():
    db = get_db()
    if request.method == "GET":
        positions = [{**dict(r), "locked": r["name"].lower() in protected_position_names()}
                     for r in choice_rows(db, CHOICE_KINDS["position"])]
        voices = [{**dict(r), "locked": False} for r in choice_rows(db, CHOICE_KINDS["voice"])]
        return render_template("choices.html", voices=voices, positions=positions, name_max=CHOICE_NAME_MAX)

    wants_json = request.headers.get("X-Requested-With") == "fetch"
    kind = CHOICE_KINDS.get(request.form.get("kind", ""))
    action = request.form.get("action", "")

    def done(ok, message, **extra):
        if wants_json:
            return {"ok": ok, "message": message, **extra}, (200 if ok else 400)
        flash(message, "success" if ok else "error")
        return redirect(url_for("members_choices"))

    if kind is None or action not in ("add", "rename", "retire", "restore", "remove"):
        return done(False, "That change was not understood. Nothing was changed.")
    table, noun, code = kind["table"], kind["noun"], kind["code"]

    name = collapse(request.form.get("name"))
    if action in ("add", "rename"):
        if not name:
            return done(False, f"Type a name for the {noun}.")
        if len(name) > CHOICE_NAME_MAX:
            return done(False, f"The name must be {CHOICE_NAME_MAX} characters or fewer.")
        if not has_letters_ok(name):
            return done(False, "The name must contain letters.")

    try:
        if action == "add":
            order = db.execute(f"SELECT COALESCE(MAX(sort_order), 0) + 1 FROM {table}").fetchone()[0]
            cursor = db.execute(f"INSERT INTO {table} (name, sort_order) VALUES (?, ?)", (name, order))
            log_activity(db, f"{code}.added", detail=name, actor_user_id=g.user["id"], target_type=code,
                         target_id=cursor.lastrowid)
            db.commit()
            return done(True, f"Added the {noun} {name}.", id=cursor.lastrowid, name=name)

        raw_id = request.form.get("id", "")
        row = None
        if re.fullmatch(r"[0-9]{1,9}", raw_id):
            row = next((r for r in choice_rows(db, kind) if r["id"] == int(raw_id)), None)
        if row is None:
            return done(False, f"That {noun} was not found. Nothing was changed.")

        if action == "rename":
            if kind is CHOICE_KINDS["position"]:
                if row["name"].lower() in protected_position_names():
                    return done(False, f"{row['name']} has a special meaning in the member area, so it cannot be renamed here.")
                if name.lower() in protected_position_names():
                    return done(False, f"{name} has a special meaning in the member area, so it cannot be used as a new name.")
            db.execute(f"UPDATE {table} SET name = ? WHERE id = ?", (name, row["id"]))
            log_activity(db, f"{code}.renamed", detail=f"{row['name']} to {name}", actor_user_id=g.user["id"],
                         target_type=code, target_id=row["id"])
            message = f"Renamed {row['name']} to {name}. Members who have it now show the new name."
        elif action == "retire":
            if row["is_active"] and sum(1 for r in choice_rows(db, kind) if r["is_active"]) <= 1:
                return done(False, f"You need at least one active {noun}, so the last one cannot be retired.")
            db.execute(f"UPDATE {table} SET is_active = 0 WHERE id = ?", (row["id"],))
            log_activity(db, f"{code}.retired", detail=row["name"], actor_user_id=g.user["id"],
                         target_type=code, target_id=row["id"])
            message = f"Retired {row['name']}. It is hidden from the forms; members who have it keep it."
        elif action == "restore":
            db.execute(f"UPDATE {table} SET is_active = 1 WHERE id = ?", (row["id"],))
            log_activity(db, f"{code}.restored", detail=row["name"], actor_user_id=g.user["id"],
                         target_type=code, target_id=row["id"])
            message = f"Brought back {row['name']}."
        else:  # remove
            if row["people"]:
                return done(False, f"{row['name']} is used by {row['people']} member(s), so it can only be retired.")
            if kind is CHOICE_KINDS["voice"] and db.execute(
                    "SELECT 1 FROM announcements WHERE audience_voice_id = ?", (row["id"],)).fetchone():
                return done(False, f"{row['name']} is used by an announcement, so it can only be retired.")
            db.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
            log_activity(db, f"{code}.removed", detail=row["name"], actor_user_id=g.user["id"], target_type=code)
            message = f"Removed {row['name']}."
        db.commit()
        return done(True, message)
    except sqlite3.IntegrityError:
        db.rollback()
        return done(False, f"A {noun} with that name already exists. Nothing was changed.")
    except sqlite3.Error:
        db.rollback()
        return done(False, "That could not be saved. Nothing was changed.")


# ------------------------------------------------------------- reset roster --
# [RESET ROSTER] Wipes EVERY choir member (active, inactive and archived) and everything that belongs to
# them: their logins, photos, history, tasks and attendance records. Member IDs restart at JDC-0001.
# Meant for the end of pilot testing. It never touches administrator/viewer accounts, voice sections,
# choir positions, announcements or the attendance sessions themselves.
#
# Why Member IDs did not restart before: the members table uses AUTOINCREMENT, so SQLite keeps a
# separate counter (table sqlite_sequence) that only goes up. Deleting rows does not lower it.
# The reset deletes that counter row, so the next member gets id 1 -> JDC-0001.
#
# Safeguards: administrators only (members.reset), a separate confirmation page that shows what
# will be erased, the exact phrase typed, the administrator's own password, an automatic backup
# of the database first, and everything happens in ONE transaction (all or nothing).

RESET_PHRASE = "RESET ROSTER"
BACKUP_KEEP = 10                       # how many reset backups to keep in instance/backups


def roster_counts(db):
    """What a reset would erase, for the confirmation page."""
    one = lambda sql: db.execute(sql).fetchone()[0]
    return {
        "total": one("SELECT COUNT(*) FROM members"),
        "active": one("SELECT COUNT(*) FROM members WHERE deleted_at IS NULL"),
        "archived": one("SELECT COUNT(*) FROM members WHERE deleted_at IS NOT NULL"),
        "logins": one("SELECT COUNT(*) FROM users WHERE member_id IS NOT NULL"),
        "photos": one("SELECT COUNT(*) FROM members WHERE photo_filename IS NOT NULL"),
        "history": one("SELECT COUNT(*) FROM activity_log WHERE target_type IN ('member', 'members')"),
        "tasks": one("SELECT COUNT(*) FROM tasks") if table_columns(db, "tasks") else 0,
        "attendance": one("SELECT COUNT(*) FROM attendance_records") if table_columns(db, "attendance_records") else 0,
    }


def backup_database_before_reset(label="roster-reset"):
    """Copy the whole database to instance/backups and return the file name."""
    folder = Path(app.config["DATABASE"]).parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"before-{label}-{datetime.now():%Y%m%d-%H%M%S}.db"
    source = sqlite3.connect(app.config["DATABASE"])
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)          # safe, consistent copy even while the app is running
    finally:
        destination.close()
        source.close()
    for old in sorted(folder.glob(f"before-{label}-*.db"))[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return target.name


@app.route("/members/reset", methods=["GET", "POST"])
@permission_required("members.reset")
def roster_reset():
    db = get_db()
    counts = roster_counts(db)

    if request.method == "GET":
        return render_template("roster_reset.html", counts=counts, phrase=RESET_PHRASE)

    # --- POST: every check must pass, otherwise NOTHING is changed ---------------------
    if request.form.get("confirm_phrase", "").strip() != RESET_PHRASE:
        flash(f"To reset the roster, type {RESET_PHRASE} exactly. Nothing was changed.", "error")
        return redirect(url_for("roster_reset"))
    me = db.execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if not me or not check_password_hash(me["password_hash"], request.form.get("password", "")):
        flash("Your password was not correct. Nothing was changed.", "error")
        return redirect(url_for("roster_reset"))

    try:
        backup_name = backup_database_before_reset()
    except (sqlite3.Error, OSError):
        flash("A safety backup could not be made, so nothing was changed.", "error")
        return redirect(url_for("roster_reset"))

    photos = [row["photo_filename"] for row in
              db.execute("SELECT photo_filename FROM members WHERE photo_filename IS NOT NULL")]
    try:
        # Members' tasks and attendance records point at members, so they go first.
        db.execute("DELETE FROM tasks")
        db.execute("DELETE FROM attendance_records")
        # Posts written by members keep the announcement but lose the member's name.
        db.execute("UPDATE announcements SET author_name = 'Former member' WHERE author_user_id IN "
                   "(SELECT id FROM users WHERE member_id IS NOT NULL)")
        db.execute("UPDATE choir_files SET uploader_name = 'Former member' WHERE uploaded_by IN "
                   "(SELECT id FROM users WHERE member_id IS NOT NULL)")
        db.execute("UPDATE music_files SET uploader_name = 'Former member' WHERE uploaded_by IN "
                   "(SELECT id FROM users WHERE member_id IS NOT NULL)")
        db.execute("DELETE FROM login_attempts WHERE identifier LIKE 'jdc-%'")
        db.execute("DELETE FROM users WHERE member_id IS NOT NULL")          # member logins only
        # Old history is removed too. Otherwise the next JDC-0001 would inherit the old JDC-0001's history.
        db.execute("DELETE FROM activity_log WHERE target_type IN ('member', 'members')")
        db.execute("DELETE FROM members")
        db.execute("DELETE FROM sqlite_sequence WHERE name = 'members'")     # restart numbering at 1
        log_activity(db, "roster.reset",
                     detail=f"{counts['total']} member(s) cleared; backup {backup_name}",
                     actor_user_id=g.user["id"], target_type="roster")
        db.commit()
    except sqlite3.Error:
        db.rollback()
        flash("The roster could not be reset. Nothing was changed.", "error")
        return redirect(url_for("roster_reset"))
    for name in photos:                                                      # only after the commit
        delete_photo(name)
    flash(f"The roster was reset. The next member will be JDC-0001. A backup was saved as {backup_name} "
          "in the instance/backups folder.", "success")
    return redirect(url_for("members_list"))


def csv_safe(value):
    """Stop spreadsheet programs from running text that starts like a formula."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return text if re.fullmatch(r"\+?[0-9]+", text) else "'" + text
    return text


@app.route("/members/export", methods=["POST"])
@permission_required("members.export")
def members_export():
    db = get_db()
    where, params, _filters = member_filters(db, request.form, True)
    rows = db.execute(
        f"SELECT {member_columns(True, False)} {MEMBER_JOINS} {where} {ORDER_BY_NAME}", params
    ).fetchall()

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Member ID", "First name", "Middle name", "Last name", "Email", "Contact number",
                     "Voice section", "Choir position", "Date joined", "Status"])
    for row in rows:
        # Contact numbers are written as ="0917..." so Excel keeps the leading zero.
        phone = f'="{row["phone"]}"' if PHONE_RE.match(row["phone"] or "") else ""
        writer.writerow([csv_safe(row["member_code"]), csv_safe(row["first_name"]), csv_safe(row["middle_name"]),
                         csv_safe(row["last_name"]), csv_safe(row["email"]), phone,
                         csv_safe(row["voice_name"]), csv_safe(row["position_name"]),
                         row["date_joined"], MEMBER_STATUSES.get(row["status"], row["status"])])

    log_activity(db, "members.exported", detail=f"{len(rows)} member{'s' if len(rows) != 1 else ''}",
                 actor_user_id=g.user["id"], target_type="members")
    db.commit()
    filename = f"jubilate-deo-members-{date.today():%Y-%m-%d}.csv"
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------- member area --
#
# The choir MEMBERS' own interface (sign in with Member ID + password). It is separate from the
# administrator pages above. Rules that hold for everything below:
#   * A member can only ever see their own attendance and tasks.
#   * What a member may DO beyond that depends on their CHOIR POSITION (see POSITION_CAPABILITIES),
#     but only inside this member area. A position never opens an administrator page.
#   * Every route checks that on the SERVER (member_area_required), not just by hiding buttons.

# [ACTION CENTER] The permissions an administrator can switch on or off for ONE member.
#   effective permission = the member's override (member_permissions table), if there is one;
#                          otherwise the default below (choir position + the built-in rules).
# Defaults reproduce exactly how the app behaved before the Action Center existed: everybody sees every
# panel, a member manages files only in their OWN voice section, and positions keep POSITION_CAPABILITIES.
# Keys that end in ":<voice section id>" are per voice section: vfile.view:3, vfile.upload:3, vfile.edit:3, vfile.delete:3.
PANEL_PERMISSIONS = (
    ("panel.directory", "Choir Member Directory", "View member profiles"),
    ("panel.general_files", "General Choir Files", "View and download shared files"),
    ("panel.voice_files", "Voice Sections", "View and download voice section files"),
    ("panel.schedule", "Schedule", "View the schedule and calendar"),
    ("panel.announcements", "Announcements", "See announcements on the dashboard"),
    ("panel.reports", "Reports", "See the treasurer's report"),
    ("panel.music", "Music Library", "View, preview, download and save music files offline"),
)
ACTION_PERMISSIONS = (
    ("announcements.post", "Post announcements", "Create announcements for everyone or a voice section"),
    ("schedule.manage", "Manage schedules", "Add, edit and cancel schedules"),
    ("tasks.manage", "Manage tasks", "Assign tasks and tick them as done"),
    ("attendance.manage", "Take attendance", "Record attendance for activities"),
    ("finance.post", "Post treasurer's report", "Publish the report shown on every dashboard"),
    ("gfile.upload", "Upload general files", "Add files to General Choir Files"),
    ("gfile.edit", "Edit general files", "Rename, describe or replace general files"),
    ("gfile.delete", "Delete general files", "Remove files from General Choir Files"),
    ("music.upload", "Upload music files", "Add files to the Music Library"),
    ("music.edit", "Edit music files", "Change a music file's details or replace the file"),
    ("music.archive", "Archive music files", "Archive and restore music files"),
)
VOICE_FILE_PERMISSIONS = (("view", "View"), ("upload", "Upload"), ("edit", "Edit"), ("delete", "Delete"))
PANEL_KEYS = {key for key, _label, _hint in PANEL_PERMISSIONS}
GENERAL_FILE_KEYS = ("gfile.upload", "gfile.edit", "gfile.delete")
MUSIC_ACTION_KEYS = ("music.upload", "music.edit", "music.archive")


def position_capabilities(member):
    """What the member's choir position gives by default (POSITION_CAPABILITIES, case-insensitive)."""
    table = {name.lower(): caps for name, caps in POSITION_CAPABILITIES.items()}
    return table.get((member["position_name"] or "").lower(), set())


def permission_baseline(member, key):
    """The default for one permission key, before any Action Center override."""
    if key in PANEL_KEYS:
        return True
    if key in GENERAL_FILE_KEYS or key in MUSIC_ACTION_KEYS:      # officers who manage General Choir Files also manage the Music Library
        return "files.general" in position_capabilities(member)
    if key.startswith("vfile."):
        action, _sep, voice_id = key.partition(":")
        if action == "vfile.view":
            return True
        return voice_id == str(member["voice_section_id"])       # upload / edit / delete: own section only
    return key in position_capabilities(member)


def member_overrides(db, member_id):
    return {row["permission"]: bool(row["granted"]) for row in db.execute(
        "SELECT permission, granted FROM member_permissions WHERE member_id = ?", (member_id,))}


def member_can(capability):
    """True if the signed-in MEMBER may do this in the member area (override first, then the default)."""
    me = getattr(g, "member", None)
    if me is None:
        return False
    overrides = getattr(g, "_perm_overrides", None)
    if overrides is None:
        overrides = g._perm_overrides = member_overrides(get_db(), me["id"])

    def effective(key):
        return overrides[key] if key in overrides else permission_baseline(me, key)

    if capability == "files.general":                  # the old name: any general-file right
        return any(effective(key) for key in GENERAL_FILE_KEYS)
    return effective(capability)


def member_area_required(capability=None):
    """Protect a member-area route. Not signed in -> the member sign-in page; no right -> 403."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                flash("Sign in to continue.", "info")
                return redirect(url_for("login"))
            if not user_can("member.home") or getattr(g, "member", None) is None:
                abort(403)
            if capability and not member_can(capability):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def parse_iso_date(value):
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").date()
    except ValueError:
        return None


# ----------------------------------------------------------- signing in (both pages) --

def sign_in(user):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]


def login_blocked(db, key, ip):
    """True when there were too many wrong passwords recently for this name or this computer."""
    window = f"-{LOGIN_WINDOW_MINUTES} minutes"
    by_name = db.execute("SELECT COUNT(*) FROM login_attempts WHERE identifier = ? "
                         "AND attempted_at > datetime('now', ?)", (key, window)).fetchone()[0]
    by_ip = db.execute("SELECT COUNT(*) FROM login_attempts WHERE ip = ? "
                       "AND attempted_at > datetime('now', ?)", (ip, window)).fetchone()[0]
    return by_name >= MAX_FAILED_LOGINS or by_ip >= MAX_FAILED_PER_IP


def record_failed_login(db, key, ip):
    db.execute("INSERT INTO login_attempts (identifier, ip) VALUES (?, ?)", (key[:120], ip[:64]))
    db.execute("DELETE FROM login_attempts WHERE attempted_at < datetime('now', '-1 day')")
    db.commit()


def authenticate(db, identifier, password, members_only=False):
    """Check a sign-in. Returns (user, None) on success or (None, message to show).

    members_only=True is the member page: only Member IDs work there, never email addresses.
    """
    identifier = " ".join((identifier or "").split())[:120]
    key = identifier.lower()
    ip = request.remote_addr or ""
    generic = MEMBER_LOGIN_ERROR if members_only else GENERIC_LOGIN_ERROR
    if login_blocked(db, key, ip):
        return None, TOO_MANY_ATTEMPTS_MESSAGE

    code = identifier.upper()
    user = None
    if MEMBER_CODE_RE.fullmatch(code):
        user = db.execute("SELECT * FROM users WHERE username = ?", (code,)).fetchone()
    elif not members_only:
        # An email address: staff accounts only. Members sign in with their Member ID.
        user = db.execute("SELECT * FROM users WHERE email = ? AND member_id IS NULL", (key,)).fetchone()

    if user and check_password_hash(user["password_hash"], password or ""):
        problem = member_sign_in_problem(db, user["member_id"]) if user["member_id"] is not None else None
        if problem is None and home_endpoint(user["role"]) is None:
            problem = "This account does not have access to anything yet. Please contact your choir administrator."
        if problem:
            return None, problem
        db.execute("DELETE FROM login_attempts WHERE identifier = ?", (key,))
        db.commit()
        return user, None

    if user is None and MEMBER_CODE_RE.fullmatch(code) and db.execute(
            "SELECT 1 FROM activity_log WHERE action = 'member.purged' AND detail = ?", (code,)).fetchone():
        return None, NO_ACCESS_MESSAGE          # this Member ID belonged to a permanently deleted member
    record_failed_login(db, key, ip)
    return None, generic


@app.route("/member")
def member_login():
    """Old members' address. There is ONE sign-in page for everyone now: /login.

    Administrators sign in with their email, choir members with their Member ID. Which area opens
    (the administrator interface or the member area) depends on the account's role in the database.
    """
    return redirect(url_for("login"))


@app.route("/member-area/password", methods=["GET", "POST"])
@member_area_required()
def member_password():
    """Choose your own password. Required at the first sign-in (the given one is temporary)."""
    if not MEMBER_PASSWORD_CHANGE_ENABLED:
        flash("Changing your password is not available yet. Use the password your administrator gave you.", "info")
        return redirect(url_for("member_home"))
    db = get_db()
    forced = bool(g.user["must_change_password"])
    errors = {}
    if request.method == "POST":
        row = db.execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not check_password_hash(row["password_hash"], current):
            errors["current_password"] = "That is not your current password."
        if len(new) < MIN_NEW_PASSWORD_LENGTH:
            errors["new_password"] = f"Use at least {MIN_NEW_PASSWORD_LENGTH} characters."
        elif not (any(c.isalpha() for c in new) and any(c.isdigit() for c in new)):
            errors["new_password"] = "Use a mix of letters and numbers."
        elif new == current:
            errors["new_password"] = "Choose a different password from the one you were given."
        elif new.lower() == g.member["member_code"].lower():
            errors["new_password"] = "Your password cannot be your Member ID."
        if "new_password" not in errors and new != confirm:
            errors["confirm_password"] = "The two passwords do not match."
        if not errors:
            db.execute("UPDATE users SET password_hash = ?, must_change_password = 0, "
                       "password_set_at = CURRENT_TIMESTAMP WHERE id = ?", (generate_password_hash(new), g.user["id"]))
            log_activity(db, "member.password_changed", detail=member_full_name(g.member),
                         actor_user_id=g.user["id"], target_type="member", target_id=g.member["id"])
            db.commit()
            flash("Your new password is saved.", "success")
            return redirect(url_for("member_home"))
    return render_template("member_password.html", forced=forced, errors=errors, minimal=forced,
                           min_length=MIN_NEW_PASSWORD_LENGTH)


# ---- first sign-in: the "About you" form --------------------------------------------------

FB_HOSTS = ("facebook.com", "fb.com", "fb.me")     # Facebook links must point to one of these
JOIN_REASON_MIN, JOIN_REASON_MAX = 10, 1000


def profile_incomplete(member):
    """True while the member still has to fill in the "About you" form (also when the birthday is missing)."""
    return not member["profile_completed_at"] or not member["birthday"]


def age_on(birthday, today=None):
    """Age in whole years for a 'YYYY-MM-DD' birthday (None if it is not a valid date)."""
    born, today = parse_iso_date(birthday), today or date.today()
    if born is None:
        return None
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def read_profile_form():
    """Check the submitted form. Returns (values, errors); values are cleaned and ready to save."""
    form, errors = request.form, {}
    v = {k: collapse(form.get(k)) for k in ("first_name", "middle_name", "last_name", "nickname", "email", "fb_link")}
    v["birthday"] = form.get("birthday", "").strip()
    v["phone"] = re.sub(r"[\s\-().]", "", form.get("phone", ""))             # "0917 123-4567" -> "09171234567"
    v["join_reason"] = form.get("join_reason", "").replace("\r\n", "\n").strip()

    check_name(errors, "first_name", "First name", v["first_name"], True)
    check_name(errors, "middle_name", "Middle name", v["middle_name"], False)
    check_name(errors, "last_name", "Last name", v["last_name"], True)
    check_name(errors, "nickname", "Nickname", v["nickname"], True)

    age = age_on(v["birthday"])                          # the age is always worked out here, never trusted from the form
    if age is None:
        errors["birthday"] = "Enter your birthday."
    elif age < 5 or age > 110:
        errors["birthday"] = "Please check your birthday."

    v["email"] = v["email"].lower()
    if not EMAIL_RE.match(v["email"]) or len(v["email"]) > 254:
        errors["email"] = "Enter a valid email address."

    if not PHONE_RE.match(v["phone"]):
        errors["phone"] = "Enter a valid contact number (7 to 15 digits, for example 09171234567)."

    link = v["fb_link"]
    if link and "://" not in link:
        link = "https://" + link                                               # allow "facebook.com/juan"
    parts = urlparse(link)
    host = (parts.hostname or "").lower()
    if (parts.scheme not in ("http", "https") or len(link) > 300
            or not any(host == h or host.endswith("." + h) for h in FB_HOSTS)
            or len(parts.path.strip("/")) == 0 and not parts.query):
        errors["fb_link"] = "Enter the link to your Facebook profile (for example facebook.com/yourname)."
    v["fb_link"] = link

    reason = v["join_reason"]
    if len(reason) < JOIN_REASON_MIN:
        errors["join_reason"] = f"Tell us a little more (at least {JOIN_REASON_MIN} characters)."
    elif len(reason) > JOIN_REASON_MAX:
        errors["join_reason"] = f"Please keep it to {JOIN_REASON_MAX} characters or fewer."
    elif has_control_chars(reason, allow_newlines=True):
        errors["join_reason"] = "Your answer contains characters that are not allowed."
    return v, errors


def profile_form_view(editing):
    """The "About you" form. editing=False: first sign-in, the member cannot go further until it is saved.
    editing=True: the same form opened later from the profile menu (top right) to update the answers."""
    db = get_db()
    existing = db.execute("SELECT first_name, middle_name, last_name, nickname, birthday, email, phone, fb_link, join_reason "
                          "FROM members WHERE id = ?", (g.member["id"],)).fetchone()
    values, errors = dict(existing), {}                       # name is pre-filled from what the admin typed
    if request.method == "POST":
        values, errors = read_profile_form()
        if errors:
            flash("Please check the highlighted fields.", "error")
        else:
            db.execute(
                "UPDATE members SET first_name = ?, middle_name = ?, last_name = ?, nickname = ?, birthday = ?, email = ?, phone = ?, "
                "fb_link = ?, join_reason = ?, profile_completed_at = COALESCE(profile_completed_at, CURRENT_TIMESTAMP), "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (values["first_name"], values["middle_name"], values["last_name"], values["nickname"], values["birthday"],
                 values["email"], values["phone"], values["fb_link"], values["join_reason"], g.member["id"]))
            db.execute("UPDATE users SET full_name = ? WHERE member_id = ?", (member_full_name(values), g.member["id"]))
            log_activity(db, "member.profile_updated" if editing else "member.profile_completed",
                         detail=" ".join((values["first_name"], values["last_name"])),
                         actor_user_id=g.user["id"], target_type="member", target_id=g.member["id"])
            db.commit()
            flash("Your details are updated." if editing else "Thank you! Your details are saved. Welcome to Jubilate Deo Chorale.",
                  "success")
            return redirect(url_for("member_home"))
    return render_template("member_welcome.html", values=values, errors=errors, minimal=not editing, editing=editing,
                           today_iso=date.today().isoformat(), reason_min=JOIN_REASON_MIN, reason_max=JOIN_REASON_MAX)


@app.route("/member-area/welcome", methods=["GET", "POST"])
@member_area_required()
def member_profile_setup():
    """Shown once, right after the first sign-in. The member cannot open the dashboard until it is saved."""
    return profile_form_view(editing=not profile_incomplete(g.member))


@app.route("/member-area/profile", methods=["GET", "POST"])
@member_area_required()
def member_profile():
    """"Edit my information" in the profile menu: the same form, for updating the answers later."""
    if profile_incomplete(g.member):
        return redirect(url_for("member_profile_setup"))
    return profile_form_view(editing=True)


@app.route("/member-area/photo")
@member_area_required()
def member_my_photo():
    name = g.member["photo_filename"]
    if not name or not PHOTO_NAME_RE.match(name):
        abort(404)
    response = send_from_directory(app.config["MEMBER_PHOTO_DIR"], name, mimetype=PHOTO_MIME[name.rsplit(".", 1)[1]])
    response.headers["Cache-Control"] = "private, no-cache"
    return response


# ------------------------------------------------------- Members page: directory and choir files --
# [MEMBERS PAGE] What a signed-in member sees under "Members" in the member area:
#   Organization Structure (locked, coming soon) / Choir Members Profile (read-only) /
#   General Choir Files / one panel per voice section.
#
# FILE RULES (all enforced here on the server, the buttons are only hidden for tidiness):
#   * every member can VIEW and DOWNLOAD every file, in every voice section
#   * a member can UPLOAD, EDIT and REMOVE files only in THEIR OWN voice section
#   * General Choir Files can only be changed by positions with "files.general" (POSITION_CAPABILITIES)
# "Available offline" is done in the browser (static/js/app.js and static/js/sw.js), it needs https.

VOICE_PANEL_ORDER = ["soprano", "tenor", "alto", "bass"]        # the order of the four voice panels
FILE_EXTENSIONS = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "png", "jpg", "jpeg", "webp",
                   "mp3", "m4a", "wav"}
FILE_TITLE_MAX = 120
FILE_DESC_MAX = 300
STORED_FILE_RE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{2,4}$")


def format_size(size):
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def file_extension(name):
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def can_view_files(voice_id):
    """May the signed-in member see and download files in this place? (None = General Choir Files)"""
    if voice_id is None:
        return member_can("panel.general_files")
    return member_can("panel.voice_files") and member_can(f"vfile.view:{voice_id}")


def can_file_action(voice_id, action):
    """May the signed-in member upload / edit / delete files here? action is 'upload', 'edit' or 'delete'.
    Needs the right itself (default: own voice section only, General Files by position) AND permission to view the place.
    Both come from the Action Center overrides first, then the defaults (see member_can)."""
    if not can_view_files(voice_id):
        return False
    return member_can(f"gfile.{action}" if voice_id is None else f"vfile.{action}:{voice_id}")


def present_file(row):
    return {**dict(row), "ext": file_extension(row["original_name"]).upper(), "size_label": format_size(row["size_bytes"]),
            "when": format_stamp(row["updated_at"]),
            "can_modify": can_file_action(row["voice_section_id"], "edit"),
            "can_delete": can_file_action(row["voice_section_id"], "delete"),
            "can_edit": can_file_action(row["voice_section_id"], "edit") or can_file_action(row["voice_section_id"], "delete"),
            "url": url_for("choir_file_download", file_id=row["id"], v=row["version"])}


def fetch_choir_file(db, file_id):
    row = db.execute("SELECT * FROM choir_files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def read_file_form(require_file):
    """Check an upload / edit form. Returns (title, description, data, ext, original_name, error)."""
    title = collapse(request.form.get("title"))
    description = (request.form.get("description") or "").strip()
    upload = request.files.get("file")
    data, ext, original = None, "", ""
    if upload and upload.filename:
        original = collapse(Path(upload.filename.replace("\\", "/")).name)[:150]
        ext = file_extension(original)
        if has_control_chars(original) or ext not in FILE_EXTENSIONS:
            return title, description, None, "", "", "That file type is not allowed. Use PDF, Word, PowerPoint, Excel, text, image or audio files."
        data = upload.read(FILE_MAX_BYTES + 1)
        if not data:
            return title, description, None, "", "", "That file is empty."
        if len(data) > FILE_MAX_BYTES:
            return title, description, None, "", "", f"That file is too big. The limit is {FILE_MAX_BYTES // (1024 * 1024)} MB."
        if not title:
            title = original.rsplit(".", 1)[0]
    elif require_file:
        return title, description, None, "", "", "Choose a file to upload."
    if not title:
        return title, description, None, "", "", "Give the file a title."
    if len(title) > FILE_TITLE_MAX or has_control_chars(title):
        return title, description, None, "", "", f"The title must be {FILE_TITLE_MAX} characters or fewer."
    if len(description) > FILE_DESC_MAX or has_control_chars(description, allow_newlines=True):
        return title, description, None, "", "", f"The description must be {FILE_DESC_MAX} characters or fewer."
    return title, description, data, ext, original, None


def save_choir_file(data, ext):
    folder = Path(app.config["CHOIR_FILE_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(16) + "." + ext
    (folder / name).write_bytes(data)
    return name


def delete_choir_file(name):
    if name and STORED_FILE_RE.match(name):
        try:
            (Path(app.config["CHOIR_FILE_DIR"]) / name).unlink()
        except OSError:
            pass


def anchor_for(voice_id):
    return "general" if voice_id is None else f"voice-{voice_id}"


def is_officer_position(position_name):
    """Every choir position except the ordinary "Member" counts as an officer position."""
    return (position_name or "").strip().lower() != ORG_ORDINARY.lower()


def officer_details(position_name):
    """What the profile panel shows for an officer: title, description, duties, and what the position can do in this
    app. Description and duties come only from POSITION_DESCRIPTIONS (nothing is made up); the abilities come from
    POSITION_CAPABILITIES. Returns None for an ordinary member."""
    if not is_officer_position(position_name):
        return None
    key = position_name.strip().lower()
    written = next((v for k, v in POSITION_DESCRIPTIONS.items() if k.strip().lower() == key), {}) or {}
    rights = next((v for k, v in POSITION_CAPABILITIES.items() if k.strip().lower() == key), set())
    names = {code: (label, hint) for code, label, hint in ACTION_PERMISSIONS} | EXTRA_CAPABILITY_LABELS
    abilities = [{"label": names[code][0], "hint": names[code][1]} if code in names else {"label": code, "hint": ""}
                 for code in sorted(rights)]
    return {"title": position_name, "description": (written.get("description") or "").strip(),
            "duties": [d for d in (written.get("duties") or []) if str(d).strip()], "abilities": abilities}


@app.route("/member-area/members")
@member_area_required()
def choir_members():
    if not (member_can("panel.directory") or member_can("panel.general_files") or member_can("panel.voice_files")):
        abort(403)                                           # every panel on this page was switched off for this member
    db = get_db()
    q = collapse(request.args.get("q"))[:80]
    # [MEMBER GALLERY] Every member is sent to the page; app.js (static/js/members.js) searches and filters them on
    # the spot. Without JavaScript the search box still works: the server hides the cards that do not match.
    # The Member ID is deliberately NOT searched or sent here: it is the member's sign-in name.
    words = q.casefold().split()
    members = []
    for r in db.execute(
            "SELECT m.id, m.first_name, m.middle_name, m.last_name, m.photo_filename, vs.name AS voice_name, "
            "cp.name AS position_name, m.nickname FROM members m "
            "JOIN voice_sections vs ON vs.id = m.voice_section_id JOIN choir_positions cp ON cp.id = m.position_id "
            f"WHERE m.deleted_at IS NULL {ORDER_BY_NAME} LIMIT 300"):
        person = present_member(r)
        person["photo_url"] = url_for("choir_member_photo", member_id=r["id"]) if person["photo_ok"] else ""
        person["is_officer"] = is_officer_position(r["position_name"])
        person["search_text"] = " ".join(
            [person["display_name"], r["nickname"] or "", r["voice_name"], r["position_name"]]).casefold()
        person["shown"] = all(word in person["search_text"] for word in words)
        members.append(person)

    files = {}
    for row in db.execute("SELECT * FROM choir_files ORDER BY updated_at DESC, id DESC"):
        if can_view_files(row["voice_section_id"]):
            files.setdefault(row["voice_section_id"], []).append(present_file(row))

    sections = sorted((v for v in active_voice_sections(db) if can_view_files(v["id"])), key=lambda v: (
        VOICE_PANEL_ORDER.index(v["name"].lower()) if v["name"].lower() in VOICE_PANEL_ORDER else len(VOICE_PANEL_ORDER)))
    panels = [{"id": v["id"], "name": v["name"], "mine": v["id"] == g.member["voice_section_id"],
               "can_upload": can_file_action(v["id"], "upload"), "files": files.get(v["id"], [])} for v in sections]
    show_directory = member_can("panel.directory")
    members = members if show_directory else []
    return render_template("member_members.html", q=q, members=members, panels=panels,
                           officer_total=sum(1 for m in members if m["is_officer"]),
                           shown_total=sum(1 for m in members if m["shown"]),
                           org=build_org_structure(db, member_area=True),
                           general=files.get(None, []), show_directory=show_directory,
                           show_general=member_can("panel.general_files"), show_voices=member_can("panel.voice_files"),
                           can_upload_general=can_file_action(None, "upload"), file_max_mb=FILE_MAX_BYTES // (1024 * 1024),
                           file_types=", ".join(sorted(e.upper() for e in FILE_EXTENSIONS)))


@app.route("/member-area/members/<int(max=2147483647):member_id>")
@member_area_required()
def choir_member_view(member_id):
    """Read-only profile of another member. There is no edit route for other people's details."""
    if not member_can("panel.directory"):
        abort(403)
    row = get_db().execute(
        "SELECT m.id, m.first_name, m.middle_name, m.last_name, m.photo_filename, m.date_joined, m.email, m.phone, "
        "m.nickname, m.birthday, m.fb_link, m.profile_completed_at, vs.name AS voice_name, cp.name AS position_name "
        "FROM members m JOIN voice_sections vs ON vs.id = m.voice_section_id "
        "JOIN choir_positions cp ON cp.id = m.position_id "
        "WHERE m.id = ? AND m.deleted_at IS NULL", (member_id,)).fetchone()
    if row is None:
        abort(404)
    born = parse_iso_date(row["birthday"])
    # The reason for joining is private: only administrators see it. A birthday is shown as month and day.
    shown = {**present_member(row), "voice_name": row["voice_name"], "position_name": row["position_name"],
             "date_joined": row["date_joined"], "nickname": row["nickname"], "has_profile": bool(row["profile_completed_at"]),
             "birthday": f"{born:%B} {born.day}" if born else "", "email": row["email"],
             "phone": row["phone"], "facebook_link": row["fb_link"]}
    return render_template("member_choir_profile.html", person=shown, is_me=member_id == g.member["id"])


@app.route("/member-area/members/<int(max=2147483647):member_id>/card")
@member_area_required()
def choir_member_card(member_id):
    """[PROFILE PANEL] The data behind the side panel that opens on the Members page, as JSON (static/js/members.js).
    Same right as the full profile page (panel.directory). The answers a member typed in the "About you" form are
    shown like the full profile page shows them, with ONE difference that keeps the private part private: the reason
    for joining is sent only to the member themselves and to administrators who may read private details
    (members.private). A birthday shows month and day only, and the age is hidden, unless it is your own."""
    if not member_can("panel.directory"):
        abort(403)
    row = get_db().execute(
        "SELECT m.id, m.first_name, m.middle_name, m.last_name, m.photo_filename, m.date_joined, m.email, m.phone, "
        "m.nickname, m.birthday, m.fb_link, m.join_reason, m.profile_completed_at, "
        "vs.name AS voice_name, cp.name AS position_name "
        "FROM members m JOIN voice_sections vs ON vs.id = m.voice_section_id "
        "JOIN choir_positions cp ON cp.id = m.position_id "
        "WHERE m.id = ? AND m.deleted_at IS NULL", (member_id,)).fetchone()
    if row is None:
        abort(404)
    is_me = member_id == g.member["id"]
    sees_private = is_me or user_can("members.private")
    born = parse_iso_date(row["birthday"])
    if born:
        birthday = f"{born:%B} {born.day}, {born.year}" if sees_private else f"{born:%B} {born.day}"
    else:
        birthday = ""
    age = age_on(row["birthday"]) if (born and sees_private) else None
    joined = parse_iso_date(row["date_joined"])
    fb = row["fb_link"] or ""
    if fb and urlparse(fb).scheme not in ("http", "https"):
        fb = ""                                             # never hand the browser anything but a web link

    def item(label, value, kind="text"):
        return {"label": label, "value": value or "", "kind": kind}

    answered = bool(row["profile_completed_at"])
    reason_item = (item("Why do you want to join the choir?", row["join_reason"], "long") if sees_private
                   else {"label": "Why do you want to join the choir?", "value": "", "kind": "private"})
    payload = {
        "id": row["id"], "is_me": is_me,
        "name": member_full_name(row), "nickname": row["nickname"] or "",
        "initials": ((row["first_name"][:1] + row["last_name"][:1]) or "?").upper(),
        "voice": row["voice_name"], "position": row["position_name"],
        "is_officer": is_officer_position(row["position_name"]),
        "photo_url": url_for("choir_member_photo", member_id=row["id"]) if photo_file_ok(row["photo_filename"]) else "",
        "profile": [
            {"title": "Choir details", "items": [
                item("Voice section", row["voice_name"]),
                item("Choir position", row["position_name"]),
                item("Date joined", f"{joined:%B} {joined.day}, {joined.year}" if joined else row["date_joined"]),
            ]},
            {"title": "Contact", "items": [
                item("Email address", row["email"], "email"),
                item("Phone number", row["phone"], "phone"),
                item("Facebook link", fb, "link"),
                item("Birthday", birthday),
            ]} if answered else {"title": "Contact", "items": [
                item("Email address", row["email"], "email"),
                item("Phone number", row["phone"], "phone"),
            ]},
        ],
        # The registration form = the "About you" form each member fills in at first sign-in. Labels are the form's own.
        "registration": {
            "completed": answered, "submitted": row["profile_completed_at"] or "",
            "sections": [
                {"title": "About you", "items": [
                    item("Full name", member_full_name(row)),
                    item("Nickname", row["nickname"]),
                    item("Birthday", birthday),
                ] + ([item("Age", f"{age} years old")] if age is not None else [])},
                {"title": "How to reach you", "items": [
                    item("Email address", row["email"], "email"),
                    item("Phone number", row["phone"], "phone"),
                    item("Facebook link", fb, "link"),
                ]},
                {"title": "Your story", "items": [reason_item]},
            ] if answered else [],
        },
        "officer": officer_details(row["position_name"]),
    }
    response = jsonify(payload)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route("/member-area/members/<int(max=2147483647):member_id>/photo")
@member_area_required()
def choir_member_photo(member_id):
    if not member_can("panel.directory"):
        abort(403)
    if member_id >= LEADER_ID_OFFSET:                        # a non-member leader (Parish Priest...) shown in the tree
        row = get_db().execute("SELECT photo_filename FROM org_leaders WHERE id = ? AND deleted_at IS NULL",
                               (member_id - LEADER_ID_OFFSET,)).fetchone()
    else:
        row = get_db().execute("SELECT photo_filename FROM members WHERE id = ? AND deleted_at IS NULL", (member_id,)).fetchone()
    name = row["photo_filename"] if row else None
    if not name or not PHOTO_NAME_RE.match(name):
        abort(404)
    response = send_from_directory(app.config["MEMBER_PHOTO_DIR"], name, mimetype=PHOTO_MIME[name.rsplit(".", 1)[1]])
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@app.route("/member-area/files/upload", methods=["POST"])
@member_area_required()
def choir_file_upload():
    scope = request.form.get("scope", "")
    if scope == "general":
        voice_id = None
    elif scope.isdigit() and len(scope) < 10:
        voice_id = int(scope)
    else:
        abort(400)
    if not can_file_action(voice_id, "upload"):             # the server decides, whatever the form said
        abort(403)
    db = get_db()
    if voice_id is not None and db.execute("SELECT 1 FROM voice_sections WHERE id = ?", (voice_id,)).fetchone() is None:
        abort(404)
    back = url_for("choir_members") + "#" + anchor_for(voice_id)
    title, description, data, ext, original, error = read_file_form(require_file=True)
    if error:
        flash(error, "error")
        return redirect(back)
    stored = save_choir_file(data, ext)
    try:
        db.execute("INSERT INTO choir_files (voice_section_id, title, description, original_name, stored_name, size_bytes, "
                   "uploaded_by, uploader_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (voice_id, title, description, original, stored, len(data), g.user["id"], member_full_name(g.member)))
        db.commit()
    except sqlite3.Error:
        db.rollback()
        delete_choir_file(stored)
        flash("The file could not be saved. Nothing was changed.", "error")
        return redirect(back)
    flash(f"{title} was uploaded.", "success")
    return redirect(back)


@app.route("/member-area/files/<int(max=2147483647):file_id>/edit", methods=["GET", "POST"])
@member_area_required()
def choir_file_edit(file_id):
    db = get_db()
    row = fetch_choir_file(db, file_id)
    if not can_file_action(row["voice_section_id"], "edit"):
        abort(403)
    back = url_for("choir_members") + "#" + anchor_for(row["voice_section_id"])
    values = {"title": row["title"], "description": row["description"]}
    if request.method == "POST":
        title, description, data, ext, original, error = read_file_form(require_file=False)
        values = {"title": title, "description": description}
        if error:
            flash(error, "error")
        else:
            new_name = save_choir_file(data, ext) if data else None
            try:
                if data:
                    db.execute("UPDATE choir_files SET title = ?, description = ?, original_name = ?, stored_name = ?, "
                               "size_bytes = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                               (title, description, original, new_name, len(data), file_id))
                else:
                    db.execute("UPDATE choir_files SET title = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                               (title, description, file_id))
                db.commit()
            except sqlite3.Error:
                db.rollback()
                delete_choir_file(new_name)
                flash("The changes could not be saved. Nothing was changed.", "error")
                return render_template("member_file_edit.html", file=present_file(row), values=values, back=back,
                                       file_max_mb=FILE_MAX_BYTES // (1024 * 1024))
            if data:
                delete_choir_file(row["stored_name"])               # only after the database commit
            flash("The file was updated.", "success")
            return redirect(back)
    return render_template("member_file_edit.html", file=present_file(row), values=values, back=back,
                           file_max_mb=FILE_MAX_BYTES // (1024 * 1024))


@app.route("/member-area/files/<int(max=2147483647):file_id>/delete", methods=["POST"])
@member_area_required()
def choir_file_delete(file_id):
    db = get_db()
    row = fetch_choir_file(db, file_id)
    if not can_file_action(row["voice_section_id"], "delete"):
        abort(403)
    db.execute("DELETE FROM choir_files WHERE id = ?", (file_id,))
    db.commit()
    delete_choir_file(row["stored_name"])
    flash(f"{row['title']} was removed.", "success")
    return redirect(url_for("choir_members") + "#" + anchor_for(row["voice_section_id"]))


@app.route("/member-area/files/<int(max=2147483647):file_id>/download")
@member_area_required()
def choir_file_download(file_id):
    """Any signed-in member may download any choir file. The ?v= number only lets the browser tell versions apart."""
    row = fetch_choir_file(get_db(), file_id)
    if not can_view_files(row["voice_section_id"]):
        abort(403)
    if not STORED_FILE_RE.match(row["stored_name"]):
        abort(404)
    response = send_from_directory(app.config["CHOIR_FILE_DIR"], row["stored_name"], as_attachment=True,
                                   download_name=row["original_name"])
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@app.route("/member-area/sw.js")
def member_service_worker():
    """The small script that lets downloaded choir files open without internet. It holds no private data."""
    response = send_from_directory(BASE_DIR / "static" / "js", "sw.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/member-area/"
    return response


# ---------------------------------------------------------------- announcements --
# Posting goes through a small QUEUE so several people posting at once can never collide:
# each post is saved as "queued", then published one at a time, oldest first. If the database
# is busy the post simply stays queued and is retried the next time a page loads.

_announcement_queue_lock = threading.Lock()


def process_announcement_queue(db, limit=25):
    """Publish queued announcements one at a time. Returns how many were published."""
    if not _announcement_queue_lock.acquire(blocking=False):
        return 0                                   # someone else is already publishing
    published = 0
    try:
        for row in db.execute("SELECT id FROM announcements WHERE status = 'queued' ORDER BY id LIMIT ?",
                              (limit,)).fetchall():
            db.execute("UPDATE announcements SET status = 'published', is_active = 1, "
                       "published_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'queued'", (row["id"],))
            db.commit()
            published += 1
    except sqlite3.OperationalError:
        db.rollback()                              # database busy: the rest stay queued, retried next time
    finally:
        _announcement_queue_lock.release()
    return published


def read_announcement_form(db):
    """Validate the announcement form. Returns (values, clean, errors)."""
    form = request.form
    values = {"title": collapse(form.get("title")),
              "body": form.get("body", "").replace("\r\n", "\n").strip(),
              "audience": form.get("audience", "all").strip() or "all"}
    errors, clean = {}, dict(values)
    if len(values["title"]) < 3:
        errors["title"] = "Write a short title (at least 3 characters)."
    elif len(values["title"]) > ANNOUNCEMENT_TITLE_MAX or has_control_chars(values["title"]):
        errors["title"] = f"The title must be {ANNOUNCEMENT_TITLE_MAX} characters or fewer."
    if len(values["body"]) < 3:
        errors["body"] = "Write the announcement."
    elif len(values["body"]) > ANNOUNCEMENT_BODY_MAX or has_control_chars(values["body"], allow_newlines=True):
        errors["body"] = f"The announcement must be {ANNOUNCEMENT_BODY_MAX} characters or fewer."
    clean["audience_voice_id"] = None
    if values["audience"] != "all":
        ok = re.fullmatch(r"[0-9]{1,9}", values["audience"]) and db.execute(
            "SELECT 1 FROM voice_sections WHERE id = ? AND is_active = 1", (int(values["audience"]),)).fetchone()
        if ok:
            clean["audience_voice_id"] = int(values["audience"])
        else:
            errors["audience"] = "Choose who should see this."
    return values, clean, errors


def queue_announcement(db, clean, author_user_id, author_label, event=None):
    """Save an announcement (and publish it from the queue). Returns its id.

    event = (schedule_values, voice_ids) also creates the linked Schedule activity. The announcement, the activity and
    both log entries are saved in ONE transaction: either everything is saved or nothing is, so a failure can never
    leave an announcement without its event (or an event without its announcement)."""
    try:
        cursor = db.execute(
            "INSERT INTO announcements (title, body, audience_voice_id, author_user_id, author_name) VALUES (?, ?, ?, ?, ?)",
            (clean["title"], clean["body"], clean["audience_voice_id"], author_user_id, author_label))
        announcement_id = cursor.lastrowid
        log_activity(db, "announcement.posted", detail=clean["title"], actor_user_id=author_user_id,
                     target_type="announcement", target_id=announcement_id)
        if event is not None:
            ev_values, ev_voice_ids = event
            activity_id = save_activity(db, ev_values, ev_voice_ids, [], announcement_id=announcement_id)
            log_activity(db, "schedule.created", detail=f"{ev_values['title']} ({ev_values['activity_date']})",
                         actor_user_id=author_user_id, target_type="activity", target_id=activity_id)
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise
    process_announcement_queue(db)
    return announcement_id


def read_announcement_event(db, clean, announcement_errors):
    """[ANNOUNCEMENT LINK] The optional "Also add to Schedule as an event" part of the announcement form.

    Returns (ev, event, errors):
      ev      what to show again in the form (always filled)
      event   None when the box is not ticked, otherwise (schedule_values, voice_ids) ready for queue_announcement
      errors  messages for the event fields (keys start with ev_)
    The event fields are checked by read_schedule_form(), the same rules as the Schedule page, and the choir-wide
    "same title, date and time" duplicate check comes with it. Creating an event needs the schedule.manage right, which is
    checked HERE on the server, not just by hiding the fields."""
    form = request.form
    ev = {"add": form.get("add_to_schedule") == "1", "activity_date": form.get("activity_date", "").strip(),
          "start_time": form.get("start_time", "").strip(), "location": collapse(form.get("location")),
          "category": form.get("category", "").strip()}
    if not ev["add"]:
        return ev, None, {}                          # a normal announcement: the event fields are ignored completely
    if not user_can("schedule.manage"):
        abort(403)
    v, found, _voices, _members = read_schedule_form(db)
    errors = {f"ev_{key}": found[key] for key in ("category", "activity_date", "start_time", "location") if key in found}
    if "title" in found and "title" not in announcement_errors:
        errors["ev_duplicate"] = found["title"]      # the announcement title itself is fine, so this is the duplicate rule
    voice_ids = [clean["audience_voice_id"]] if clean.get("audience_voice_id") else []   # "Soprano only" = event for Soprano
    return ev, (v, voice_ids), errors


def active_voice_sections(db):
    return db.execute("SELECT id, name FROM voice_sections WHERE is_active = 1 ORDER BY sort_order, name").fetchall()


def announcements_for_member(db, member, tab):
    """Published announcements this member may see: general ones and ones for their voice section."""
    where, params = "(a.audience_voice_id IS NULL OR a.audience_voice_id = ?)", [member["voice_section_id"]]
    if tab == "general":
        where, params = "a.audience_voice_id IS NULL", []
    elif tab == "voice":
        where, params = "a.audience_voice_id = ?", [member["voice_section_id"]]
    return db.execute(
        f"SELECT a.id, a.title, a.body, a.author_name, a.published_at, a.audience_voice_id, vs.name AS audience_name "
        f"FROM announcements a LEFT JOIN voice_sections vs ON vs.id = a.audience_voice_id "
        f"WHERE a.status = 'published' AND a.is_active = 1 AND {where} "
        f"ORDER BY a.published_at DESC, a.id DESC LIMIT 12", params).fetchall()


# ------------------------------------------------------------- attendance and tasks --

def member_attendance_summary(db, member_id):
    rows = db.execute(
        "SELECT s.session_date, s.title, r.status FROM attendance_records r "
        "JOIN attendance_sessions s ON s.id = r.session_id WHERE r.member_id = ? "
        "ORDER BY s.session_date DESC, s.id DESC", (member_id,)).fetchall()
    counts = {code: 0 for code in ATTENDANCE_STATUSES}
    for row in rows:
        counts[row["status"]] += 1
    counted = counts["present"] + counts["late"] + counts["absent"]       # excused sessions are not counted against you
    return {"rows": rows[:6], "total": len(rows), "counts": counts,
            "rate": round(100 * (counts["present"] + counts["late"]) / counted) if counted else None}


def member_task_list(db, member_id):
    today = date.today().isoformat()
    rows = db.execute(
        "SELECT id, title, details, due_date, completed_at FROM tasks WHERE member_id = ? "
        "ORDER BY (completed_at IS NOT NULL), due_date, id LIMIT 30", (member_id,)).fetchall()
    return [{**dict(r), "done": r["completed_at"] is not None,
             "overdue": r["completed_at"] is None and r["due_date"] < today} for r in rows]


def member_calendar(db, member_id, month_text, show_activities=True):
    """Month grid for the dashboard calendar. month_text is 'YYYY-MM' (anything else = this month).
    Marks days with an attendance session (rehearsal/Mass) and days when one of YOUR tasks is due."""
    today = date.today()
    try:
        first = datetime.strptime(month_text or "", "%Y-%m").date()
    except ValueError:
        first = today.replace(day=1)
    last_day = calendar.monthrange(first.year, first.month)[1]
    start, end = first.isoformat(), first.replace(day=last_day).isoformat()

    sessions = db.execute("SELECT session_date, title, activity_id FROM attendance_sessions WHERE session_date BETWEEN ? AND ? "
                          "ORDER BY session_date, id", (start, end)).fetchall() if show_activities else []
    due = db.execute("SELECT due_date, title FROM tasks WHERE member_id = ? AND completed_at IS NULL "
                     "AND due_date BETWEEN ? AND ? ORDER BY due_date, id", (member_id, start, end)).fetchall()
    notes = {}                                    # 'YYYY-MM-DD' -> list of short notes shown in the tooltip
    kinds = {}                                    # 'YYYY-MM-DD' -> set of 'session' / 'task'
    for r in (db.execute("SELECT activity_date, title, start_time FROM activities WHERE status = 'scheduled' "
                         "AND activity_date BETWEEN ? AND ? ORDER BY activity_date, start_time, id", (start, end))
              if show_activities else ()):
        label = r["title"] + (" (" + format_time(r["start_time"]) + ")" if r["start_time"] else "")
        notes.setdefault(r["activity_date"], []).append(label)               # scheduled activities
        kinds.setdefault(r["activity_date"], set()).add("session")
    for r in sessions:
        if r["activity_id"] is not None:
            continue                                  # already shown above as its scheduled activity (or cancelled)
        notes.setdefault(r["session_date"], []).append(r["title"])
        kinds.setdefault(r["session_date"], set()).add("session")
    for r in due:
        notes.setdefault(r["due_date"], []).append("Task due: " + r["title"])
        kinds.setdefault(r["due_date"], set()).add("task")

    weeks = []                                    # weeks start on Sunday
    for week in calendar.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month):
        weeks.append([{"day": d.day, "other": d.month != first.month, "today": d == today,
                       "session": "session" in kinds.get(d.isoformat(), ()),
                       "task": "task" in kinds.get(d.isoformat(), ()),
                       "note": "; ".join(notes.get(d.isoformat(), []))} for d in week])
    prev_month = (first - timedelta(days=1)).strftime("%Y-%m")
    next_month = (first.replace(day=last_day) + timedelta(days=1)).strftime("%Y-%m")
    return {"title": f"{first:%B %Y}", "weeks": weeks, "prev": prev_month, "next": next_month,
            "is_current": first == today.replace(day=1)}


def member_recent_activity(db, member, limit=8, include_announcements=True):
    """Latest things that concern THIS member: announcements they can see, tasks given or finished,
    and attendance recorded for them. Newest first. (The admin-wide activity log is not shown to members.)"""
    items = []
    for r in (db.execute(
            "SELECT title, published_at AS at FROM announcements WHERE status = 'published' AND is_active = 1 "
            "AND (audience_voice_id IS NULL OR audience_voice_id = ?) ORDER BY published_at DESC, id DESC LIMIT ?",
            (member["voice_section_id"], limit)) if include_announcements else ()):
        items.append({"kind": "announcement", "text": "New announcement: " + r["title"], "at": r["at"]})
    for r in db.execute("SELECT title, created_at AS at FROM tasks WHERE member_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                        (member["id"], limit)):
        items.append({"kind": "task", "text": "Task assigned: " + r["title"], "at": r["at"]})
    for r in db.execute("SELECT title, completed_at AS at FROM tasks WHERE member_id = ? AND completed_at IS NOT NULL "
                        "ORDER BY completed_at DESC, id DESC LIMIT ?", (member["id"], limit)):
        items.append({"kind": "done", "text": "Task completed: " + r["title"], "at": r["at"]})
    for r in db.execute("SELECT s.title, s.session_date, r.status, r.marked_at AS at FROM attendance_records r "
                        "JOIN attendance_sessions s ON s.id = r.session_id WHERE r.member_id = ? "
                        "ORDER BY r.marked_at DESC, r.id DESC LIMIT ?", (member["id"], limit)):
        items.append({"kind": "attendance", "at": r["at"],
                      "text": f"Attendance recorded: {ATTENDANCE_STATUSES[r['status']]} · {r['title']} ({pretty_date_filter(r['session_date'])})"})
    items.sort(key=lambda i: i["at"] or "", reverse=True)
    return [{**i, "when": format_stamp(i["at"])} for i in items[:limit]]


TREASURER_NOTE_MAX = 500
MONEY_MAX = Decimal("999999999.99")


def parse_money(text):
    """'1,250.50' or '₱1250' -> centavos (125050). Returns None when it is not a valid amount."""
    try:
        amount = Decimal((text or "").replace(",", "").replace("\u20b1", "").strip())
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0 or amount > MONEY_MAX or amount != amount.quantize(Decimal("0.01")):
        return None
    return int(amount * 100)


def treasurer_reports(db, limit=4):
    """Newest reports first. The dashboard shows the first one big and the rest as a short history."""
    return db.execute("SELECT id, report_date, money_in, money_out, balance, notes, posted_name FROM treasurer_reports "
                      "ORDER BY report_date DESC, id DESC LIMIT ?", (limit,)).fetchall()


def render_member_dashboard(db, compose=None):
    me = g.member
    tab = request.args.get("tab", "all")
    tab = tab if tab in ("all", "general", "voice") else "all"
    tasks = member_task_list(db, me["id"])
    queued_own = db.execute("SELECT COUNT(*) FROM announcements WHERE status = 'queued' AND author_user_id = ?",
                            (g.user["id"],)).fetchone()[0] if member_can("announcements.post") else 0
    return render_template(
        "member_home.html", tab=tab,
        feed=announcements_for_member(db, me, tab) if member_can("panel.announcements") else [],
        attendance=member_attendance_summary(db, me["id"]), tasks=tasks,
        cal=member_calendar(db, me["id"], request.args.get("month"), show_activities=member_can("panel.schedule")),
        recent=member_recent_activity(db, me, include_announcements=member_can("panel.announcements")),
        treasurer=treasurer_reports(db) if member_can("panel.reports") else [],
        open_tasks=sum(1 for t in tasks if not t["done"]), queued_own=queued_own,
        voices=active_voice_sections(db), compose=compose or {"values": {}, "errors": {}, "open": False},
        statuses=ATTENDANCE_STATUSES, today=datetime.now().strftime("%A, %B %d, %Y").replace(" 0", " "))


@app.route("/member-area")
@member_area_required()
def member_home():
    db = get_db()
    process_announcement_queue(db)
    return render_member_dashboard(db)


@app.route("/member-area/announcements", methods=["POST"])
@member_area_required("announcements.post")
def member_announcement_post():
    db = get_db()
    values, clean, errors = read_announcement_form(db)
    if errors:
        flash("Please check the announcement and try again.", "error")
        return render_member_dashboard(db, compose={"values": values, "errors": errors, "open": True})
    queue_announcement(db, clean, g.user["id"], f"{member_full_name(g.member)} \u00b7 {g.member['position_name']}")
    flash("Your announcement was added to the queue and will appear on the dashboard in a moment.", "success")
    return redirect(url_for("member_home") + "#announcements")


# ---- secretary: tasks -----------------------------------------------------------------

def assignable_members(db):
    """Members a task or attendance can be recorded for: in the roster and not deactivated."""
    return db.execute(
        "SELECT m.id, m.member_code, m.first_name, m.middle_name, m.last_name, vs.name AS voice_name "
        "FROM members m JOIN voice_sections vs ON vs.id = m.voice_section_id "
        "WHERE m.deleted_at IS NULL AND m.status <> 'inactive' "
        "ORDER BY vs.sort_order, m.last_name COLLATE NOCASE, m.first_name COLLATE NOCASE").fetchall()


# ---- treasurer: the report shown on every member's dashboard ----------------------------

@app.route("/member-area/treasurer", methods=["GET", "POST"])
@member_area_required("finance.post")
def member_treasurer_manage():
    db = get_db()
    values, errors = {"report_date": date.today().isoformat(), "money_in": "", "money_out": "", "balance": "", "notes": ""}, {}
    if request.method == "POST":
        values = {k: request.form.get(k, "").strip() for k in ("report_date", "money_in", "money_out", "balance")}
        values["notes"] = request.form.get("notes", "").replace("\r\n", "\n").strip()
        as_of = parse_iso_date(values["report_date"])
        if as_of is None or as_of > date.today():
            errors["report_date"] = "Choose the date this report is as of (not in the future)."
        amounts = {}
        for key, label in (("money_in", "Money in"), ("money_out", "Money out"), ("balance", "Current balance")):
            amounts[key] = parse_money(values[key])
            if amounts[key] is None:
                errors[key] = f"{label}: enter an amount such as 1500 or 1,500.50."
        if len(values["notes"]) > TREASURER_NOTE_MAX or has_control_chars(values["notes"], allow_newlines=True):
            errors["notes"] = f"Notes must be {TREASURER_NOTE_MAX} characters or fewer."
        if not errors:
            db.execute("INSERT INTO treasurer_reports (report_date, money_in, money_out, balance, notes, posted_by, posted_name) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (as_of.isoformat(), amounts["money_in"], amounts["money_out"], amounts["balance"], values["notes"],
                        g.user["id"], member_full_name(g.member)))
            log_activity(db, "report.posted", detail=f"As of {as_of.isoformat()}", actor_user_id=g.user["id"], target_type="report")
            db.commit()
            flash("The treasurer's report is now on every member's dashboard.", "success")
            return redirect(url_for("member_treasurer_manage"))
        flash("Please check the form and try again.", "error")
    return render_template("member_treasurer.html", values=values, errors=errors, today=date.today().isoformat(),
                           reports=treasurer_reports(db, 30), notes_max=TREASURER_NOTE_MAX)


@app.route("/member-area/treasurer/<int(max=2147483647):report_id>/delete", methods=["POST"])
@member_area_required("finance.post")
def member_treasurer_delete(report_id):
    db = get_db()
    if db.execute("SELECT 1 FROM treasurer_reports WHERE id = ?", (report_id,)).fetchone() is None:
        abort(404)
    db.execute("DELETE FROM treasurer_reports WHERE id = ?", (report_id,))
    log_activity(db, "report.removed", actor_user_id=g.user["id"], target_type="report", target_id=report_id)
    db.commit()
    flash("Report removed.", "success")
    return redirect(url_for("member_treasurer_manage"))


@app.route("/member-area/tasks", methods=["GET", "POST"])
@member_area_required("tasks.manage")
def member_tasks_manage():
    db = get_db()
    members = assignable_members(db)
    values, errors = {"title": "", "details": "", "due_date": "", "member_ids": []}, {}
    if request.method == "POST":
        values = {"title": collapse(request.form.get("title")),
                  "details": request.form.get("details", "").replace("\r\n", "\n").strip(),
                  "due_date": request.form.get("due_date", "").strip(),
                  "member_ids": request.form.getlist("member_ids")}
        allowed = {str(m["id"]) for m in members}
        chosen = [x for x in dict.fromkeys(values["member_ids"]) if x in allowed]
        if len(values["title"]) < 3 or len(values["title"]) > TASK_TITLE_MAX or has_control_chars(values["title"]):
            errors["title"] = f"Write a task title (3 to {TASK_TITLE_MAX} characters)."
        if len(values["details"]) > TASK_DETAILS_MAX or has_control_chars(values["details"], allow_newlines=True):
            errors["details"] = f"Details must be {TASK_DETAILS_MAX} characters or fewer."
        due = parse_iso_date(values["due_date"])
        if due is None:
            errors["due_date"] = "Choose the target date."
        elif due < date.today():
            errors["due_date"] = "The target date cannot be in the past."
        if not chosen:
            errors["member_ids"] = "Choose at least one member."
        if not errors:
            for member_id in chosen:
                db.execute("INSERT INTO tasks (member_id, title, details, due_date, created_by) VALUES (?, ?, ?, ?, ?)",
                           (int(member_id), values["title"], values["details"], due.isoformat(), g.user["id"]))
            log_activity(db, "task.assigned", detail=f"{values['title']} ({len(chosen)} member{'s' if len(chosen) != 1 else ''})",
                         actor_user_id=g.user["id"], target_type="task")
            db.commit()
            flash(f"Task assigned to {len(chosen)} member{'s' if len(chosen) != 1 else ''}.", "success")
            return redirect(url_for("member_tasks_manage"))
        flash("Please check the form and try again.", "error")
    tasks = db.execute(
        "SELECT t.id, t.title, t.due_date, t.completed_at, m.first_name, m.middle_name, m.last_name, m.member_code "
        "FROM tasks t JOIN members m ON m.id = t.member_id "
        "WHERE m.deleted_at IS NULL ORDER BY (t.completed_at IS NOT NULL), t.due_date, t.id LIMIT 150").fetchall()
    today = date.today().isoformat()
    return render_template("member_tasks.html", members=members, values=values, errors=errors, today=today,
                           tasks=[{**dict(t), "name": member_full_name(t), "done": t["completed_at"] is not None,
                                   "overdue": t["completed_at"] is None and t["due_date"] < today} for t in tasks])


@app.route("/member-area/tasks/<int(max=2147483647):task_id>/complete", methods=["POST"])
@member_area_required("tasks.manage")
def member_task_toggle(task_id):
    db = get_db()
    if db.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
        abort(404)
    if request.form.get("done") == "1":
        db.execute("UPDATE tasks SET completed_at = CURRENT_TIMESTAMP, completed_by = ? WHERE id = ?", (g.user["id"], task_id))
    else:
        db.execute("UPDATE tasks SET completed_at = NULL, completed_by = NULL WHERE id = ?", (task_id,))
    db.commit()
    return redirect(url_for("member_tasks_manage"))


@app.route("/member-area/tasks/<int(max=2147483647):task_id>/delete", methods=["POST"])
@member_area_required("tasks.manage")
def member_task_delete(task_id):
    db = get_db()
    if db.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
        abort(404)
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    flash("The task was removed.", "info")
    return redirect(url_for("member_tasks_manage"))


# ---- secretary: attendance ------------------------------------------------------------

@app.route("/member-area/attendance", methods=["GET", "POST"])
@member_area_required("attendance.manage")
def member_attendance_manage():
    db = get_db()
    values, errors = {"title": "", "session_date": date.today().isoformat()}, {}
    if request.method == "POST":
        values = {"title": collapse(request.form.get("title")), "session_date": request.form.get("session_date", "").strip()}
        when = parse_iso_date(values["session_date"])
        if len(values["title"]) < 3 or len(values["title"]) > TASK_TITLE_MAX or has_control_chars(values["title"]):
            errors["title"] = "Write what this was, for example Sunday rehearsal."
        if when is None:
            errors["session_date"] = "Choose the date."
        elif when > date.today():
            errors["session_date"] = "Attendance can only be taken for today or a past date."
        if not errors:
            cursor = db.execute("INSERT INTO attendance_sessions (title, session_date, created_by) VALUES (?, ?, ?)",
                                (values["title"], when.isoformat(), g.user["id"]))
            db.commit()
            return redirect(url_for("member_attendance_mark", session_id=cursor.lastrowid))
        flash("Please check the form and try again.", "error")
    sessions = db.execute(
        "SELECT s.id, s.title, s.session_date, COUNT(r.id) AS marked, "
        "COALESCE(SUM(r.status IN ('present', 'late')), 0) AS attended "
        "FROM attendance_sessions s LEFT JOIN attendance_records r ON r.session_id = s.id "
        "GROUP BY s.id ORDER BY s.session_date DESC, s.id DESC LIMIT 30").fetchall()
    return render_template("member_attendance.html", sessions=sessions, values=values, errors=errors,
                           today=date.today().isoformat())


@app.route("/member-area/attendance/<int(max=2147483647):session_id>", methods=["GET", "POST"])
@member_area_required("attendance.manage")
def member_attendance_mark(session_id):
    db = get_db()
    session_row = db.execute("SELECT id, title, session_date FROM attendance_sessions WHERE id = ?", (session_id,)).fetchone()
    if session_row is None:
        abort(404)
    members = assignable_members(db)
    if request.method == "POST":
        for m in members:
            choice = request.form.get(f"status_{m['id']}", "")
            if choice in ATTENDANCE_STATUSES:
                db.execute(
                    "INSERT INTO attendance_records (session_id, member_id, status, marked_by) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (session_id, member_id) DO UPDATE SET status = excluded.status, "
                    "marked_by = excluded.marked_by, marked_at = CURRENT_TIMESTAMP",
                    (session_id, m["id"], choice, g.user["id"]))
            elif choice == "":
                db.execute("DELETE FROM attendance_records WHERE session_id = ? AND member_id = ?", (session_id, m["id"]))
        log_activity(db, "attendance.recorded", detail=f"{session_row['title']} ({session_row['session_date']})",
                     actor_user_id=g.user["id"], target_type="attendance", target_id=session_id)
        db.commit()
        flash("Attendance saved.", "success")
        return redirect(url_for("member_attendance_mark", session_id=session_id))
    current = {r["member_id"]: r["status"] for r in db.execute(
        "SELECT member_id, status FROM attendance_records WHERE session_id = ?", (session_id,))}
    return render_template("member_attendance_mark.html", session_row=session_row, statuses=ATTENDANCE_STATUSES,
                           members=[{**dict(m), "name": member_full_name(m), "status": current.get(m["id"], "")} for m in members])


# ------------------------------------------------------------ schedule & activities --
#
# One set of pages for everybody, at /schedule:
#   * Administrators and viewers open it from the admin side menu; choir members from the member menu.
#   * EVERYONE who can open it may VIEW. Only the administrator, or a member whose choir position has
#     "schedule.manage" (see POSITION_CAPABILITIES), may add, edit or cancel. The server checks this on
#     every route (schedule_access), not just by hiding buttons.
#   * Activities are never deleted. "Cancel" keeps the record and its attendance.
#   * Search words: [CATEGORY] [AUDIENCE] [CALENDAR] [OFFLINE]

# [CATEGORY] code -> label. The codes are also allowed by the CHECK rule on the activities table.
ACTIVITY_CATEGORIES = {
    "rehearsal": "Rehearsal",
    "liturgical": "Liturgical Service",
    "meeting": "Meeting",
    "formation": "Formation",
    "fellowship": "Fellowship",
    "parish": "Parish Activity",
    "other": "Other",
}
ACTIVITY_TITLE_MAX, ACTIVITY_VENUE_MAX, ACTIVITY_NOTES_MAX = 120, 120, 1000
SCHEDULE_LIST_LIMIT = 200
SCHEDULE_STATUSES = {"upcoming": "Upcoming", "past": "Past", "cancelled": "Cancelled", "all": "All"}


def can_manage_schedule():
    """The administrator, or a member whose position allows it."""
    return user_can("schedule.manage") or (getattr(g, "member", None) is not None and member_can("schedule.manage"))


def schedule_access(manage=False):
    """Protect a schedule route. manage=False: anyone signed in who may see the schedule. manage=True: officers only."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                flash("Sign in to continue.", "info")
                return redirect(url_for("login"))
            if manage:
                allowed = can_manage_schedule()
            else:
                allowed = user_can("schedule.view") or (
                    g.member is not None and user_can("member.home")
                    and (member_can("panel.schedule") or member_can("schedule.manage")))   # [ACTION CENTER] panel can be revoked
            if not allowed:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def schedule_layout():
    """Members see their own layout (member menu); administrators and viewers see the admin menu."""
    return "member_base.html" if getattr(g, "member", None) is not None else "base.html"


# ---- reading activities --------------------------------------------------------------------

AUDIENCE_NOBODY_ASSIGNED = ("NOT EXISTS (SELECT 1 FROM activity_voices x WHERE x.activity_id = a.id) "
                            "AND NOT EXISTS (SELECT 1 FROM activity_members y WHERE y.activity_id = a.id)")


def activity_audiences(db, ids):
    """{activity_id: {'voices': [names], 'members': [(id, name)]}} for the given activities."""
    result = {i: {"voices": [], "members": []} for i in ids}
    if not ids:
        return result
    marks = ",".join("?" * len(ids))
    for r in db.execute(f"SELECT av.activity_id, vs.name FROM activity_voices av JOIN voice_sections vs "
                        f"ON vs.id = av.voice_section_id WHERE av.activity_id IN ({marks}) ORDER BY vs.sort_order, vs.name", ids):
        result[r["activity_id"]]["voices"].append(r["name"])
    for r in db.execute(f"SELECT am.activity_id, m.id, m.first_name, m.middle_name, m.last_name FROM activity_members am "
                        f"JOIN members m ON m.id = am.member_id WHERE am.activity_id IN ({marks}) "
                        f"ORDER BY m.last_name COLLATE NOCASE, m.first_name COLLATE NOCASE", ids):
        result[r["activity_id"]]["members"].append((r["id"], member_full_name(r)))
    return result


def present_activities(db, rows, show_names):
    """Plain dicts for the templates. Individual member names are shown only to people who manage the schedule."""
    today = date.today().isoformat()
    audiences = activity_audiences(db, [r["id"] for r in rows])
    items = []
    for r in rows:
        aud, when = audiences[r["id"]], describe_date(r["activity_date"])
        parts = list(aud["voices"])
        if aud["members"]:
            parts.append(", ".join(name for _id, name in aud["members"]) if show_names
                         else f"{len(aud['members'])} member{'' if len(aud['members']) == 1 else 's'}")
        items.append({
            "id": r["id"], "title": r["title"], "category": r["category"],
            "category_label": ACTIVITY_CATEGORIES.get(r["category"], "Other"),
            "date": r["activity_date"], "month": when["month"], "day": when["day"], "date_label": when["label"],
            "time": format_time(r["start_time"]), "venue": r["location"], "instructions": r["instructions"],
            "cancelled": r["status"] == "cancelled", "reason": r["cancel_reason"],
            "past": r["activity_date"] < today,
            "announcement_id": r["announcement_id"],          # set when the activity was created with an announcement
            "everyone": not aud["voices"] and not aud["members"],
            "audience": ", ".join(parts) if parts else "Everyone",
            "voices": aud["voices"], "members": aud["members"],
        })
    return items


ACTIVITY_COLUMNS = ("a.id, a.title, a.category, a.activity_date, a.start_time, a.location, a.instructions, "
                    "a.status, a.cancel_reason, a.version, a.created_at, a.updated_at, a.announcement_id")


def schedule_filters():
    """Read and clean the search/filter choices from the page address."""
    args = request.args
    view = "list" if args.get("view") == "list" else "calendar"
    q = collapse(args.get("q"))[:80]
    category = args.get("category", "") if args.get("category", "") in ACTIVITY_CATEGORIES else ""
    voice = args.get("voice", "") if args.get("voice", "").isdigit() and len(args.get("voice", "")) <= 9 else ""   # a huge number would overflow SQLite
    status = args.get("status")
    if status not in SCHEDULE_STATUSES:
        status = "upcoming" if view == "list" else "all"               # each view has its own sensible default
    day = args.get("day", "") if parse_iso_date(args.get("day", "")) else ""
    mine = "1" if args.get("mine") == "1" and g.member is not None else ""
    try:
        first = datetime.strptime(args.get("month", ""), "%Y-%m").date()
    except ValueError:
        first = date.today().replace(day=1)
    return {"view": view, "q": q, "category": category, "voice": voice, "status": status, "day": day,
            "mine": mine, "month": first.strftime("%Y-%m"), "first": first}


def schedule_query(f, today):
    """(WHERE text, params) for the filters. Every value goes in as a parameter, never into the SQL text."""
    where, params = ["1 = 1"], []
    for token in f["q"].split()[:6]:
        like = "%" + like_escape(token) + "%"
        where.append("(a.title LIKE ? ESCAPE '\\' OR a.location LIKE ? ESCAPE '\\' OR a.instructions LIKE ? ESCAPE '\\')")
        params += [like, like, like]
    if f["category"]:
        where.append("a.category = ?"); params.append(f["category"])
    if f["voice"]:       # activities for that voice section, plus the ones for the whole choir
        where.append(f"(({AUDIENCE_NOBODY_ASSIGNED}) OR EXISTS (SELECT 1 FROM activity_voices v WHERE v.activity_id = a.id AND v.voice_section_id = ?))")
        params.append(int(f["voice"]))
    if f["mine"]:        # "assigned to me": whole choir, or my voice section, or me by name
        where.append(f"(({AUDIENCE_NOBODY_ASSIGNED}) OR EXISTS (SELECT 1 FROM activity_voices v WHERE v.activity_id = a.id AND v.voice_section_id = ?) "
                     f"OR EXISTS (SELECT 1 FROM activity_members w WHERE w.activity_id = a.id AND w.member_id = ?))")
        params += [g.member["voice_section_id"], g.member["id"]]
    if f["status"] == "upcoming":
        where.append("a.status = 'scheduled' AND a.activity_date >= ?"); params.append(today)
    elif f["status"] == "past":
        where.append("a.status = 'scheduled' AND a.activity_date < ?"); params.append(today)
    elif f["status"] == "cancelled":
        where.append("a.status = 'cancelled'")
    if f["day"]:
        where.append("a.activity_date = ?"); params.append(f["day"])
    return " AND ".join(where), params


def schedule_summary(db, today):
    """The four summary cards. Real counts, not affected by the search filters."""
    first = date.today().replace(day=1)
    month_end = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    one = lambda sql, *p: db.execute(sql, p).fetchone()[0]
    live = "status = 'scheduled'"
    upcoming = one(f"SELECT COUNT(*) FROM activities WHERE {live} AND activity_date >= ?", today)
    week = one(f"SELECT COUNT(*) FROM activities WHERE {live} AND activity_date BETWEEN ? AND ?",
               today, (date.today() + timedelta(days=6)).isoformat())
    month = one(f"SELECT COUNT(*) FROM activities WHERE {live} AND activity_date BETWEEN ? AND ?",
                first.isoformat(), month_end.isoformat())
    nxt = db.execute(f"SELECT title, activity_date, start_time FROM activities WHERE {live} AND activity_date >= ? "
                     f"ORDER BY activity_date, start_time, id LIMIT 1", (today,)).fetchone()
    if nxt:
        when = describe_date(nxt["activity_date"])
        next_card = {"label": "Next activity", "value": f"{when['month'].title()} {when['day']}", "state": "ready",
                     "note": nxt["title"] + (" \u00b7 " + format_time(nxt["start_time"]) if nxt["start_time"] else "")}
    else:
        next_card = summary_card("Next activity", 0, "", "Nothing scheduled", "")
    return [summary_card("Upcoming activities", upcoming, "From today onward", "Nothing scheduled yet", ""),
            summary_card("Next 7 days", week, "Starting today", "A quiet week", ""),
            summary_card("This month", month, f"In {first:%B}", "Nothing this month", ""),
            next_card]


def schedule_calendar(items, first):
    """Month grid (weeks start on Sunday). items: presented activities that fall in this month."""
    today = date.today()
    by_day = {}
    for it in items:
        by_day.setdefault(it["date"], []).append(it)
    weeks = []
    for week in calendar.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month):
        weeks.append([{"iso": d.isoformat(), "day": d.day, "other": d.month != first.month, "today": d == today,
                       "items": by_day.get(d.isoformat(), []) if d.month == first.month else []} for d in week])
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return {"title": f"{first:%B %Y}", "weeks": weeks, "prev": (first - timedelta(days=1)).strftime("%Y-%m"),
            "next": (last + timedelta(days=1)).strftime("%Y-%m"), "is_current": first == today.replace(day=1)}


@app.route("/schedule")
@schedule_access()
def schedule_list():
    db = get_db()
    f = schedule_filters()
    today = date.today().isoformat()
    where, params = schedule_query(f, today)
    manage = can_manage_schedule()
    cal = agenda = rows = None
    if f["view"] == "calendar":
        first = f["first"]
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        found = db.execute(f"SELECT {ACTIVITY_COLUMNS} FROM activities a WHERE {where} AND a.activity_date BETWEEN ? AND ? "
                           f"ORDER BY a.activity_date, a.start_time, a.id", params + [first.isoformat(), last.isoformat()]).fetchall()
        agenda = present_activities(db, found, manage)
        cal = schedule_calendar(agenda, first)
    else:
        order = "a.activity_date, a.start_time, a.id" if f["status"] == "upcoming" else "a.activity_date DESC, a.start_time DESC, a.id DESC"
        found = db.execute(f"SELECT {ACTIVITY_COLUMNS} FROM activities a WHERE {where} ORDER BY {order} LIMIT {SCHEDULE_LIST_LIMIT}",
                           params).fetchall()
        rows = present_activities(db, found, manage)
    upcoming = present_activities(db, db.execute(
        f"SELECT {ACTIVITY_COLUMNS} FROM activities a WHERE a.status = 'scheduled' AND a.activity_date >= ? "
        f"ORDER BY a.activity_date, a.start_time, a.id LIMIT 6", (today,)).fetchall(), manage)
    has_filters = bool(f["q"] or f["category"] or f["voice"] or f["day"] or f["mine"]
                       or f["status"] != ("upcoming" if f["view"] == "list" else "all"))
    return render_template(
        "schedule.html", layout=schedule_layout(), f=f, cal=cal, agenda=agenda, rows=rows, upcoming=upcoming,
        cards=schedule_summary(db, today), categories=ACTIVITY_CATEGORIES, statuses=SCHEDULE_STATUSES,
        voices=active_voice_sections(db), can_manage=manage, has_filters=has_filters, limit=SCHEDULE_LIST_LIMIT,
        clear_counts=schedule_clear_counts(db))


# ---- one activity ----------------------------------------------------------------------------

def fetch_activity(db, activity_id):
    row = db.execute(f"SELECT {ACTIVITY_COLUMNS}, a.uuid, a.cancelled_at FROM activities a WHERE a.id = ?", (activity_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/schedule/<int(max=2147483647):activity_id>")
@schedule_access()
def schedule_view(activity_id):
    db = get_db()
    row = fetch_activity(db, activity_id)
    manage = can_manage_schedule()
    item = present_activities(db, [row], manage)[0]
    attendance = db.execute(
        "SELECT s.id, COUNT(r.id) AS marked, COALESCE(SUM(r.status IN ('present', 'late')), 0) AS attended, "
        "COALESCE(SUM(r.status = 'absent'), 0) AS absent, COALESCE(SUM(r.status = 'excused'), 0) AS excused "
        "FROM attendance_sessions s LEFT JOIN attendance_records r ON r.session_id = s.id "
        "WHERE s.activity_id = ? GROUP BY s.id", (activity_id,)).fetchone()
    can_take = (g.member is not None and member_can("attendance.manage") and not item["cancelled"]
                and row["activity_date"] <= date.today().isoformat())
    linked = (db.execute("SELECT id, title, is_active FROM announcements WHERE id = ?", (row["announcement_id"],)).fetchone()
              if row["announcement_id"] else None)       # the announcement this activity was created with
    return render_template("schedule_detail.html", layout=schedule_layout(), a=item, attendance=attendance,
                           can_manage=manage, can_take=can_take, meta=row, announcement=linked)


# ---- add / edit ------------------------------------------------------------------------------

def read_schedule_form(db, activity_id=None):
    """Check the submitted form. Returns (values, errors, voice_ids, member_ids)."""
    form, errors = request.form, {}
    v = {"title": collapse(form.get("title")), "category": form.get("category", "").strip(),
         "activity_date": form.get("activity_date", "").strip(), "start_time": form.get("start_time", "").strip(),
         "location": collapse(form.get("location")), "instructions": form.get("instructions", "").replace("\r\n", "\n").strip()}
    if len(v["title"]) < 3 or len(v["title"]) > ACTIVITY_TITLE_MAX or has_control_chars(v["title"]):
        errors["title"] = f"Write a title of 3 to {ACTIVITY_TITLE_MAX} characters, for example Sunday rehearsal."
    if v["category"] not in ACTIVITY_CATEGORIES:
        errors["category"] = "Choose a category."
    when = parse_iso_date(v["activity_date"])
    if when is None or not (2000 <= when.year <= 2100):
        errors["activity_date"] = "Choose the date."
    elif activity_id is None and when < date.today():
        errors["activity_date"] = "Choose today or a later date."
    try:
        datetime.strptime(v["start_time"], "%H:%M")
    except ValueError:
        errors["start_time"] = "Choose the start time."
    if not v["location"] or len(v["location"]) > ACTIVITY_VENUE_MAX or has_control_chars(v["location"]):
        errors["location"] = "Enter the venue."
    if len(v["instructions"]) > ACTIVITY_NOTES_MAX or has_control_chars(v["instructions"], allow_newlines=True):
        errors["instructions"] = f"Instructions must be {ACTIVITY_NOTES_MAX} characters or fewer."

    voice_choices = {str(r["id"]) for r in db.execute("SELECT id FROM voice_sections")}
    member_choices = {str(r["id"]) for r in db.execute("SELECT id FROM members WHERE deleted_at IS NULL")}
    voice_ids = sorted({int(x) for x in form.getlist("voice_ids") if x in voice_choices})
    member_ids = sorted({int(x) for x in form.getlist("member_ids") if x in member_choices})

    if not errors:       # the same activity twice (for example a double click) is almost always a mistake
        same = db.execute("SELECT 1 FROM activities WHERE lower(title) = lower(?) AND activity_date = ? AND start_time = ? "
                          "AND status = 'scheduled' AND id <> ?", (v["title"], v["activity_date"], v["start_time"], activity_id or 0)).fetchone()
        if same:
            errors["title"] = "This activity is already scheduled for that date and time."
    return v, errors, voice_ids, member_ids


def schedule_form_choices(db, voice_ids, member_ids):
    """Voice sections and members to tick. Ones already assigned stay listed even if retired or inactive."""
    vmarks, mmarks = ",".join("?" * len(voice_ids)) or "NULL", ",".join("?" * len(member_ids)) or "NULL"
    voices = db.execute(f"SELECT id, name FROM voice_sections WHERE is_active = 1 OR id IN ({vmarks}) ORDER BY sort_order, name", voice_ids).fetchall()
    members = db.execute(
        f"SELECT m.id, m.first_name, m.middle_name, m.last_name, vs.name AS voice_name FROM members m "
        f"JOIN voice_sections vs ON vs.id = m.voice_section_id WHERE m.deleted_at IS NULL AND (m.status <> 'inactive' OR m.id IN ({mmarks})) "
        f"ORDER BY vs.sort_order, m.last_name COLLATE NOCASE, m.first_name COLLATE NOCASE", member_ids).fetchall()
    return voices, [{**dict(m), "name": member_full_name(m)} for m in members]


def save_activity(db, v, voice_ids, member_ids, activity_id=None, announcement_id=None):
    """Insert or update one activity and who it is for. Returns its id. Caller commits.
    announcement_id is used only when inserting (it links the new activity to its announcement). Editing an
    activity never changes or removes that link."""
    uid = g.user["id"]
    if activity_id is None:
        cursor = db.execute(
            "INSERT INTO activities (uuid, title, category, activity_date, start_time, location, instructions, created_by, updated_by, announcement_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), v["title"], v["category"], v["activity_date"], v["start_time"], v["location"], v["instructions"], uid, uid,
             announcement_id))
        activity_id = cursor.lastrowid
    else:
        db.execute(
            "UPDATE activities SET title = ?, category = ?, activity_date = ?, start_time = ?, location = ?, instructions = ?, "
            "updated_by = ?, updated_at = CURRENT_TIMESTAMP, version = version + 1 WHERE id = ?",
            (v["title"], v["category"], v["activity_date"], v["start_time"], v["location"], v["instructions"], uid, activity_id))
        db.execute("UPDATE attendance_sessions SET title = ?, session_date = ? WHERE activity_id = ?",   # keep attendance in step
                   (v["title"], v["activity_date"], activity_id))
    db.execute("DELETE FROM activity_voices WHERE activity_id = ?", (activity_id,))
    db.execute("DELETE FROM activity_members WHERE activity_id = ?", (activity_id,))
    db.executemany("INSERT INTO activity_voices (activity_id, voice_section_id) VALUES (?, ?)", [(activity_id, i) for i in voice_ids])
    db.executemany("INSERT INTO activity_members (activity_id, member_id) VALUES (?, ?)", [(activity_id, i) for i in member_ids])
    return activity_id


@app.route("/schedule/new", methods=["GET", "POST"])
@schedule_access(manage=True)
def schedule_new():
    db = get_db()
    values = {"title": "", "category": "", "activity_date": date.today().isoformat(), "start_time": "", "location": "", "instructions": ""}
    voice_ids, member_ids, errors = [], [], {}
    if request.method == "POST":
        values, errors, voice_ids, member_ids = read_schedule_form(db)
        if not errors:
            new_id = save_activity(db, values, voice_ids, member_ids)
            log_activity(db, "schedule.created", detail=f"{values['title']} ({values['activity_date']})",
                         actor_user_id=g.user["id"], target_type="activity", target_id=new_id)
            db.commit()
            flash("The schedule was added.", "success")
            return redirect(url_for("schedule_view", activity_id=new_id))
        flash("Please check the form and try again.", "error")
    voices, members = schedule_form_choices(db, voice_ids, member_ids)
    return render_template("schedule_form.html", layout=schedule_layout(), mode="new", values=values, errors=errors,
                           categories=ACTIVITY_CATEGORIES, voices=voices, members=members, voice_ids=voice_ids,
                           member_ids=member_ids, today=date.today().isoformat(), notes_max=ACTIVITY_NOTES_MAX, a=None)


@app.route("/schedule/<int(max=2147483647):activity_id>/edit", methods=["GET", "POST"])
@schedule_access(manage=True)
def schedule_edit(activity_id):
    db = get_db()
    row = fetch_activity(db, activity_id)
    voice_ids = [r[0] for r in db.execute("SELECT voice_section_id FROM activity_voices WHERE activity_id = ?", (activity_id,))]
    member_ids = [r[0] for r in db.execute("SELECT member_id FROM activity_members WHERE activity_id = ?", (activity_id,))]
    values, errors = {k: row[k] for k in ("title", "category", "activity_date", "start_time", "location", "instructions")}, {}
    if request.method == "POST":
        values, errors, voice_ids, member_ids = read_schedule_form(db, activity_id)
        if not errors:
            save_activity(db, values, voice_ids, member_ids, activity_id)
            log_activity(db, "schedule.updated", detail=f"{values['title']} ({values['activity_date']})",
                         actor_user_id=g.user["id"], target_type="activity", target_id=activity_id)
            db.commit()
            flash("The schedule was updated.", "success")
            return redirect(url_for("schedule_view", activity_id=activity_id))
        flash("Please check the form and try again.", "error")
    voices, members = schedule_form_choices(db, voice_ids, member_ids)
    return render_template("schedule_form.html", layout=schedule_layout(), mode="edit", values=values, errors=errors,
                           categories=ACTIVITY_CATEGORIES, voices=voices, members=members, voice_ids=voice_ids,
                           member_ids=member_ids, today=date.today().isoformat(), notes_max=ACTIVITY_NOTES_MAX, a=row)


@app.route("/schedule/<int(max=2147483647):activity_id>/cancel", methods=["POST"])
@schedule_access(manage=True)
def schedule_cancel(activity_id):
    """Cancel an activity (the record and its attendance are kept), or bring a cancelled one back."""
    db = get_db()
    row = fetch_activity(db, activity_id)
    if request.form.get("restore") == "1":
        db.execute("UPDATE activities SET status = 'scheduled', cancel_reason = '', cancelled_at = NULL, updated_by = ?, "
                   "updated_at = CURRENT_TIMESTAMP, version = version + 1 WHERE id = ?", (g.user["id"], activity_id))
        log_activity(db, "schedule.restored", detail=row["title"], actor_user_id=g.user["id"], target_type="activity", target_id=activity_id)
        flash("The schedule is active again.", "success")
    else:
        reason = collapse(request.form.get("reason"))[:200]
        if has_control_chars(reason):
            reason = ""
        db.execute("UPDATE activities SET status = 'cancelled', cancel_reason = ?, cancelled_at = CURRENT_TIMESTAMP, updated_by = ?, "
                   "updated_at = CURRENT_TIMESTAMP, version = version + 1 WHERE id = ?", (reason, g.user["id"], activity_id))
        log_activity(db, "schedule.cancelled", detail=row["title"], actor_user_id=g.user["id"], target_type="activity", target_id=activity_id)
        flash("The schedule was cancelled. The record is kept.", "success")
    db.commit()
    return redirect(url_for("schedule_view", activity_id=activity_id))


# ---- attendance: start (or reopen) the attendance sheet for an activity ------------------------

@app.route("/schedule/<int(max=2147483647):activity_id>/attendance", methods=["POST"])
@schedule_access()
def schedule_attendance(activity_id):
    """Members with the attendance right (the secretary) turn an activity into an attendance session."""
    if g.member is None or not member_can("attendance.manage"):
        abort(403)
    db = get_db()
    row = fetch_activity(db, activity_id)
    if row["status"] == "cancelled" or row["activity_date"] > date.today().isoformat():
        flash("Attendance can only be taken for an activity that is not cancelled and has started.", "error")
        return redirect(url_for("schedule_view", activity_id=activity_id))
    existing = db.execute("SELECT id FROM attendance_sessions WHERE activity_id = ?", (activity_id,)).fetchone()
    if existing is None:
        try:
            cursor = db.execute("INSERT INTO attendance_sessions (title, session_date, created_by, activity_id) VALUES (?, ?, ?, ?)",
                                (row["title"], row["activity_date"], g.user["id"], activity_id))
            db.commit()
            session_id = cursor.lastrowid
        except sqlite3.IntegrityError:          # someone else just did the same: use theirs
            db.rollback()
            session_id = db.execute("SELECT id FROM attendance_sessions WHERE activity_id = ?", (activity_id,)).fetchone()["id"]
    else:
        session_id = existing["id"]
    return redirect(url_for("member_attendance_mark", session_id=session_id))


# ---- [OFFLINE] read-only change feed ---------------------------------------------------------
# A future offline copy (for example a service worker) can ask "what changed since I last looked?":
#     GET /api/schedule?since=2026-10-20 07:00:00        (UTC, same format the database stores)
# Every activity has a stable uuid, an updated_at time and a version, and cancelled activities stay in the
# feed with status "cancelled", so a copy can always tell what to remove. Nothing here writes data.

@app.route("/api/schedule")
@schedule_access()
def schedule_feed():
    since = request.args.get("since", "").strip()
    if since:
        try:
            datetime.strptime(since, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            abort(400, "since must look like 2026-10-20 07:00:00 (UTC).")
    rows = get_db().execute(f"SELECT {ACTIVITY_COLUMNS}, a.uuid FROM activities a WHERE a.updated_at > ? "
                            f"ORDER BY a.updated_at, a.id LIMIT 500", (since or "1970-01-01 00:00:00",)).fetchall()
    items = present_activities(get_db(), rows, can_manage_schedule())
    body = {"server_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "schedules": [{"uuid": r["uuid"], "version": r["version"], "updated_at": r["updated_at"], "title": i["title"],
                           "category": i["category"], "date": i["date"], "time": r["start_time"], "venue": i["venue"],
                           "instructions": i["instructions"], "status": "cancelled" if i["cancelled"] else "scheduled",
                           "audience": i["audience"]} for r, i in zip(rows, items)]}
    for r in get_db().execute("SELECT uuid, deleted_at FROM activity_deletions WHERE deleted_at > ? ORDER BY deleted_at LIMIT 500",
                              (since or "1970-01-01 00:00:00",)):
        body["schedules"].append({"uuid": r["uuid"], "status": "deleted", "updated_at": r["deleted_at"]})
    response = jsonify(body)
    response.headers["Cache-Control"] = "private, no-cache"
    return response


# ---- [CLEAR] permanently delete schedules (administrator only) -----------------------------------
# Cancelling keeps the record. Clearing is for the administrator after pilot testing: it really removes
# activities. Safety: typed phrase + password, an automatic backup first, and attendance sheets are KEPT
# (just unlinked) unless the administrator ticks the box to remove them too.

SCHEDULE_CLEAR_PHRASE = "CLEAR SCHEDULE"


def delete_activities(db, ids, with_attendance):
    """Permanently remove these activities. Caller commits. Returns how many attendance sheets were removed."""
    removed_sheets = 0
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        db.execute(f"INSERT OR REPLACE INTO activity_deletions (uuid) SELECT uuid FROM activities WHERE id IN ({marks})", chunk)
        if with_attendance:
            removed_sheets += db.execute(f"DELETE FROM attendance_sessions WHERE activity_id IN ({marks})", chunk).rowcount  # records go with them
        else:
            db.execute(f"UPDATE attendance_sessions SET activity_id = NULL WHERE activity_id IN ({marks})", chunk)
        db.execute(f"DELETE FROM activity_voices WHERE activity_id IN ({marks})", chunk)
        db.execute(f"DELETE FROM activity_members WHERE activity_id IN ({marks})", chunk)
        db.execute(f"DELETE FROM activities WHERE id IN ({marks})", chunk)
    return removed_sheets


def schedule_clear_counts(db):
    one = lambda sql: db.execute(sql).fetchone()[0]
    return {"total": one("SELECT COUNT(*) FROM activities"),
            "cancelled": one("SELECT COUNT(*) FROM activities WHERE status = 'cancelled'"),
            "sheets": one("SELECT COUNT(*) FROM attendance_sessions WHERE activity_id IS NOT NULL"),
            "records": one("SELECT COUNT(*) FROM attendance_records r JOIN attendance_sessions s ON s.id = r.session_id "
                           "WHERE s.activity_id IS NOT NULL")}


@app.route("/schedule/clear", methods=["GET", "POST"])
@permission_required("schedule.clear")
def schedule_clear():
    db = get_db()
    counts = schedule_clear_counts(db)
    if request.method == "GET":
        return render_template("schedule_clear.html", counts=counts, phrase=SCHEDULE_CLEAR_PHRASE)

    # POST: every check must pass, otherwise NOTHING is changed
    scope = request.form.get("scope", "")
    with_attendance = request.form.get("with_attendance") == "1"
    if scope not in ("cancelled", "all"):
        flash("Choose what to clear. Nothing was changed.", "error")
        return redirect(url_for("schedule_clear"))
    if request.form.get("confirm_phrase", "").strip() != SCHEDULE_CLEAR_PHRASE:
        flash(f"To clear schedules, type {SCHEDULE_CLEAR_PHRASE} exactly. Nothing was changed.", "error")
        return redirect(url_for("schedule_clear"))
    me = db.execute("SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    if not me or not check_password_hash(me["password_hash"], request.form.get("password", "")):
        flash("Your password was not correct. Nothing was changed.", "error")
        return redirect(url_for("schedule_clear"))
    ids = [r[0] for r in db.execute("SELECT id FROM activities" + (" WHERE status = 'cancelled'" if scope == "cancelled" else ""))]
    if not ids:
        flash("There was nothing to clear.", "info")
        return redirect(url_for("schedule_list"))
    try:
        backup_name = backup_database_before_reset("schedule-clear")
    except (sqlite3.Error, OSError):
        flash("A safety backup could not be made, so nothing was changed.", "error")
        return redirect(url_for("schedule_clear"))
    try:
        sheets = delete_activities(db, ids, with_attendance)
        log_activity(db, "schedule.cleared", detail=f"{len(ids)} {'cancelled ' if scope == 'cancelled' else ''}schedule(s) cleared; backup {backup_name}",
                     actor_user_id=g.user["id"], target_type="schedule")
        db.commit()
    except sqlite3.Error:
        db.rollback()
        flash("The schedule could not be cleared. Nothing was changed.", "error")
        return redirect(url_for("schedule_clear"))
    flash(f"{len(ids)} schedule{'' if len(ids) == 1 else 's'} cleared"
          + (f" together with {sheets} attendance sheet{'' if sheets == 1 else 's'}" if with_attendance else ". Attendance sheets were kept")
          + f". A backup was saved as {backup_name} in the instance/backups folder.", "success")
    return redirect(url_for("schedule_list"))


@app.route("/schedule/<int(max=2147483647):activity_id>/delete", methods=["POST"])
@permission_required("schedule.clear")
def schedule_delete(activity_id):
    """Permanently delete ONE activity (administrator). Its attendance sheet, if any, is kept but unlinked."""
    db = get_db()
    row = fetch_activity(db, activity_id)
    delete_activities(db, [activity_id], with_attendance=False)
    log_activity(db, "schedule.deleted", detail=f"{row['title']} ({row['activity_date']})", actor_user_id=g.user["id"],
                 target_type="schedule")
    db.commit()
    flash("The schedule was deleted.", "success")
    return redirect(url_for("schedule_list"))


# ------------------------------------------------------ announcements (administrator) --

def announcement_rows(db, limit=100):
    """Announcements for the administrator's table, each with its linked Schedule event (if any)."""
    rows = []
    for r in db.execute(
            "SELECT a.id, a.title, a.body, a.status, a.is_active, a.author_name, a.created_at, a.published_at, "
            "vs.name AS audience_name, ac.id AS event_id, ac.activity_date AS ev_date, ac.start_time AS ev_time, "
            "ac.location AS ev_location, ac.category AS ev_category, ac.status AS ev_status "
            "FROM announcements a LEFT JOIN voice_sections vs ON vs.id = a.audience_voice_id "
            "LEFT JOIN activities ac ON ac.announcement_id = a.id ORDER BY a.id DESC LIMIT ?", (limit,)):
        item = dict(r)
        item["event"] = None
        if r["event_id"]:
            when = describe_date(r["ev_date"])
            item["event"] = {"id": r["event_id"], "date_label": when["label"], "time": format_time(r["ev_time"]),
                             "venue": r["ev_location"], "category_label": ACTIVITY_CATEGORIES.get(r["ev_category"], "Other"),
                             "cancelled": r["ev_status"] == "cancelled"}
        rows.append(item)
    return rows


@app.route("/announcements", methods=["GET", "POST"])
@permission_required("announcements.manage")
def announcements_admin():
    db = get_db()
    process_announcement_queue(db)
    values, errors = {"title": "", "body": "", "audience": "all"}, {}
    ev = {"add": False, "activity_date": date.today().isoformat(), "start_time": "", "location": "", "category": ""}
    if request.method == "POST":
        values, clean, errors = read_announcement_form(db)
        ev, event, event_errors = read_announcement_event(db, clean, errors)
        errors.update(event_errors)
        if not errors:
            try:
                queue_announcement(db, clean, g.user["id"], f"{g.user['full_name']} \u00b7 Administrator", event)
            except sqlite3.Error:
                flash("The announcement could not be saved, so nothing was changed. Please try again.", "error")
            else:
                flash("The announcement was added to the queue and will reach the members' dashboards in a moment."
                      + (" Its event was added to the Schedule." if event else ""), "success")
                return redirect(url_for("announcements_admin"))
        else:
            flash("Please check the announcement and try again.", "error")
    can_schedule = user_can("schedule.manage")
    preview = []
    if user_can("schedule.view"):        # the next few events that came from announcements (the Schedule preview card)
        preview = present_activities(db, db.execute(
            f"SELECT {ACTIVITY_COLUMNS} FROM activities a WHERE a.announcement_id IS NOT NULL AND a.status = 'scheduled' "
            f"AND a.activity_date >= ? ORDER BY a.activity_date, a.start_time, a.id LIMIT 3",
            (date.today().isoformat(),)).fetchall(), can_manage_schedule())
    return render_template("announcements_admin.html", values=values, errors=errors, rows=announcement_rows(db),
                           voices=active_voice_sections(db), ev=ev, categories=ACTIVITY_CATEGORIES,
                           today=date.today().isoformat(), can_schedule=can_schedule, preview=preview)


@app.route("/announcements/new")
@permission_required("announcements.manage")
def announcements_new():
    return redirect(url_for("announcements_admin") + "#new")


@app.route("/announcements/<int(max=2147483647):announcement_id>/retire", methods=["POST"])
@permission_required("announcements.manage")
def announcements_retire(announcement_id):
    db = get_db()
    row = db.execute("SELECT id, title, status FROM announcements WHERE id = ?", (announcement_id,)).fetchone()
    if row is None or row["status"] != "published":
        abort(404)
    active = 1 if request.form.get("active") == "1" else 0
    db.execute("UPDATE announcements SET is_active = ? WHERE id = ?", (active, announcement_id))
    log_activity(db, "announcement.restored" if active else "announcement.retired", detail=row["title"],
                 actor_user_id=g.user["id"], target_type="announcement", target_id=announcement_id)
    db.commit()
    if db.execute("SELECT 1 FROM activities WHERE announcement_id = ?", (announcement_id,)).fetchone():
        flash("The announcement was " + ("restored" if active else "retired")
              + ". Its event stays on the Schedule; change that from the Schedule page.", "success")
    return redirect(url_for("announcements_admin"))


# ------------------------------------------------------------------ music library --
# [MUSIC LIBRARY] Scores, recordings, lyrics and choir documents, in six categories.
#
# WHO MAY DO WHAT (always checked here on the server; hidden buttons are only for tidiness):
#   * administrators ("music.view", "music.manage" in ROLE_PERMISSIONS): everything
#   * choir members, from the Action Center:   panel "Music Library"  = open, preview, download, save offline
#                                              "Upload / Edit / Archive music files" = the three actions
#     By default every member has the panel, and only positions that manage General Choir Files
#     (President, Choir Director, Internal Secretary) have the three actions.
# Files are never in the public static folder and never linked by their storage name: every preview and
# download is a request to a route below, so a revoked permission takes effect on the very next click.
# Where the bytes are kept (this computer's disk, or an S3-compatible bucket) is decided in music_storage.py.
#
# "Available offline" is done in the browser (static/js/music.js and static/js/music_sw.js), it needs https
# (or http://localhost). Each person's device keeps its own copy.

MUSIC_CATEGORIES = {
    "sheet_music": "Sheet Music",
    "voice_tutorials": "Voice Tutorials",
    "songs_lyrics": "Songs & Lyrics",
    "liturgical_music": "Liturgical Music",
    "choir_documents": "Choir Documents",
    "general_resources": "General Resources",
}
MUSIC_MATERIAL_CATEGORIES = ("sheet_music", "voice_tutorials", "songs_lyrics", "liturgical_music")   # the "Music materials" card
_OFFICE = "application/vnd.openxmlformats-officedocument."
# extension: (content type sent to the browser, filter group, how it can be previewed in the page)
MUSIC_TYPES = {
    "pdf": ("application/pdf", "pdf", "pdf"),
    "mp3": ("audio/mpeg", "audio", "audio"), "m4a": ("audio/mp4", "audio", "audio"), "wav": ("audio/wav", "audio", "audio"),
    "mp4": ("video/mp4", "video", "video"),
    "png": ("image/png", "image", "image"), "jpg": ("image/jpeg", "image", "image"),
    "jpeg": ("image/jpeg", "image", "image"), "webp": ("image/webp", "image", "image"),
    "txt": ("text/plain; charset=utf-8", "document", "text"),
    "doc": ("application/msword", "document", None),
    "docx": (_OFFICE + "wordprocessingml.document", "document", None),
    "ppt": ("application/vnd.ms-powerpoint", "document", None),
    "pptx": (_OFFICE + "presentationml.presentation", "document", None),
    "xls": ("application/vnd.ms-excel", "document", None),
    "xlsx": (_OFFICE + "spreadsheetml.sheet", "document", None),
}
MUSIC_KIND_FILTERS = {"pdf": "PDF", "audio": "Audio", "video": "Video", "image": "Images", "document": "Documents"}
MUSIC_SORTS = {"new": "Newest first", "title": "Title A to Z", "composer": "Composer A to Z", "size": "Largest first"}
MUSIC_SORT_SQL = {"new": "f.created_at DESC, f.id DESC", "title": "f.title COLLATE NOCASE, f.id",
                  "composer": "(f.composer = ''), f.composer COLLATE NOCASE, f.title COLLATE NOCASE", "size": "f.size_bytes DESC, f.id DESC"}
MUSIC_TITLE_MAX, MUSIC_COMPOSER_MAX, MUSIC_NOTES_MAX = 120, 120, 200
MUSIC_PAGE_SIZE = 10
MUSIC_ACCEPT = ",".join("." + ext for ext in MUSIC_TYPES)


def music_storage():
    """The place the files are kept (disk or bucket). Built once, from the environment."""
    store = app.config.get("MUSIC_STORAGE_OBJ")
    if store is None:
        try:
            store = make_storage(str(Path(app.config["DATABASE"]).parent / "uploads" / "music_library"))
        except StorageError as error:
            app.logger.error("Music storage is not set up correctly: %s", error)
            abort(503)
        app.config["MUSIC_STORAGE_OBJ"] = store
    return store


@app.template_global()
def music_asset(filename):
    """A static file's address with its last-change time added, so browsers fetch the new copy after every update."""
    try:
        stamp = int((BASE_DIR / "static" / filename).stat().st_mtime)
    except OSError:
        stamp = 0
    return url_for("static", filename=filename, v=stamp)


def music_layout():
    """Members see their own layout (member menu); administrators see the admin menu."""
    return "member_base.html" if getattr(g, "member", None) is not None else "base.html"


def music_can_view():
    if g.user is None:
        return False
    if user_can("music.view"):
        return True
    return g.member is not None and user_can("member.home") and member_can("panel.music")


def music_can(action):
    """action: 'upload', 'edit' or 'archive'. A member also needs the Music Library panel itself."""
    if g.user is None:
        return False
    if user_can("music.manage"):
        return True
    return (g.member is not None and user_can("member.home")
            and member_can("panel.music") and member_can(f"music.{action}"))


def music_access(action=None):
    """Protect a Music Library route. action=None: anyone who may open the library. Otherwise that action's right."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                flash("Sign in to continue.", "info")
                return redirect(url_for("login"))
            if not (music_can_view() if action is None else music_can(action)):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


app.jinja_env.globals["music_can"] = lambda action: music_can(action)      # templates: {% if music_can('upload') %}


def format_stamp_date(value):
    """'2026-10-17 07:42:00' (UTC) -> 'Oct 17, 2026' in local time."""
    try:
        moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
    except (TypeError, ValueError):
        return value or ""
    return f"{moment:%b} {moment.day}, {moment.year}"


def music_signature_ok(ext, data):
    """The first bytes must look like the type the file name claims. (A renamed .exe or .html is refused.)"""
    head = data[:16]
    if ext == "pdf":
        return b"%PDF-" in data[:1024]
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == "webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == "wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == "mp3":
        return head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if ext in ("mp4", "m4a"):
        return head[4:8] == b"ftyp"
    if ext in ("docx", "pptx", "xlsx"):
        return head.startswith(b"PK\x03\x04")
    if ext in ("doc", "ppt", "xls"):
        return head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if ext == "txt":
        return b"\x00" not in data[:8192]
    return False


def fetch_music_file(db, file_id):
    """One file's row, or 404. Archived files are only found by people who may archive (so others never learn they exist)."""
    row = db.execute(
        "SELECT f.*, vs.name AS voice_name FROM music_files f "
        "LEFT JOIN voice_sections vs ON vs.id = f.voice_section_id WHERE f.id = ?", (file_id,)).fetchone()
    if row is None or (row["is_archived"] and not music_can("archive")):
        abort(404)
    return row


def present_music(row):
    mime, kind, preview = MUSIC_TYPES.get(row["ext"], ("application/octet-stream", "document", None))
    return {
        "id": row["id"], "title": row["title"], "notes": row["notes"], "composer": row["composer"],
        "category": row["category"], "category_label": MUSIC_CATEGORIES.get(row["category"], row["category"]),
        "voice_id": row["voice_section_id"], "voice_label": row["voice_name"] or "General",
        "ext": row["ext"].upper(), "kind": kind, "preview": preview, "size": row["size_bytes"],
        "size_label": format_size(row["size_bytes"]), "version": row["version"], "archived": bool(row["is_archived"]),
        "uploader": row["uploader_name"] or "Former member", "date": format_stamp_date(row["created_at"]),
        "original_name": row["original_name"],
        "file_url": url_for("music_file", file_id=row["id"], v=row["version"]),
        "download_url": url_for("music_download", file_id=row["id"]),
        "edit_url": url_for("music_edit", file_id=row["id"]),
    }


def music_voice_options(db):
    return [{"id": v["id"], "name": v["name"]} for v in active_voice_sections(db)]


def read_music_form(db, require_file, current=None):
    """Check an upload or edit form. Returns (values, errors, data, ext, original_name, voice_id)."""
    form = request.form
    values = {"title": collapse(form.get("title")), "category": (form.get("category") or "").strip(),
              "voice": (form.get("voice") or "").strip(), "composer": collapse(form.get("composer")),
              "notes": collapse(form.get("notes"))}
    errors, data, ext, original = {}, None, "", ""

    upload = request.files.get("file")
    if upload and upload.filename:
        original = collapse(Path(upload.filename.replace("\\", "/")).name)[:150]
        ext = file_extension(original)
        if has_control_chars(original) or ext not in MUSIC_TYPES:
            errors["file"] = "That file type is not allowed. Use PDF, audio (MP3, M4A, WAV), video (MP4), image, text, Word, PowerPoint or Excel files."
        else:
            data = upload.read(MUSIC_MAX_BYTES + 1)
            if not data:
                errors["file"] = "That file is empty."
            elif len(data) > MUSIC_MAX_BYTES:
                errors["file"] = f"That file is too big. The limit is {MUSIC_MAX_MB} MB."
            elif not music_signature_ok(ext, data):
                errors["file"] = f"That file does not look like a real .{ext} file, so it was not accepted."
            if errors.get("file"):
                data = None
            elif not values["title"]:
                values["title"] = original.rsplit(".", 1)[0]
    elif require_file:
        errors["file"] = "Choose a file to upload."

    if not values["title"]:
        errors["title"] = "Give the file a title."
    elif len(values["title"]) > MUSIC_TITLE_MAX or has_control_chars(values["title"]):
        errors["title"] = f"The title must be {MUSIC_TITLE_MAX} characters or fewer."
    if values["category"] not in MUSIC_CATEGORIES:
        errors["category"] = "Choose a category."
    if len(values["composer"]) > MUSIC_COMPOSER_MAX or has_control_chars(values["composer"]):
        errors["composer"] = f"The composer must be {MUSIC_COMPOSER_MAX} characters or fewer."
    if len(values["notes"]) > MUSIC_NOTES_MAX or has_control_chars(values["notes"]):
        errors["notes"] = f"The note must be {MUSIC_NOTES_MAX} characters or fewer."
    voice_id = None
    if values["voice"] not in ("", "general"):
        if values["voice"].isdigit() and len(values["voice"]) < 10 and db.execute(
                "SELECT 1 FROM voice_sections WHERE id = ?", (int(values["voice"]),)).fetchone():
            voice_id = int(values["voice"])
        else:
            errors["voice"] = "Choose a voice section."
    return values, errors, data, ext, original, voice_id


def music_uploader_name():
    return member_full_name(g.member) if g.member is not None else g.user["full_name"]


def safe_music_return(default):
    """Where to go back to after an action: only a page inside the Music Library."""
    target = request.form.get("return_to") or ""
    parsed = urlparse(target)
    return target if target.startswith("/music/") and not parsed.netloc and "\\" not in target else default


def music_page_url(args, page):
    return url_for("music_library", **{**args, "page": page if page > 1 else None})


def render_music_library(upload=None, status=200):
    """The library page. 'upload' (values, errors) re-opens the upload box after a refused upload."""
    db = get_db()
    can_archive = music_can("archive")
    args = {}
    q = collapse(request.args.get("q"))[:80]
    category = request.args.get("category", "")
    category = category if category in MUSIC_CATEGORIES else ""
    voice = request.args.get("voice", "")
    voice = voice if voice == "general" or (voice.isdigit() and len(voice) < 10) else ""
    kind = request.args.get("type", "")
    kind = kind if kind in MUSIC_KIND_FILTERS else ""
    sort = request.args.get("sort", "new")
    sort = sort if sort in MUSIC_SORTS else "new"
    archived = request.args.get("archived") == "1" and can_archive
    for key, value in (("q", q), ("category", category), ("voice", voice), ("type", kind),
                       ("sort", sort if sort != "new" else ""), ("archived", "1" if archived else "")):
        if value:
            args[key] = value

    where, params = ["f.is_archived = ?"], [1 if archived else 0]
    if category:
        where.append("f.category = ?"); params.append(category)
    if voice == "general":
        where.append("f.voice_section_id IS NULL")
    elif voice:
        where.append("f.voice_section_id = ?"); params.append(int(voice))
    if kind:
        exts = [ext for ext, (_m, group, _p) in MUSIC_TYPES.items() if group == kind]
        where.append(f"f.ext IN ({','.join('?' * len(exts))})"); params += exts
    for token in q.split()[:6]:
        like = "%" + like_escape(token) + "%"
        fields = ["f.title", "f.composer", "f.notes", "f.original_name", "f.uploader_name", "vs.name"]
        clause = " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields)
        params += [like] * len(fields)
        matching = [code for code, label in MUSIC_CATEGORIES.items() if token.lower() in label.lower()]
        if matching:
            clause += f" OR f.category IN ({','.join('?' * len(matching))})"; params += matching
        where.append(f"({clause})")
    condition = " AND ".join(where)
    join = "FROM music_files f LEFT JOIN voice_sections vs ON vs.id = f.voice_section_id"
    found = db.execute(f"SELECT COUNT(*) {join} WHERE {condition}", params).fetchone()[0]
    pages = max(1, -(-found // MUSIC_PAGE_SIZE))
    page = request.args.get("page", "1")
    page = min(max(int(page) if page.isdigit() and len(page) < 7 else 1, 1), pages)
    rows = db.execute(
        f"SELECT f.*, vs.name AS voice_name {join} WHERE {condition} ORDER BY {MUSIC_SORT_SQL[sort]} LIMIT ? OFFSET ?",
        params + [MUSIC_PAGE_SIZE, (page - 1) * MUSIC_PAGE_SIZE]).fetchall()

    # Folder and voice section counts always cover the whole (non-archived) library, whatever is searched.
    cat_counts = {r["category"]: r["n"] for r in db.execute(
        "SELECT category, COUNT(*) AS n FROM music_files WHERE is_archived = 0 GROUP BY category")}
    voice_counts = {r["voice_section_id"]: r["n"] for r in db.execute(
        "SELECT voice_section_id, COUNT(*) AS n FROM music_files WHERE is_archived = 0 GROUP BY voice_section_id")}
    total = sum(cat_counts.values())
    materials = sum(cat_counts.get(code, 0) for code in MUSIC_MATERIAL_CATEGORIES)
    archived_count = db.execute("SELECT COUNT(*) FROM music_files WHERE is_archived = 1").fetchone()[0] if can_archive else 0
    # Every file the signed-in person may open (id and version): the page uses it to drop offline copies that no longer apply.
    index = [{"id": r["id"], "v": r["version"]} for r in db.execute("SELECT id, version FROM music_files WHERE is_archived = 0")]

    pager = []
    if pages > 1:
        shown = sorted({1, pages, *range(max(1, page - 1), min(pages, page + 1) + 1)})
        last = 0
        for n in shown:
            if n - last > 1:
                pager.append({"gap": True})
            pager.append({"n": n, "url": music_page_url(args, n) + "#library", "current": n == page})
            last = n
    first_item = (page - 1) * MUSIC_PAGE_SIZE + 1 if found else 0

    def link(**changes):
        """The library address with some choices changed (None clears one). Used by the folder list on the left."""
        merged = dict(args)
        for key, value in changes.items():
            if value in (None, ""):
                merged.pop(key, None)
            else:
                merged[key] = value
        return url_for("music_library", **merged) + "#library"

    return render_template(
        "music_library.html", layout=music_layout(), rows=[present_music(r) for r in rows], found=found,
        first_item=first_item, last_item=first_item + len(rows) - 1 if rows else 0,
        pager=pager, prev_url=music_page_url(args, page - 1) + "#library" if page > 1 else None,
        next_url=music_page_url(args, page + 1) + "#library" if page < pages else None,
        f={"q": q, "category": category, "voice": voice, "type": kind, "sort": sort, "archived": archived},
        has_filters=bool(q or category or voice or kind), link=link,
        categories=MUSIC_CATEGORIES, cat_counts=cat_counts, voice_counts=voice_counts, general_count=voice_counts.get(None, 0),
        voices=music_voice_options(db), kinds=MUSIC_KIND_FILTERS, sorts=MUSIC_SORTS,
        stats={"total": total, "materials": materials, "categories": len(MUSIC_CATEGORIES)},
        archived_count=archived_count, index=index, max_mb=MUSIC_MAX_MB, accept=MUSIC_ACCEPT,
        can_upload=music_can("upload"), can_edit=music_can("edit"), can_archive=can_archive,
        upload=upload, open_upload=bool(upload) or (request.args.get("upload") == "1" and music_can("upload")),
    ), status


@app.route("/music/")
@music_access()
def music_library():
    return render_music_library()


@app.route("/music/upload", methods=["GET", "POST"])
@music_access("upload")
def music_upload():
    if request.method == "GET":                                   # the dashboard's "Upload music" button lands here
        return redirect(url_for("music_library", upload=1))
    db = get_db()
    values, errors, data, ext, original, voice_id = read_music_form(db, require_file=True)
    if errors:
        return render_music_library(upload={"values": values, "errors": errors}, status=400)
    store, stored = music_storage(), secrets.token_hex(16) + "." + ext
    try:
        store.put(stored, data, MUSIC_TYPES[ext][0])
    except StorageError as error:
        app.logger.error("Music upload could not be stored: %s", error)
        return render_music_library(upload={"values": values, "errors": {"file": "The file could not be saved. Nothing was changed. Please try again."}}, status=503)
    try:
        cursor = db.execute(
            "INSERT INTO music_files (title, category, voice_section_id, composer, notes, original_name, stored_name, ext, "
            "size_bytes, uploaded_by, uploader_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (values["title"], values["category"], voice_id, values["composer"], values["notes"], original, stored, ext,
             len(data), g.user["id"], music_uploader_name()))
        log_activity(db, "music.uploaded", detail=values["title"], actor_user_id=g.user["id"],
                     target_type="music_file", target_id=cursor.lastrowid)
        db.commit()
    except sqlite3.Error:
        db.rollback()
        try:
            store.delete(stored)
        except StorageError:
            pass
        return render_music_library(upload={"values": values, "errors": {"file": "The file could not be saved. Nothing was changed."}}, status=500)
    flash(f"{values['title']} was added to the Music Library.", "success")
    return redirect(url_for("music_library", category=values["category"]))


def range_for(size):
    """A single 'Range: bytes=a-b' request -> (start, end), None for a normal full request, 'bad' if unsatisfiable."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (request.headers.get("Range") or "").strip())
    if not match or match.group(0) == "bytes=-":
        return None
    first, last = match.groups()
    if first == "":
        count = int(last)
        if count == 0:
            return "bad"
        return (max(size - count, 0), size - 1)
    start, end = int(first), (min(int(last), size - 1) if last else size - 1)
    return "bad" if start >= size or start > end else (start, end)


def serve_music(row, attachment):
    """Send one file: whole, or the part a player asks for (so audio and video can skip ahead)."""
    info = MUSIC_TYPES.get(row["ext"])
    if info is None:
        abort(404)
    mime, _group, preview = info
    size, etag = row["size_bytes"], f'"{row["stored_name"]}"'
    attachment = attachment or preview is None
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", row["original_name"]) or "file"
    headers = {
        "Content-Type": mime, "ETag": etag, "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-cache",                          # the browser must ask us (and our permission check) every time
        "Content-Disposition": f"{'attachment' if attachment else 'inline'}; filename=\"{ascii_name}\"; "
                               f"filename*=UTF-8''{quote(row['original_name'])}",
        "X-Frame-Options": "SAMEORIGIN",                               # the preview box on our own page may show it
        "X-Music-File": str(row["id"]),                                # lets the page confirm an offline copy is a real file
    }
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={k: v for k, v in headers.items() if k in ("ETag", "Cache-Control")})
    span = range_for(size)
    if span == "bad":
        return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
    start, end = span if span else (0, size - 1)
    try:
        stream = music_storage().read(row["stored_name"], start, end)
    except StorageNotFound:
        app.logger.error("Music file %s is missing from storage", row["id"])
        abort(404)
    except StorageError as error:
        app.logger.error("Music file %s could not be read: %s", row["id"], error)
        abort(503)
    headers["Content-Length"] = str(end - start + 1)
    if span:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(stream, status=206 if span else 200, headers=headers)


@app.route("/music/files/<int(max=2147483647):file_id>/file")
@music_access()
def music_file(file_id):
    """The file itself: shown in the page when its type allows (PDF, image, audio, video, text), otherwise saved."""
    return serve_music(fetch_music_file(get_db(), file_id), attachment=False)


@app.route("/music/files/<int(max=2147483647):file_id>/download")
@music_access()
def music_download(file_id):
    return serve_music(fetch_music_file(get_db(), file_id), attachment=True)


@app.route("/music/files/<int(max=2147483647):file_id>/edit", methods=["GET", "POST"])
@music_access("edit")
def music_edit(file_id):
    db = get_db()
    row = fetch_music_file(db, file_id)
    values = {"title": row["title"], "category": row["category"], "voice": str(row["voice_section_id"] or "general"),
              "composer": row["composer"], "notes": row["notes"]}
    errors = {}
    if request.method == "POST":
        values, errors, data, ext, original, voice_id = read_music_form(db, require_file=False)
        if not errors:
            store, new_key = music_storage(), None
            if data:
                new_key = secrets.token_hex(16) + "." + ext
                try:
                    store.put(new_key, data, MUSIC_TYPES[ext][0])
                except StorageError as error:
                    app.logger.error("Music replacement could not be stored: %s", error)
                    errors["file"] = "The file could not be saved. Nothing was changed. Please try again."
            if not errors:
                try:
                    if data:
                        db.execute("UPDATE music_files SET title = ?, category = ?, voice_section_id = ?, composer = ?, notes = ?, "
                                   "original_name = ?, stored_name = ?, ext = ?, size_bytes = ?, version = version + 1, "
                                   "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                   (values["title"], values["category"], voice_id, values["composer"], values["notes"],
                                    original, new_key, ext, len(data), file_id))
                    else:
                        db.execute("UPDATE music_files SET title = ?, category = ?, voice_section_id = ?, composer = ?, notes = ?, "
                                   "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                   (values["title"], values["category"], voice_id, values["composer"], values["notes"], file_id))
                    log_activity(db, "music.updated", detail=values["title"], actor_user_id=g.user["id"],
                                 target_type="music_file", target_id=file_id)
                    db.commit()
                except sqlite3.Error:
                    db.rollback()
                    if new_key:
                        try:
                            store.delete(new_key)
                        except StorageError:
                            pass
                    errors["file"] = "The changes could not be saved. Nothing was changed."
                else:
                    if new_key:                                      # the old file goes only after the database commit
                        try:
                            store.delete(row["stored_name"])
                        except StorageError as error:
                            app.logger.warning("Old music file %s could not be removed: %s", row["stored_name"], error)
                    flash("The file was updated.", "success")
                    return redirect(url_for("music_library", category=values["category"]))
    return render_template("music_edit.html", layout=music_layout(), file=present_music(row), values=values, errors=errors,
                           categories=MUSIC_CATEGORIES, voices=music_voice_options(db), max_mb=MUSIC_MAX_MB,
                           accept=MUSIC_ACCEPT), (400 if errors else 200)


def set_music_archived(file_id, archived):
    db = get_db()
    row = fetch_music_file(db, file_id)
    db.execute("UPDATE music_files SET is_archived = ?, archived_at = ?, archived_by = ?, updated_at = CURRENT_TIMESTAMP "
               "WHERE id = ?", (1 if archived else 0, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if archived else None,
                                g.user["id"] if archived else None, file_id))
    log_activity(db, "music.archived" if archived else "music.restored", detail=row["title"], actor_user_id=g.user["id"],
                 target_type="music_file", target_id=file_id)
    db.commit()
    flash(f"{row['title']} was {'archived' if archived else 'restored'}.", "success")
    return redirect(safe_music_return(url_for("music_library")))


@app.route("/music/files/<int(max=2147483647):file_id>/archive", methods=["POST"])
@music_access("archive")
def music_archive(file_id):
    return set_music_archived(file_id, True)


@app.route("/music/files/<int(max=2147483647):file_id>/restore", methods=["POST"])
@music_access("archive")
def music_restore(file_id):
    return set_music_archived(file_id, False)


@app.route("/music/sw.js")
def music_service_worker():
    """The small script that lets saved music files open without internet. It holds no private data."""
    response = send_from_directory(BASE_DIR / "static" / "js", "music_sw.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/music/"
    return response


@app.route("/music/offline")
def music_offline_shell():
    """What opens when the network is down: a plain page that lists the files saved on THIS device. No private data."""
    response = make_response(render_template("music_offline.html"))
    response.headers["Cache-Control"] = "no-cache"
    return response


# ------------------------------------------------------------------ action center --
# [ACTION CENTER] The right-hand sidebar (administrators only, permission "members.access").
# An administrator picks ONE choir member and switches member-area permissions on or off. Nothing is edited in
# code: the choices are saved in the member_permissions table and take effect on the member's next click,
# because member_can() (see "member permissions" above) reads them on every request.
#
# Only DIFFERENCES from the member's position defaults are stored, so a member nobody touched behaves exactly
# as before. "Reset" deletes the member's rows. This controls what a choir MEMBER may do inside the member
# area; the admin / viewer roles in ROLE_PERMISSIONS are not changed by it.

def access_voices(db, member):
    """Voice sections to show in the file matrix: active ones, plus the member's own even if retired."""
    return db.execute("SELECT id, name FROM voice_sections WHERE is_active = 1 OR id = ? ORDER BY sort_order, name",
                      (member["voice_section_id"],)).fetchall()


def access_labels(voices):
    """{permission key: readable label}. Also the list of keys the server accepts (anything else is ignored)."""
    labels = {key: label for key, label, _hint in PANEL_PERMISSIONS}
    labels.update({key: label for key, label, _hint in ACTION_PERMISSIONS})
    for voice in voices:
        for action, action_label in VOICE_FILE_PERMISSIONS:
            labels[f"vfile.{action}:{voice['id']}"] = f"{action_label} files · {voice['name']}"
    return labels


def access_payload(db, member):
    """Everything the sidebar needs to draw one member's switches: current state, default, and whether it is custom."""
    overrides = member_overrides(db, member["id"])
    voices = access_voices(db, member)
    labels = access_labels(voices)

    def item(key, label, hint=""):
        default = permission_baseline(member, key)
        on = overrides.get(key, default)
        return {"key": key, "label": label, "hint": hint, "on": on, "default": default, "custom": on != default}

    account = db.execute("SELECT 1 FROM users WHERE member_id = ?", (member["id"],)).fetchone()
    photo = url_for("member_photo", member_id=member["id"]) if photo_file_ok(member["photo_filename"]) else ""
    return {
        "member": {
            "id": member["id"], "name": member_full_name(member), "code": member["member_code"],
            "voice": member["voice_name"], "position": member["position_name"],
            "status": member["status"], "status_label": MEMBER_STATUSES.get(member["status"], member["status"]),
            "initials": ((member["first_name"][:1] + member["last_name"][:1]) or "?").upper(),
            "photo": photo, "has_login": account is not None,
        },
        "panels": [item(key, label, hint) for key, label, hint in PANEL_PERMISSIONS],
        "actions": [item(key, label, hint) for key, label, hint in ACTION_PERMISSIONS],
        "voices": [{"id": v["id"], "name": v["name"], "own": v["id"] == member["voice_section_id"],
                    "items": [{**item(f"vfile.{a}:{v['id']}", al), "action": a} for a, al in VOICE_FILE_PERMISSIONS]}
                   for v in voices],
        "custom_count": sum(1 for key in labels if key in overrides and overrides[key] != permission_baseline(member, key)),
    }


def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/action-center/members")
@permission_required("members.access")
def action_center_search():
    """Type-ahead for the sidebar: active (not archived) members by name, Member ID or voice section."""
    db = get_db()
    q = collapse(request.args.get("q"))[:80]
    where, params = ["m.deleted_at IS NULL"], []
    for token in q.split()[:6]:
        like = "%" + like_escape(token) + "%"
        fields = ["m.first_name", "m.middle_name", "m.last_name", "m.member_code", "vs.name"]
        where.append("(" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields) + ")")
        params += [like] * len(fields)
    rows = db.execute(
        "SELECT m.id, m.member_code, m.first_name, m.middle_name, m.last_name, m.photo_filename, "
        "vs.name AS voice_name, cp.name AS position_name FROM members m "
        "JOIN voice_sections vs ON vs.id = m.voice_section_id JOIN choir_positions cp ON cp.id = m.position_id "
        f"WHERE {' AND '.join(where)} {ORDER_BY_NAME} LIMIT 8", params).fetchall()
    return no_store(jsonify(ok=True, results=[{
        "id": r["id"], "name": member_full_name(r), "code": r["member_code"], "voice": r["voice_name"],
        "position": r["position_name"], "initials": ((r["first_name"][:1] + r["last_name"][:1]) or "?").upper(),
        "photo": url_for("member_photo", member_id=r["id"]) if photo_file_ok(r["photo_filename"]) else "",
    } for r in rows]))


@app.route("/action-center/members/<int(max=2147483647):member_id>", methods=["GET", "POST"])
@permission_required("members.access")
def action_center_member(member_id):
    """GET: one member's switches. POST: save them (every switch that is ON is sent as 'on'; action=reset clears overrides)."""
    db = get_db()
    member = fetch_member(db, member_id, False, False)          # 404 if missing or archived
    if request.method == "GET":
        return no_store(jsonify(ok=True, **access_payload(db, member)))

    name = member_full_name(member)
    voices = access_voices(db, member)
    labels = access_labels(voices)
    before = member_overrides(db, member_id)
    try:
        if request.form.get("action") == "reset":
            db.execute("DELETE FROM member_permissions WHERE member_id = ?", (member_id,))
            log_activity(db, "member.access_reset", detail=name, actor_user_id=g.user["id"],
                         target_type="member", target_id=member_id)
            message = f"{name} is back to the defaults of their choir position."
        else:
            wanted = set(request.form.getlist("on")) & set(labels)       # unknown keys are dropped, never stored
            granted, revoked, rows = [], [], []
            for key in sorted(labels):
                default = permission_baseline(member, key)
                was, now = before.get(key, default), key in wanted
                if now != was:
                    (granted if now else revoked).append(labels[key])
                if now != default:
                    rows.append((member_id, key, int(now), g.user["id"]))
            if not granted and not revoked:
                return no_store(jsonify(ok=True, message="No changes to save.", **access_payload(db, member)))
            marks = ",".join("?" * len(labels))
            db.execute(f"DELETE FROM member_permissions WHERE member_id = ? AND permission IN ({marks})",
                       (member_id, *sorted(labels)))
            db.executemany("INSERT INTO member_permissions (member_id, permission, granted, updated_by) VALUES (?, ?, ?, ?)", rows)
            parts = ([f"granted: {', '.join(granted)}"] if granted else []) + ([f"revoked: {', '.join(revoked)}"] if revoked else [])
            log_activity(db, "member.access_changed", detail=f"{name} ({'; '.join(parts)})"[:400],
                         actor_user_id=g.user["id"], target_type="member", target_id=member_id)
            message = f"Access saved for {name}."
        db.commit()
    except sqlite3.Error:
        db.rollback()
        return no_store(jsonify(ok=False, message="The changes could not be saved. Nothing was changed.")), 500
    return no_store(jsonify(ok=True, message=message, **access_payload(db, member)))


# ------------------------------------------------------------------- start --

def open_in_brave(url):
    """Open the app in Brave and print what happened."""
    import subprocess
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    for path in candidates:
        if os.path.exists(path):
            subprocess.Popen([path, url])
            print(f"Opening Brave: {path}")
            return
    print("Brave not found in the usual folders. Trying the Windows 'start' command...")
    subprocess.Popen(f'start "" brave "{url}"', shell=True)


init_db()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    # In debug mode Flask restarts itself once; only open the browser the first time.
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Timer(1.0, open_in_brave, args=[f"http://{host}:{port}/"]).start()

    app.run(host=host, port=port, debug=debug)