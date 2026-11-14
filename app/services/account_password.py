"""
Passwords, changed and set through the API (PASS-001..003, ADR-092).

    check_current(email, password)   Firebase says whether it is the account's password
    providers(uid)                   the Firebase sign-in methods ('password', 'google.com')
    set_password(uid, password)      the Admin SDK sets it (adds it to a Google-only account)
    revocation_time(uid)             when Firebase ended the account's older sessions
    cut_sessions(user_id, uid, at)   our side of it: tokens signed in before `at` are refused (ADR-076)
    send_notice(user, locale)        `account.password`, once per change

No password is logged, stored or returned.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional, Set

import httpx
from bson import ObjectId
from firebase_admin import auth as firebase_admin_auth

from app.config import database
from app.services.email import send_email
from app.utils.logger import get_logger

logger = get_logger(__name__)

MIN_LENGTH = 8
WRONG_LIMIT = 5
WRONG_WINDOW = 3600
SIGN_IN_ENDPOINT = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
WRONG_CODES = ("INVALID_PASSWORD", "INVALID_LOGIN_CREDENTIALS", "INVALID_CREDENTIAL")


class PasswordCheckUnavailable(RuntimeError):
    """Firebase could not be asked, or refused for its own reasons (no key, throttled)."""


async def check_current(email: str, password: str) -> bool:
    """True when `password` is the account's current one; False when it is not."""
    key = (os.getenv("FIREBASE_WEB_API_KEY") or "").strip()
    if not key:
        raise PasswordCheckUnavailable("no FIREBASE_WEB_API_KEY")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                SIGN_IN_ENDPOINT, params={"key": key}, json={"email": email, "password": password, "returnSecureToken": False}
            )
    except Exception as exc:  # noqa: BLE001
        raise PasswordCheckUnavailable(type(exc).__name__) from exc
    if response.status_code < 300:
        return True
    message = ((response.json() or {}).get("error") or {}).get("message", "") if response.content else ""
    if any(message.startswith(code) for code in WRONG_CODES):
        return False
    raise PasswordCheckUnavailable(message.split(" ")[0] or str(response.status_code))


async def providers(uid: str) -> Set[str]:
    record = await asyncio.to_thread(firebase_admin_auth.get_user, uid)
    return {entry.provider_id for entry in record.provider_data}


async def set_password(uid: str, password: str) -> None:
    await asyncio.to_thread(firebase_admin_auth.update_user, uid, password=password)


async def revocation_time(uid: str) -> int:
    """Firebase's own cut after a password change, in seconds; now when it has none."""
    record = await asyncio.to_thread(firebase_admin_auth.get_user, uid)
    valid_after_ms = getattr(record, "tokens_valid_after_timestamp", None)
    return int(valid_after_ms // 1000) if valid_after_ms else int(datetime.now(timezone.utc).timestamp())


async def cut_sessions(user_id: str, uid: str, at: int) -> None:
    """Sessions signed in before `at` are refused from now on (ADR-076's mark), on this process at once."""
    from app.auth import firebase_auth

    await database.db["users"].update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"sessions_valid_after": at, "password_changed_at": datetime.now(timezone.utc)}},
    )
    firebase_auth._sessions_cut[uid] = at


async def send_notice(address: str, locale: Optional[str]) -> str:
    """The one notice for this change (FR-046). Never raises."""
    try:
        return await send_email("account.password", address, locale, {})
    except Exception as exc:  # noqa: BLE001 - the change is done
        logger.warning("password notice not sent: %s", type(exc).__name__)
        return "failed"
