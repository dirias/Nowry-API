"""
An email is someone's word until Firebase has verified it (ADR-076, FR-018).

Two pure rules the auth dependency applies to every request:

- ``verification_update``: what the account document should record for the
  token in hand. ``email_verified`` follows Firebase's flag and never goes back
  to ``False``. When an account stored as unverified is verified by a sign-in
  that is not its own password (the owner's Google sign-in landing in an
  account someone else registered first), ``sessions_valid_after`` takes that
  sign-in's ``auth_time``.
- ``session_is_cut``: whether a token belongs to a session older than that
  mark. A refreshed token keeps its original ``auth_time``, so the earlier
  session is refused for good and the one that set the mark is not.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

PASSWORD_PROVIDER = "password"


def _auth_time(token_data: Dict[str, Any]) -> Optional[int]:
    """The token's sign-in time as whole seconds, or None when absent or unreadable."""
    value = token_data.get("auth_time")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def verification_update(user: Dict[str, Any], token_data: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """The fields to ``$set`` on ``user`` for this token; empty when nothing changes."""
    stored = user.get("email_verified")
    verified: bool = bool(token_data.get("email_verified"))
    if verified and stored is not True:
        update: Dict[str, Any] = {"email_verified": True, "email_verified_at": now or datetime.now(timezone.utc)}
        signed_in_at = _auth_time(token_data)
        by_another_sign_in: bool = token_data.get("sign_in_provider") not in (None, PASSWORD_PROVIDER)
        if stored is False and by_another_sign_in and signed_in_at is not None:
            update["sessions_valid_after"] = signed_in_at
        return update
    if not verified and stored is None:
        return {"email_verified": False}
    return {}


def session_is_cut(valid_after: Any, token_data: Dict[str, Any]) -> bool:
    """True when the token's sign-in is older than the account's ``sessions_valid_after``."""
    if not valid_after:
        return False
    signed_in_at = _auth_time(token_data)
    return signed_in_at is None or signed_in_at < int(valid_after)
