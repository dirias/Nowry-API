"""
One-off migration: copy one user's Annual Planning data — plan, focus areas,
priorities, goals (with embedded milestones), activities, quarter reports,
daily routine — from a source MongoDB deployment to a target one. Written to
move a single account's data from a dev database onto a freshly created
production cluster, where the account was already signed into under a
*different* email than the source account used.

Background
----------
`user_id` on annual_plans/daily_routines (and transitively, everything
nested under a plan) is not the Firebase UID — it is the string form of the
MongoDB `_id` of the matching `users` document (app/auth/firebase_auth.py:135).
Source and target are different accounts (different email, different
Firebase UID, different `users._id`), and the target account already has its
own `users` document from a real login — so this migration never touches the
`users` collection. Instead it looks up the target user's existing `_id` by
email and rewrites every `user_id` field in the copied documents from the
source id to the target id before writing. Every other cross-reference
(`annual_plan_id`, `focus_area_id`, `goal_id`) is left as-is — those `_id`s
are freshly copied from source, not reused from anything already on target,
so they cannot collide with the target account's existing documents.

Only active records are copied (`deleted_at: None`), mirroring the filters
`app/routers/annual_planning.py` itself uses to decide what is live.

Standalone script, NOT wired into app startup. Reads two full Mongo
connection strings plus the two account emails from `~/.nowry_migration.env`
(SOURCE_MONGO_URI/DB, TARGET_MONGO_URI/DB, SOURCE_USER_EMAIL,
TARGET_USER_EMAIL) — kept outside the repo so credentials are never
committed or logged. Run from `Nowry-API`:

    .venv/bin/python -m app.migrations.migrate_user_annual_planning            # dry run
    .venv/bin/python -m app.migrations.migrate_user_annual_planning --apply    # write

Dry run is the default and writes nothing — it reads from source, then reads
from target only to check for conflicts: colliding `_id`s, an annual plan
already on target for the same year (the unique (user_id, year) index would
reject it), or a daily_routines document already on target for this user
(the unique user_id index only allows one). Any conflict aborts the run
before anything is written, so re-running before `--apply` is safe and a
completed migration cannot be silently doubled.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Kept outside the repo on purpose — never read into chat, never committed.
MIGRATION_ENV_PATH = Path.home() / ".nowry_migration.env"

#: Defaults — override with SOURCE_USER_EMAIL / TARGET_USER_EMAIL in the env
#: file if this script is ever reused for a different pair of accounts.
DEFAULT_SOURCE_USER_EMAIL = "rikydier@gmail.com"
DEFAULT_TARGET_USER_EMAIL = "didier.irias@gmail.com"

#: Fields, per collection, that hold the owning account's `users._id` as a
#: string and must be rewritten from the source id to the target id. Every
#: other id field (`annual_plan_id`, `focus_area_id`, `goal_id`, and each
#: document's own `_id`) is copied unchanged.
USER_ID_FIELDS = {
    "annual_plans": "user_id",
    "daily_routines": "user_id",
}

#: Bounded — never an unbounded .find(). One account's data should never
#: approach this, but a runaway query must not hang the script or exhaust
#: memory (see Nowry-API/CLAUDE.md: always .to_list(length=N)).
LIMIT = 2000

#: Migrated in this order: each entry's parent must already be queued before
#: it, since a child's filter is built from a parent document's `_id`.
CHILD_COLLECTIONS = (
    "annual_plans",
    "focus_areas",
    "priorities",
    "goals",
    "activities",
    "quarter_reports",
    "daily_routines",
)


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise SystemExit(f"{key} is not set in {MIGRATION_ENV_PATH}")
    return value


async def _connect(uri_key: str, db_key: str) -> Any:
    uri = _require_env(uri_key)
    db_name = _require_env(db_key)
    client: AsyncIOMotorClient = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
    await client.admin.command("ping")
    return client[db_name]


async def _gather(source_db: Any, user_email: str) -> dict:
    """Read-only. Collects one user's full Annual Planning tree from source."""
    user = await source_db.users.find_one({"email": user_email})
    if not user:
        raise SystemExit(f"No user found in source DB with email {user_email}")
    uid = str(user["_id"])

    data: dict = {
        "user": user,
        "annual_plans": [],
        "focus_areas": [],
        "priorities": [],
        "goals": [],
        "activities": [],
        "quarter_reports": [],
        "daily_routines": [],
    }

    data["annual_plans"] = await source_db.annual_plans.find(
        {"user_id": uid, "deleted_at": None}
    ).to_list(length=LIMIT)

    for plan in data["annual_plans"]:
        pid = str(plan["_id"])

        data["quarter_reports"] += await source_db.quarter_reports.find(
            {"annual_plan_id": pid, "deleted_at": None}
        ).to_list(length=LIMIT)

        areas = await source_db.focus_areas.find(
            {"annual_plan_id": pid, "deleted_at": None}
        ).to_list(length=LIMIT)
        data["focus_areas"] += areas

        for area in areas:
            aid = str(area["_id"])

            data["priorities"] += await source_db.priorities.find(
                {"focus_area_id": aid, "deleted_at": None}
            ).to_list(length=LIMIT)

            goals = await source_db.goals.find(
                {"focus_area_id": aid, "deleted_at": None}
            ).to_list(length=LIMIT)
            data["goals"] += goals

            for goal in goals:
                gid = str(goal["_id"])
                data["activities"] += await source_db.activities.find(
                    {"goal_id": gid, "deleted_at": None}
                ).to_list(length=LIMIT)

    routine = await source_db.daily_routines.find_one({"user_id": uid})
    data["daily_routines"] = [routine] if routine else []

    return data


