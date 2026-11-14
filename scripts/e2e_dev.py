"""
The dev end-to-end suite's database helper (E2E-003, ADR-090, FR-042).

Run only through ``railway run --environment development``; it refuses any other
database. Reads are free. Writes are limited to what the suite itself created:
every address must be one of the run's aliases, ``rikydier+e2e-<run>-<case>@gmail.com``.

    python -I -m scripts.e2e_dev account <email>            the account, live and deleted
    python -I -m scripts.e2e_dev waitlist <email>           the waitlist row
    python -I -m scripts.e2e_dev make-admin <email>         is_admin on a suite account
    python -I -m scripts.e2e_dev admin-log <email>          the admin log rows about a suite address
    python -I -m scripts.e2e_dev mark-verified <email>      the account's address recorded as verified
    python -I -m scripts.e2e_dev backdate-invites <email> <hours>   its invitations' created_at and sent_at, moved back
    python -I -m scripts.e2e_dev expire-invites <email>     open invitations to that alias end now
    python -I -m scripts.e2e_dev backdate-deletion <email> <days>   the deleted record and its rows, moved back
    python -I -m scripts.e2e_dev set-xp <email> <xp>         the account's XP total, e.g. one review short of a stage
    python -I -m scripts.e2e_dev cleanup <run>              everything the run made, gone (Firebase included)

Prints one JSON object. Never prints a token or a secret.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

ALIAS = re.compile(r"^rikydier\+e2e-[a-z0-9]+-[a-z0-9-]+@gmail\.com$")
RUN = re.compile(r"^[a-z0-9]{4,16}$")
DEV_DATABASE = "nowry-db"


def refuse(message: str) -> None:
    print(json.dumps({"ok": False, "error": message}))
    sys.exit(2)


def guard_environment() -> None:
    # `railway run` withholds ENV from local processes; Railway's own name for the environment is passed.
    if os.getenv("RAILWAY_ENVIRONMENT_NAME") != "development" or os.getenv("MONGO_DB") != DEV_DATABASE:
        refuse("this helper runs only against the development database")


def guard_alias(email: str) -> str:
    email = email.strip().lower()
    if not ALIAS.match(email):
        refuse("not a suite alias")
    return email


def iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    return value


def alias_query(field: str, email: str) -> Dict[str, Any]:
    return {field: {"$regex": f"^{re.escape(email)}$", "$options": "i"}}


async def account(db, email: str) -> Dict[str, Any]:
    from app.services.account_deletion import OWNED

    live = await db.users.find_one({**alias_query("email", email), "deleted_at": None})
    out: Dict[str, Any] = {"live": None, "deleted": []}
    if live:
        prefs = live.get("preferences") or {}
        uid = str(live["_id"])
        out["live"] = {
            "email_verified": live.get("email_verified"),
            "beta_cohort": (live.get("beta") or {}).get("cohort"),
            "pet_active": (prefs.get("pet") or {}).get("pet_active"),
            "pet_revealed": (prefs.get("pet") or {}).get("pet_revealed"),
            "roaming": (prefs.get("agent") or {}).get("roaming_enabled"),
            "is_admin": live.get("is_admin") is True,
            "previous_account_dismissed": bool((live.get("notices") or {}).get("previous_account_dismissed_at")),
            "created_at": iso(live.get("created_at")),
            "decks": await db.decks.count_documents({"user_id": uid, "deleted_at": None}),
        }
    deleted = await db.users.find({**alias_query("deleted_identity.email", email), "deleted_at": {"$ne": None}}).to_list(length=10)
    for row in deleted:
        uid = str(row["_id"])
        marked = {}
        for name, owner in OWNED:
            count = await db[name].count_documents({owner: uid, "deleted_at": {"$ne": None}})
            if count:
                marked[name] = count
        out["deleted"].append({
            "deleted_at": iso(row.get("deleted_at")),
            "email_released": row.get("email", "").endswith("@deleted.nowry.invalid"),
            "notice": (row.get("deletion_notice") or {}).get("outcome"),
            "marked": marked,
        })
    return out


async def waitlist(db, email: str) -> Dict[str, Any]:
    row = await db.waitlist.find_one(alias_query("email", email))
    return {"row": None if not row else {"status": row.get("status"), "confirmations": row.get("confirmation_sent_at") is not None}}


async def make_admin(db, email: str) -> Dict[str, Any]:
    result = await db.users.update_one({**alias_query("email", email), "deleted_at": None}, {"$set": {"is_admin": True}})
    return {"changed": result.modified_count}


async def expire_invites(db, email: str) -> Dict[str, Any]:
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    result = await db.invites.update_many({**alias_query("email", email), "status": "open"}, {"$set": {"expires_at": past}})
    return {"changed": result.modified_count}


async def backdate_invites(db, email: str, hours: float) -> Dict[str, Any]:
    """Move a suite address's invitations back in time, so Resend's hourly limit is passed (E2E-ADMIN-1)."""
    if hours <= 0 or hours > 48:
        refuse("hours must be between 0 and 48")
    rows = await db.invites.find(alias_query("email", email)).to_list(length=20)
    shift = timedelta(hours=hours)
    for row in rows:
        moved = {name: row[name] - shift for name in ("created_at", "sent_at") if isinstance(row.get(name), datetime)}
        if moved:
            await db.invites.update_one({"_id": row["_id"]}, {"$set": moved})
    return {"changed": len(rows)}


async def mark_verified(db, email: str) -> Dict[str, Any]:
    """Record a suite account's address as verified (ADR-076), without spending a verification email (E2E-ADMIN-3)."""
    result = await db.users.update_one({**alias_query("email", email), "deleted_at": None}, {"$set": {"email_verified": True}})
    return {"changed": result.modified_count}


