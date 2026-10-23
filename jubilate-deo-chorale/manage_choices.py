"""Change VOICE SECTIONS and CHOIR POSITIONS in the database you are really using.

Run it from the project folder with the virtual environment active.

THE EASY WAY (recommended)
    1. Edit the list in app.py:  DEFAULT_VOICE_SECTIONS  and/or  DEFAULT_CHOIR_POSITIONS
       (search for [VOICE SECTION] or [CHOIR POSITION]).
    2. Run:    python manage_choices.py sync
    3. Read what it plans to do, type  yes,  then refresh the Members page.

    Why is step 2 needed? These two lists live in the database. The lists in app.py only
    fill an EMPTY database the very first time, so editing them alone changes nothing.
    "sync" makes the database match your list:
        - names that are new in your list are added
        - names that are missing from your list are REMOVED if nobody uses them, or
          RETIRED (hidden from the forms) if members still have them, so nobody loses data
        - the order follows your list

ONE CHANGE AT A TIME
    python manage_choices.py        asks what to do: add, rename, retire, bring back, or sync.

Membership STATUSES (Active, Temporarily Inactive, Inactive) are not managed here: they are
part of how the system works. See [MEMBERSHIP STATUS] in app.py.
"""

import sqlite3
import sys

from app import (
    DEFAULT_CHOIR_POSITIONS, DEFAULT_VOICE_SECTIONS, app, get_db, has_letters_ok, log_activity,
)

NAME_MAX = 40

# Table and column names come from this fixed list, never from what you type.
KINDS = {
    "1": {"table": "voice_sections", "noun": "voice section", "code": "voice_section",
          "member_column": "voice_section_id", "wanted": DEFAULT_VOICE_SECTIONS},
    "2": {"table": "choir_positions", "noun": "choir position", "code": "choir_position",
          "member_column": "position_id", "wanted": DEFAULT_CHOIR_POSITIONS},
}


# ----------------------------------------------------------------- showing the lists --

def people_count(db, kind, choice_id):
    """How many members (including archived ones) have this choice."""
    return db.execute(
        f"SELECT COUNT(*) FROM members WHERE {kind['member_column']} = ?", (choice_id,)
    ).fetchone()[0]


def rows_of(db, kind):
    return db.execute(
        f"SELECT id, name, is_active, sort_order FROM {kind['table']} ORDER BY sort_order, name"
    ).fetchall()


def show(db, kind):
    print(f"\nCurrent {kind['noun']}s:")
    for row in rows_of(db, kind):
        state = "" if row["is_active"] else "   (retired)"
        print(f"  {row['id']:>3}. {row['name']}  - {people_count(db, kind, row['id'])} member(s){state}")


def clean_name(raw):
    name = " ".join(str(raw).split())
    if not name:
        return None, "empty"
    if len(name) > NAME_MAX:
        return None, f"'{name}' is longer than {NAME_MAX} characters"
    if not has_letters_ok(name):
        return None, f"'{name}' must contain letters"
    return name, None


# ------------------------------------------------------------------------- sync --

def plan_sync(db, kind):
    """Work out what it would take to make the database match the list in app.py."""
    wanted, seen, problems = [], set(), []
    for raw in kind["wanted"]:
        name, problem = clean_name(raw)
        if problem:
            problems.append(problem)
        elif name.lower() not in seen:
            seen.add(name.lower())
            wanted.append(name)
    plan = {"kind": kind, "wanted": wanted, "problems": problems,
            "add": [], "reactivate": [], "respell": [], "remove": [], "retire": [], "keep_retired": []}
    if problems or not wanted:
        return plan

    existing = {row["name"].lower(): row for row in rows_of(db, kind)}
    for name in wanted:
        row = existing.get(name.lower())
        if row is None:
            plan["add"].append(name)
        else:
            if not row["is_active"]:
                plan["reactivate"].append(name)
            if row["name"] != name:
                plan["respell"].append((row["name"], name))
    for key, row in existing.items():
        if key in seen:
            continue
        people = people_count(db, kind, row["id"])
        if people == 0:
            plan["remove"].append(row["name"])
        elif row["is_active"]:
            plan["retire"].append((row["name"], people))
        else:
            plan["keep_retired"].append(row["name"])
    return plan


def plan_is_empty(plan):
    return not any(plan[key] for key in ("add", "reactivate", "respell", "remove", "retire"))


def describe(plan):
    kind = plan["kind"]
    print(f"\n--- {kind['noun'].capitalize()}s ---")
    if plan["problems"]:
        print("  Cannot sync, the list in app.py has a problem: " + "; ".join(plan["problems"]))
        return
    if not plan["wanted"]:
        print("  The list in app.py is empty. Nothing will be changed (at least one is needed).")
        return
    if plan_is_empty(plan):
        print("  Already matches the list in app.py. Nothing to do.")
        return
    for name in plan["add"]:
        print(f"  + ADD         {name}")
    for name in plan["reactivate"]:
        print(f"  + BRING BACK  {name}")
    for old, new in plan["respell"]:
        print(f"  ~ RESPELL     {old}  ->  {new}")
    for name in plan["remove"]:
        print(f"  - REMOVE      {name}   (nobody uses it)")
    for name, people in plan["retire"]:
        print(f"  - RETIRE      {name}   ({people} member(s) still have it, so it is only hidden from the forms)")
    if kind["code"] == "choir_position" and "member" not in [n.lower() for n in plan["wanted"]]:
        print("  Note: new members are pre-selected as 'Member', which is not in your list.")