async def _resolve_target_user(target_db: Any, target_email: str) -> dict:
    """Read-only. The target account must already exist — this migration never
    writes to the `users` collection, since the target already has its own
    real account document (subscription state, Stripe customer, etc.) that
    must not be touched."""
    user = await target_db.users.find_one({"email": target_email})
    if not user:
        raise SystemExit(
            f"No user found in target DB with email {target_email} — log into "
            "the prod app with this account at least once before migrating."
        )
    return user


def _rewrite_user_ids(data: dict, source_uid: str, target_uid: str) -> None:
    """Mutates `data` in place: swaps the source account's id for the target
    account's id on every field in USER_ID_FIELDS. Nothing else is touched —
    `_id` and the plan/focus-area/goal cross-references are copied as-is."""
    for name, field in USER_ID_FIELDS.items():
        for doc in data[name]:
            if doc.get(field) != source_uid:
                raise SystemExit(
                    f"{name} document {doc['_id']} has unexpected {field}="
                    f"{doc.get(field)!r} (expected {source_uid!r}) — aborting "
                    "rather than guessing."
                )
            doc[field] = target_uid


def _is_empty_default_routine(doc: dict) -> bool:
    """True only for an untouched auto-created template: every activity list
    empty and no completion history. Anything else is real user data and must
    never be silently replaced."""
    return (
        not doc.get("morning_routine")
        and not doc.get("afternoon_routine")
        and not doc.get("evening_routine")
        and not doc.get("daily_completions")
    )


