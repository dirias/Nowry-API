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

A cohort code may carry a ceiling and a last day (ADR-077, FR-020), and two
fallbacks cover the entries that set none. Unset means no limit:

    BETA_INVITE_CODES=friends:c0-7f3a:25:2026-12-15,students:c1-9b2e
    BETA_CODE_MAX_USES=50         for entries with no ceiling of their own
    BETA_CODE_EXPIRES=2026-12-31  for entries with no last day of their own

Full access for testers (ADR-079, FR-025) is one more flag and an optional
last day, read on each request like the others:

    BETA_FULL_ACCESS=true               a stamped account is answered as Pro
    BETA_FULL_ACCESS_UNTIL=2027-01-31   optional last day (UTC, inclusive)

`gate_new_account` is the one check that account creation runs. It takes the
header and a function that counts a code's accounts, so it is tested without
Firebase or Mongo.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Tuple, TypedDict

from fastapi import HTTPException

#: The header a sign-up request carries its invite code in.
INVITE_HEADER: str = "X-Invite-Code"

#: The stable codes the clients switch on.
BETA_INVITE_REQUIRED_CODE: str = "beta_invite_required"
UPGRADES_CLOSED_CODE: str = "upgrades_closed"
#: The 503 a sign-up gets when a code's uses cannot be counted (ADR-077).
INVITE_CHECK_UNAVAILABLE_CODE: str = "invite_check_unavailable"

#: Counts the accounts a code has made, up to a ceiling: (code, ceiling) → uses.
CountUses = Callable[[str, int], Awaitable[int]]

logger = logging.getLogger(__name__)


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


def full_access_open(now: Optional[datetime] = None) -> bool:
    """Is the testers' full access on right now? (ADR-079 point 3)

    Off when the flag is unset. A last day that cannot be read closes it: the
    day was meant to end the grant, so a typo must not leave it open for good.
    """
    if not _flag("BETA_FULL_ACCESS", False):
        return False
    try:
        last_day = _read_day(os.getenv("BETA_FULL_ACCESS_UNTIL"))
    except ValueError:
        return False
    if last_day is None:
        return True
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date() <= last_day


def describe_full_access() -> str:
    """One line for the start-up log: whether testers have full access, and until when."""
    if not _flag("BETA_FULL_ACCESS", False):
        return "full access for testers: off"
    raw = os.getenv("BETA_FULL_ACCESS_UNTIL")
    try:
        last_day = _read_day(raw)
    except ValueError:
        return "full access for testers: UNREADABLE last day, the grant is closed"
    return f"full access for testers: on, last day {last_day or 'none'}"


@dataclass(frozen=True)
class CodeRule:
    """One cohort code as the environment describes it (ADR-077, FR-020).

    `max_uses` and `expires` are None when the code has no limit. `usable` is
    False when the entry's limits could not be read: such a code is refused
    rather than left open, since the limit was meant to close it.
    """

    cohort: str
    max_uses: Optional[int] = None
    expires: Optional[date] = None
    usable: bool = True


def _read_max(raw: Optional[str]) -> Optional[int]:
    """A ceiling as a whole number of accounts; empty is none. Raises on anything else."""
    text = (raw or "").strip()
    if not text:
        return None
    value = int(text)
    if value < 0:
        raise ValueError("a ceiling cannot be negative")
    return value


def _read_day(raw: Optional[str]) -> Optional[date]:
    """A last day as YYYY-MM-DD; empty is none. Raises on anything else."""
    text = (raw or "").strip()
    return date.fromisoformat(text) if text else None


def _limits(max_raw: Optional[str], day_raw: Optional[str]) -> Tuple[Optional[int], Optional[date], bool]:
    try:
        return _read_max(max_raw), _read_day(day_raw), True
    except ValueError:
        return None, None, False


