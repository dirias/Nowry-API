"""
The active account holding an address (ADR-083, ADR-084).

One lookup for sign-in's email fallback and for the waitlist: the address
matched without case, a deleted account never (ADR-081).
"""
from __future__ import annotations

import re
from typing import Optional


async def active_account_by_email(users_collection, email: str) -> Optional[dict]:
    """The active account with this address, ignoring case; None for none or a deleted one.

    Exact first, on the unique index; a case-insensitive anchored match only
    when that misses.
    """
    address = (email or "").strip()
    if not address:
        return None
    user = await users_collection.find_one({"email": address, "deleted_at": None})
    if user:
        return user
    pattern = {"$regex": f"^{re.escape(address)}$", "$options": "i"}
    return await users_collection.find_one({"email": pattern, "deleted_at": None})