async def _find_conflicts(target_db: Any, data: dict, target_uid: str) -> tuple:
    """Read-only. Returns (conflicts, routine_id_to_replace).

    Any entry in `conflicts` aborts the run before writes. The one exception
    that does NOT abort: an existing daily_routines document on target that
    is still the untouched auto-created default — that one is safe to
    replace, and its `_id` is returned so `_write` can delete it first.
    """
    conflicts = []
    routine_id_to_replace = None

    for name in CHILD_COLLECTIONS:
        ids = [doc["_id"] for doc in data[name]]
        if not ids:
            continue
        found = await target_db[name].find(
            {"_id": {"$in": ids}}, {"_id": 1}
        ).to_list(length=LIMIT)
        if found:
            conflicts.append(
                f"{name}: {len(found)} of {len(ids)} document(s) already exist on target "
                "(id collision)"
            )

    for plan in data["annual_plans"]:
        existing_plan = await target_db.annual_plans.find_one(
            {"user_id": target_uid, "year": plan["year"], "deleted_at": None}
        )
        if existing_plan:
            conflicts.append(
                f"annual_plans: target already has a plan for year {plan['year']} "
                f"(_id={existing_plan['_id']}) — the unique (user_id, year) index "
                "would reject this insert"
            )

    if data["daily_routines"]:
        existing_routine = await target_db.daily_routines.find_one({"user_id": target_uid})
        if existing_routine and _is_empty_default_routine(existing_routine):
            routine_id_to_replace = existing_routine["_id"]
            logger.info(
                f"daily_routines: target has an untouched default (_id="
                f"{routine_id_to_replace}) — will be replaced, not merged."
            )
        elif existing_routine:
            conflicts.append(
                f"daily_routines: target already has a non-empty one for this user "
                f"(_id={existing_routine['_id']}) — the unique user_id index only "
                "allows one, and it has real content so it will not be auto-replaced"
            )

    return conflicts, routine_id_to_replace


async def _write(target_db: Any, data: dict, routine_id_to_replace: Any) -> dict:
    written = {}

    if routine_id_to_replace is not None:
        result = await target_db.daily_routines.delete_one({"_id": routine_id_to_replace})
        logger.info(
            f"Deleted target's empty default daily_routines document "
            f"(_id={routine_id_to_replace}), matched={result.deleted_count}"
        )

    for name in CHILD_COLLECTIONS:
        docs = data[name]
        if docs:
            await target_db[name].insert_many(docs)
        written[name] = len(docs)

    return written


async def migrate_user_annual_planning(apply_changes: bool = False) -> None:
    mode = "APPLY" if apply_changes else "DRY RUN"
    load_dotenv(MIGRATION_ENV_PATH)
    source_email = os.environ.get("SOURCE_USER_EMAIL", DEFAULT_SOURCE_USER_EMAIL)
    target_email = os.environ.get("TARGET_USER_EMAIL", DEFAULT_TARGET_USER_EMAIL)

    source_db = await _connect("SOURCE_MONGO_URI", "SOURCE_MONGO_DB")
    target_db = await _connect("TARGET_MONGO_URI", "TARGET_MONGO_DB")

    logger.info(
        f"Starting Annual Planning migration: {source_email} (source) -> "
        f"{target_email} (target) [{mode}]."
    )

    data = await _gather(source_db, source_email)
    source_uid = str(data["user"]["_id"])

    target_user = await _resolve_target_user(target_db, target_email)
    target_uid = str(target_user["_id"])
    logger.info(f"source user _id={source_uid}, target user _id={target_uid}")

    for name in CHILD_COLLECTIONS:
        logger.info(f"{name}: {len(data[name])} document(s) found on source")

    _rewrite_user_ids(data, source_uid, target_uid)

    conflicts, routine_id_to_replace = await _find_conflicts(target_db, data, target_uid)
    if conflicts:
        logger.error("Conflicts found on target — aborting, nothing written:")
        for conflict in conflicts:
            logger.error(f"  - {conflict}")
        return

    if not apply_changes:
        logger.info("Dry run — nothing written. Re-run with --apply to write.")
        return

    written = await _write(target_db, data, routine_id_to_replace)
    logger.info(f"Migration complete. Documents written to target: {written}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy one user's Annual Planning data (plan, focus areas, priorities, "
            "goals + milestones, activities, quarter reports, daily routine) from a "
            "source Mongo deployment to a different account (by email) on a target "
            "deployment. Dry run unless --apply is passed."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to the target database. Without this flag the run is read-only.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(migrate_user_annual_planning(apply_changes=args.apply))