def parse_code_rules(raw: Optional[str], default_max: Optional[str] = None, default_expires: Optional[str] = None) -> Dict[str, CodeRule]:
    """`cohort:code[:max[:YYYY-MM-DD]]`, comma-separated → {code: rule}.

    Codes compare case-insensitively. A bare entry with no colon is a code for
    an unnamed cohort, so a single `BETA_INVITE_CODES=letmein` still works. A
    limit an entry leaves out takes the fallback; an unreadable limit, in the
    entry or in the fallback it relies on, makes the code unusable.
    """
    fallback_max, fallback_day, fallback_ok = _limits(default_max, default_expires)
    rules: Dict[str, CodeRule] = {}
    for entry in (raw or "").split(","):
        parts: List[str] = [part.strip() for part in entry.strip().split(":")]
        if parts == [""]:
            continue
        if len(parts) == 1:
            parts = ["", parts[0]]
        cohort, code = parts[0], parts[1].lower()
        if not code:
            continue
        own_max, own_day, own_ok = _limits(parts[2] if len(parts) > 2 else None, parts[3] if len(parts) > 3 else None)
        max_uses = own_max if own_max is not None else fallback_max
        expires = own_day if own_day is not None else fallback_day
        relies_on_fallback = own_max is None or own_day is None
        usable = own_ok and len(parts) <= 4 and (fallback_ok or not relies_on_fallback)
        rules[code] = CodeRule(cohort=cohort, max_uses=max_uses, expires=expires, usable=usable)
    return rules


def code_rules() -> Dict[str, CodeRule]:
    """The cohort codes as the environment has them now."""
    return parse_code_rules(os.getenv("BETA_INVITE_CODES"), os.getenv("BETA_CODE_MAX_USES"), os.getenv("BETA_CODE_EXPIRES"))


def parse_invite_codes(raw: Optional[str]) -> Dict[str, str]:
    """`cohort:code,…` → {code: cohort}, limits aside."""
    return {code: rule.cohort for code, rule in parse_code_rules(raw).items()}


def code_rule(code: Optional[str]) -> Optional[CodeRule]:
    """The rule for a code, or None when the code is not one of ours."""
    if not code:
        return None
    return code_rules().get(code.strip().lower())


def invite_cohort(code: Optional[str]) -> Optional[str]:
    """The cohort a code names, or None when the code is not one of ours. Limits are not read here."""
    rule = code_rule(code)
    return rule.cohort if rule else None


def is_past(rule: CodeRule, now: datetime) -> bool:
    """True once the rule's last day (UTC, inclusive) is over."""
    return rule.expires is not None and now.astimezone(timezone.utc).date() > rule.expires


async def open_code_rule(code: Optional[str], count_uses: CountUses, now: Optional[datetime] = None) -> Optional[CodeRule]:
    """The rule for a code that can still make an account, or None.

    Unknown, unreadable, past its day and full all answer None, so no caller can
    tell them apart. The count is asked only for a code that has a ceiling, and
    only up to that ceiling. A count that cannot be read raises 503: the limit
    is a safety net, and it does not open when the database is unreachable.
    """
    rule = code_rule(code)
    if rule is None or not rule.usable or is_past(rule, now or datetime.now(timezone.utc)):
        return None
    if rule.max_uses is None:
        return rule
    if rule.max_uses == 0:
        return None
    try:
        uses = await count_uses((code or "").strip().lower(), rule.max_uses)
    except Exception:
        logger.exception("beta: the uses of a cohort code could not be counted; the sign-up is refused")
        raise HTTPException(status_code=503, detail={"code": INVITE_CHECK_UNAVAILABLE_CODE})
    return rule if uses < rule.max_uses else None


def describe_code_rules() -> List[str]:
    """One line per cohort code for the start-up log: cohort and limits, never the code."""
    lines: List[str] = []
    for rule in code_rules().values():
        limits = f"max {rule.max_uses if rule.max_uses is not None else 'none'}, last day {rule.expires or 'none'}"
        state = "" if rule.usable else " — UNREADABLE LIMIT, this code is refused"
        lines.append(f"cohort '{rule.cohort or '(unnamed)'}': {limits}{state}")
    return lines


async def gate_new_account(invite_header: Optional[str], count_uses: CountUses, now: Optional[datetime] = None) -> Optional[dict]:
    """Decide whether a request may create a brand-new account.

    Returns the `beta` stamp to write on the new document (or None when the
    gate is off), or raises the 403 the clients switch on. Never consulted for
    an account that already exists.
    """
    if not beta_config()["invite_required"]:
        return None
    at = now or datetime.now(timezone.utc)
    rule = await open_code_rule(invite_header, count_uses, at)
    if rule is None:
        raise HTTPException(status_code=403, detail={"code": BETA_INVITE_REQUIRED_CODE})
    return {
        "cohort": rule.cohort,
        "invite_code": (invite_header or "").strip().lower(),
        "joined_at": at,
    }


def require_upgrades_open() -> None:
    """Raise the 403 checkout returns while upgrades are closed."""
    if not beta_config()["upgrades_open"]:
        raise HTTPException(status_code=403, detail={"code": UPGRADES_CLOSED_CODE})