def apply_plan(db, plan):
    """Make the changes. The caller commits."""
    kind, table = plan["kind"], plan["kind"]["table"]
    order = {name.lower(): i for i, name in enumerate(plan["wanted"], start=1)}
    for name in plan["add"]:
        db.execute(f"INSERT INTO {table} (name, sort_order) VALUES (?, ?)", (name, order[name.lower()]))
    for row in rows_of(db, kind):
        key = row["name"].lower()
        if key in order:
            wanted_name = plan["wanted"][order[key] - 1]
            db.execute(f"UPDATE {table} SET name = ?, sort_order = ?, is_active = 1 WHERE id = ?",
                       (wanted_name, order[key], row["id"]))
        elif people_count(db, kind, row["id"]) == 0:
            db.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
        else:
            db.execute(f"UPDATE {table} SET is_active = 0, sort_order = ? WHERE id = ?",
                       (1000 + row["sort_order"], row["id"]))
    summary = (f"{len(plan['add'])} added, {len(plan['remove'])} removed, "
               f"{len(plan['retire'])} retired, {len(plan['reactivate'])} brought back")
    log_activity(db, f"{kind['code']}.synced", detail=f"{kind['noun']}s: {summary}", target_type=kind["code"])


def run_sync(kinds):
    with app.app_context():
        db = get_db()
        plans = [plan_sync(db, kind) for kind in kinds]
        for plan in plans:
            describe(plan)
        todo = [plan for plan in plans if not plan["problems"] and plan["wanted"] and not plan_is_empty(plan)]
        if not todo:
            print("\nNothing to change.")
            return
        print("\nMembers who already have a retired choice keep it. Nothing is deleted that anyone uses.")
        if input("Apply these changes? Type yes to continue: ").strip().lower() != "yes":
            print("Nothing was changed.")
            return
        try:
            for plan in todo:
                apply_plan(db, plan)
            db.commit()
        except sqlite3.Error as error:
            db.rollback()
            print(f"Something went wrong, so nothing was changed ({error}).")
            return
        print("\nDone. The database now matches your list. Refresh the Members page in your browser.")
        for plan in todo:
            show(db, plan["kind"])


# ------------------------------------------------------------ one change at a time --

def ask_name(noun):
    name, problem = clean_name(input(f"Name of the {noun}: "))
    if problem:
        print(f"Not accepted: {problem}.")
    return name


def pick(rows, prompt):
    raw = input(prompt).strip()
    for row in rows:
        if str(row["id"]) == raw:
            return row
    print("That number is not in the list.")
    return None


def one_change():
    print("Which list do you want to change?")
    print("  1. Voice sections (Soprano, Alto, Tenor, Bass...)")
    print("  2. Choir positions (Member, Choir Director, President...)")
    kind = KINDS.get(input("Choose 1 or 2: ").strip())
    if kind is None:
        print("Nothing changed.")
        return
    table, noun, code = kind["table"], kind["noun"], kind["code"]

    with app.app_context():
        db = get_db()
        show(db, kind)
        rows = rows_of(db, kind)
        print("\nWhat do you want to do?")
        print("  a. Add a new one        b. Rename one")
        print("  c. Retire one           d. Bring back a retired one")
        print("  e. Make the database match the list in app.py (the easy way)")
        action = input("Choose a, b, c, d or e: ").strip().lower()
        if action == "e":
            run_sync([kind])
            return

        try:
            if action == "a":
                name = ask_name(noun)
                if not name:
                    return
                order = db.execute(f"SELECT COALESCE(MAX(sort_order), 0) + 1 FROM {table}").fetchone()[0]
                db.execute(f"INSERT INTO {table} (name, sort_order) VALUES (?, ?)", (name, order))
                log_activity(db, f"{code}.added", detail=name, target_type=code)
                message = f"Added the {noun} '{name}'."
            elif action == "b":
                row = pick(rows, f"Number of the {noun} to rename: ")
                name = ask_name(f"new name for '{row['name']}'") if row else None
                if not name:
                    return
                db.execute(f"UPDATE {table} SET name = ? WHERE id = ?", (name, row["id"]))
                log_activity(db, f"{code}.renamed", detail=f"{row['name']} to {name}", target_type=code, target_id=row["id"])
                message = f"Renamed '{row['name']}' to '{name}'."
            elif action in ("c", "d"):
                row = pick(rows, f"Number of the {noun} to {'retire' if action == 'c' else 'bring back'}: ")
                if row is None:
                    return
                if action == "c" and row["is_active"] and sum(1 for r in rows if r["is_active"]) <= 1:
                    print(f"You need at least one active {noun}, so the last one cannot be retired.")
                    return
                db.execute(f"UPDATE {table} SET is_active = ? WHERE id = ?", (0 if action == "c" else 1, row["id"]))
                log_activity(db, f"{code}.{'retired' if action == 'c' else 'restored'}", detail=row["name"],
                             target_type=code, target_id=row["id"])
                message = f"{'Retired' if action == 'c' else 'Brought back'} '{row['name']}'."
            else:
                print("Nothing changed.")
                return
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            print(f"A {noun} with that name already exists. Nothing was changed.")
            return

        print("\n" + message)
        show(db, kind)
        print("\nRefresh the Members page in your browser to see it.")


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "sync":
        run_sync([KINDS["1"], KINDS["2"]])
    else:
        one_change()


if __name__ == "__main__":
    main()