"""
The beta gate (ADR-038, docs/prd-road-to-market.md FR-001..FR-003).

Three flags and a list of invite codes, all from the environment, so the beta
can be opened from a Railway variable without a deploy. Unset means off: no
mark, no gate, upgrades open, which is what development and the test suite
must see.

    BETA_ACTIVE=true              the clients show the Beta mark
    BETA_INVITE_REQUIRED=true     a new account needs a code
    BETA_UPGRADES_OPEN=false      checkout is refused, the clients hide it
    BETA_INVITE_CODES=friends:c0-7f3a,students:c1-9b2e

`gate_new_account` is the one check that account creation runs. It is pure
(a header in, a stamp or an exception out) so it is tested without Firebase.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, Optional, TypedDict

from fastapi import HTTPException

#: The header a sign-up request carries its invite code in.
INVITE_HEADER: str = "X-Invite-Code"

#: The stable codes the clients switch on.
BETA_INVITE_REQUIRED_CODE: str = "beta_invite_required"
UPGRADES_CLOSED_CODE: str = "upgrades_closed"


class BetaConfig(TypedDict):
    active: bool
    invite_required: bool
    upgrades_open: bool


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def beta_config() -> BetaConfig:
    """The three flags as the clients read them. Unset means no beta."""
    return {
        "active": _flag("BETA_ACTIVE", False),
        "invite_required": _flag("BETA_INVITE_REQUIRED", False),
        "upgrades_open": _flag("BETA_UPGRADES_OPEN", True),
    }


def parse_invite_codes(raw: Optional[str]) -> Dict[str, str]:
    """`cohort:code,cohort:code` → {code: cohort}. Codes compare case-insensitively.

    A bare entry with no colon is a code for an unnamed cohort, so a single
    `BETA_INVITE_CODES=letmein` still works.
    """
    codes: Dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        cohort, sep, code = entry.partition(":")
        if not sep:
            cohort, code = "", cohort
        code = code.strip().lower()
        if code:
            codes[code] = cohort.strip()
    return codes


def invite_cohort(code: Optional[str]) -> Optional[str]:
    """The cohort a code belongs to, or None when the code is not one of ours."""
    if not code:
        return None
    return parse_invite_codes(os.getenv("BETA_INVITE_CODES")).get(code.strip().lower())


def gate_new_account(invite_header: Optional[str], now: Optional[datetime] = None) -> Optional[dict]:
    """Decide whether a request may create a brand-new account.

    Returns the `beta` stamp to write on the new document (or None when the
    gate is off), or raises the 403 the clients switch on. Never consulted for
    an account that already exists.
    """
    if not beta_config()["invite_required"]:
        return None
    cohort = invite_cohort(invite_header)
    if cohort is None:
        raise HTTPException(status_code=403, detail={"code": BETA_INVITE_REQUIRED_CODE})
    return {
        "cohort": cohort,
        "invite_code": (invite_header or "").strip().lower(),
        "joined_at": now or datetime.now(timezone.utc),
    }


def require_upgrades_open() -> None:
    """Raise the 403 checkout returns while upgrades are closed."""
    if not beta_config()["upgrades_open"]:
        raise HTTPException(status_code=403, detail={"code": UPGRADES_CLOSED_CODE})
