"""
Read-only: active accounts whose stored Firebase id no longer exists (SIGNIN-001, FR-030, ADR-083).

For every active account (no `deleted_at`), asks Firebase, 100 ids at a time,
whether its `firebase_uid` still exists. Prints counts only, split by whether
Firebase still holds an identity under the account's email (so the next
sign-in re-links it) or not (Google: re-links at the next Google sign-in, a
new identity being made then; password: support recreates the Firebase user).

Nothing is written. No address, name or id is printed.

    railway run --environment development --service Nowry -- .venv/bin/python -m scripts.count_stale_firebase_uids
    railway run --environment Production  --service Nowry -- .venv/bin/python -m scripts.count_stale_firebase_uids
"""
from __future__ import annotations

import asyncio
from typing import Dict, List

from firebase_admin import auth

from app.config.database import users_collection
from app.config.firebase_config import initialize_firebase

#: Firebase's limit for one get_users call.
BATCH = 100
#: The most accounts read; far above any environment today.
LIMIT = 50_000


def _missing_uids(uids: List[str]) -> set:
    missing: set = set()
    for start in range(0, len(uids), BATCH):
        chunk = uids[start:start + BATCH]
        result = auth.get_users([auth.UidIdentifier(uid) for uid in chunk])
        missing.update(identifier.uid for identifier in result.not_found)
    return missing


def _emails_with_identity(emails: List[str]) -> set:
    found: set = set()
    for start in range(0, len(emails), BATCH):
        chunk = emails[start:start + BATCH]
        result = auth.get_users([auth.EmailIdentifier(email) for email in chunk])
        found.update((user.email or "").lower() for user in result.users)
    return found


async def main() -> Dict[str, int]:
    initialize_firebase()
    rows = await users_collection.find(
        {"deleted_at": None}, {"firebase_uid": 1, "email": 1, "signup_provider": 1}
    ).to_list(length=LIMIT)
    with_uid = [row for row in rows if row.get("firebase_uid")]
    missing = await asyncio.to_thread(_missing_uids, [row["firebase_uid"] for row in with_uid])
    stale = [row for row in with_uid if row["firebase_uid"] in missing]
    emails = [row["email"] for row in stale if row.get("email")]
    held = await asyncio.to_thread(_emails_with_identity, emails) if emails else set()
    counts = {
        "active_accounts": len(rows),
        "no_firebase_uid": len(rows) - len(with_uid),
        "stale_firebase_uid": len(stale),
        "stale_email_held_by_another_identity": sum(1 for row in stale if (row.get("email") or "").lower() in held),
        "stale_made_with_google": sum(1 for row in stale if row.get("signup_provider") == "google.com"),
        "stale_made_with_password": sum(1 for row in stale if row.get("signup_provider") == "password"),
        "stale_provider_unknown": sum(1 for row in stale if row.get("signup_provider") not in ("google.com", "password")),
    }
    for name, value in counts.items():
        print(f"{name}: {value}")
    return counts


if __name__ == "__main__":
    asyncio.run(main())
