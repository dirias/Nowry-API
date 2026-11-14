"""
A personal invitation: one address, one account, one use (ADR-077, FR-019).

The rules, with no database and no clock of their own: how a token is made and
recognised, what a new row holds, what state a row is in at a given moment, and
the one query that spends it. `app/services/beta_invites.py` runs them against
Mongo; the auth dependency and the admin calls go through that.

The token is returned once, at creation. Only its SHA-256 is stored, so reading
the database never yields a working link.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

#: What a personal token starts with; a cohort code never does.
TOKEN_PREFIX: str = "nwi_"

#: How long an invitation lasts unless the call or the environment says otherwise.
DEFAULT_TTL_DAYS: int = 14
MAX_TTL_DAYS: int = 90

STATUS_OPEN: str = "open"
STATUS_USED: str = "used"
STATUS_REVOKED: str = "revoked"
#: Read from `expires_at`, never stored.
STATE_EXPIRED: str = "expired"
STATES = (STATUS_OPEN, STATUS_USED, STATUS_REVOKED, STATE_EXPIRED)


def make_token() -> str:
    """A new token: the prefix and 32 random bytes, URL-safe."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def is_personal_token(value: Optional[str]) -> bool:
    """Whether a header value is shaped like a personal token rather than a cohort code."""
    return bool(value) and value.strip().startswith(TOKEN_PREFIX)


def hash_token(token: str) -> str:
    """What is stored in place of the token."""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def normalize_email(email: Optional[str]) -> str:
    """Addresses compare exactly after trimming and lowercasing; nothing cleverer."""
    return (email or "").strip().lower()


def ttl_days(requested: Optional[int] = None) -> int:
    """The life of a new invitation in days: the call's, else `BETA_INVITE_TTL_DAYS`, else 14."""
    if requested is not None:
        return max(1, min(int(requested), MAX_TTL_DAYS))
    raw = (os.getenv("BETA_INVITE_TTL_DAYS") or "").strip()
    try:
        return max(1, min(int(raw), MAX_TTL_DAYS)) if raw else DEFAULT_TTL_DAYS
    except ValueError:
        return DEFAULT_TTL_DAYS


def new_invite_document(
    email: str, cohort: str, token: str, created_by: str, now: datetime, days: Optional[int] = None, note: Optional[str] = None,
    locale: Optional[str] = None,
) -> Dict[str, Any]:
    """The row a new invitation is stored as. The token itself is not in it.

    `locale` is the language its letter is written in; `sent_at`, `send_count`
    and `last_error` record the letter (INVITE-008). A row made without a
    letter (the single-address call) keeps them empty.
    """
    return {
        "token_hash": hash_token(token),
        "email": normalize_email(email),
        "cohort": cohort,
        "status": STATUS_OPEN,
        "note": note,
        "created_at": now,
        "created_by": created_by,
        "expires_at": now + timedelta(days=ttl_days(days)),
        "used_at": None,
        "used_by": None,
        "used_by_uid": None,
        "revoked_at": None,
        "revoked_by": None,
        "locale": locale,
        "lifetime_days": ttl_days(days),
        "sent_at": None,
        "send_count": 0,
        "last_error": None,
    }


def _aware(moment: datetime) -> datetime:
    """Mongo hands dates back without a zone; they are UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def invite_state(row: Dict[str, Any], now: datetime) -> str:
    """`open`, `used`, `revoked` or `expired`. Used and revoked outlast the expiry."""
    status = row.get("status")
    if status in (STATUS_USED, STATUS_REVOKED):
        return status
    return STATE_EXPIRED if _aware(row["expires_at"]) <= now else STATUS_OPEN


def is_usable(row: Optional[Dict[str, Any]], now: datetime) -> bool:
    """Whether a row could still make an account. No row is not usable."""
    return row is not None and invite_state(row, now) == STATUS_OPEN


def claim_query(token: str, email: str, firebase_uid: str, now: datetime) -> Dict[str, Any]:
    """The filter that spends an invitation, in one atomic update.

    It matches a row only if the token is this one, the address is this one,
    the day has not passed, and the row is open. A row already spent by this
    same uid matches too: a sign-up fires two requests (QA-024) and the second
    must not be refused by the first.
    """
    return {
        "token_hash": hash_token(token),
        "email": normalize_email(email),
        "$or": [
            {"status": STATUS_OPEN, "expires_at": {"$gt": now}},
            {"status": STATUS_USED, "used_by_uid": firebase_uid},
        ],
    }


def claim_update(firebase_uid: str, now: datetime) -> Dict[str, Any]:
    """What the claim writes: spent, by whom, when."""
    return {"$set": {"status": STATUS_USED, "used_by_uid": firebase_uid, "used_at": now}}


def release_query(invite_id: Any, firebase_uid: str) -> Dict[str, Any]:
    """The filter that gives an invitation back when the account could not be written."""
    return {"_id": invite_id, "status": STATUS_USED, "used_by_uid": firebase_uid, "used_by": None}


RELEASE_UPDATE: Dict[str, Any] = {"$set": {"status": STATUS_OPEN, "used_by_uid": None, "used_at": None}}


def state_filter(state: Optional[str], now: datetime) -> Dict[str, Any]:
    """The Mongo filter for the rows in one state; empty for all of them."""
    if state == STATUS_OPEN:
        return {"status": STATUS_OPEN, "expires_at": {"$gt": now}}
    if state == STATE_EXPIRED:
        return {"status": STATUS_OPEN, "expires_at": {"$lte": now}}
    if state in (STATUS_USED, STATUS_REVOKED):
        return {"status": state}
    return {}


def invite_link(token: str) -> str:
    """The link to send. The token is its only parameter; the address is never in it."""
    base = os.environ.get("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{base}/register?invite={token}"