async def admin_log(db, email: str) -> Dict[str, Any]:
    """The admin log rows about a suite address, oldest first: action and outcome only (E2E-ADMIN-1)."""
    rows = await db.admin_actions.find(alias_query("target_title", email)).sort("created_at", 1).to_list(length=50)
    return {"actions": [row.get("action") for row in rows]}


async def set_xp(db, email: str, xp: int) -> Dict[str, Any]:
    """Put a suite account at `xp` total, e.g. one review short of a stage (EVO-020)."""
    if xp < 0:
        refuse("xp cannot be negative")
    result = await db.users.update_one({**alias_query("email", email), "deleted_at": None}, {"$set": {"agent.xp": xp}})
    return {"changed": result.modified_count, "xp": xp}


async def backdate_deletion(db, email: str, days: float) -> Dict[str, Any]:
    from app.services.account_deletion import OWNED

    rows = await db.users.find({**alias_query("deleted_identity.email", email), "deleted_at": {"$ne": None}}).to_list(length=10)
    moved = 0
    for row in rows:
        when = datetime.now(timezone.utc) - timedelta(days=days)
        uid = str(row["_id"])
        await db.users.update_one({"_id": row["_id"]}, {"$set": {"deleted_at": when}})
        for name, owner in OWNED:
            result = await db[name].update_many({owner: uid, "deleted_at": {"$ne": None}}, {"$set": {"deleted_at": when}})
            moved += result.modified_count
        moved += 1
    return {"records": len(rows), "moved": moved}


async def cleanup(db, run: str) -> Dict[str, Any]:
    from app.services.account_deletion import OWNED

    if not RUN.match(run):
        refuse("bad run id")
    pattern = {"$regex": f"^rikydier\\+e2e-{re.escape(run)}-[a-z0-9-]+@gmail\\.com$", "$options": "i"}
    removed: Dict[str, int] = {}
    users: List[dict] = await db.users.find({"$or": [{"email": pattern}, {"deleted_identity.email": pattern}]}).to_list(length=200)
    emails = set()
    for user in users:
        uid = str(user["_id"])
        emails.add((user.get("deleted_identity") or {}).get("email") or user.get("email"))
        for name, owner in OWNED:
            result = await db[name].delete_many({owner: uid})
            removed[name] = removed.get(name, 0) + result.deleted_count
        await db.users.delete_one({"_id": user["_id"]})
        removed["users"] = removed.get("users", 0) + 1
    for name, field in (("waitlist", "email"), ("invites", "email"), ("dev_outbox", "to"), ("admin_actions", "target_title")):
        result = await db[name].delete_many({field: pattern})
        removed[name] = result.deleted_count
    firebase = 0
    try:
        import app.config.firebase_config  # noqa: F401 - initialises the Admin SDK from the environment
        from firebase_admin import auth as firebase_auth

        page = firebase_auth.list_users()
        while page:
            for record in page.users:
                # The run's aliases, and its one non-suite identity (E2E-007's outsider).
                if record.email and (re.match(pattern["$regex"], record.email, re.I) or record.email.lower() == f"rikydier+nosuite-{run}@gmail.com"):
                    firebase_auth.delete_user(record.uid)
                    firebase += 1
            page = page.get_next_page()
    except Exception as exc:  # noqa: BLE001 - report it; the database part is done
        removed["firebase_error"] = type(exc).__name__
    removed["firebase"] = firebase
    return {"removed": removed}


async def main(argv: List[str]) -> None:
    guard_environment()
    if not argv:
        refuse("no command")
    from app.config import database

    db = database.db
    command, args = argv[0], argv[1:]
    if command == "cleanup":
        result = await cleanup(db, args[0] if args else "")
    else:
        email = guard_alias(args[0] if args else "")
        if command == "account":
            result = await account(db, email)
        elif command == "waitlist":
            result = await waitlist(db, email)
        elif command == "make-admin":
            result = await make_admin(db, email)
        elif command == "expire-invites":
            result = await expire_invites(db, email)
        elif command == "backdate-invites":
            result = await backdate_invites(db, email, float(args[1]))
        elif command == "mark-verified":
            result = await mark_verified(db, email)
        elif command == "admin-log":
            result = await admin_log(db, email)
        elif command == "set-xp":
            result = await set_xp(db, email, int(args[1]))
        elif command == "backdate-deletion":
            result = await backdate_deletion(db, email, float(args[1]))
        else:
            refuse("unknown command")
            return
    print(json.dumps({"ok": True, **result}))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
