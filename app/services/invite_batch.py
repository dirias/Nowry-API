"""
Invitations in batches (INVITE-008, ADR-077 amendment, docs/prd-admin.md FR-012).

Up to 50 pasted entries are trimmed, lowercased and collapsed, then each
address is sorted into one outcome:

    invalid       not an email address
    left_list     its waitlist row has left, or it bounced or complained (ADR-080)
    has_account   an active account has it
    open_invite   it already holds an open invitation, which is left as it is
    will_invite   (dry run) it would be invited; on a send, `sent` or `send_failed`

A send that would need more invitations than today's share has left is
refused whole, before anything is written. The answer never carries a link.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from fastapi import HTTPException
from pydantic import validate_email
from pydantic_core import PydanticCustomError

from app.config import invites as rules
from app.config.database import invites_collection, users_collection, waitlist_collection
from app.emails.budget import ceiling, parse_budget
from app.emails.render import resolve_locale
from app.services import beta_invites, dev_outbox, email_ledger, email_suppressions, invitation_email
from app.services.email import delivered
from app.services.admin_actions import record_admin_action

WILL_INVITE = "will_invite"
OPEN_INVITE = "open_invite"
HAS_ACCOUNT = "has_account"
INVALID = "invalid"
LEFT_LIST = "left_list"
SENT = "sent"
SEND_FAILED = "send_failed"
OVER_BUDGET_CODE = "over_budget"
#: The most entries one batch takes.
BATCH_MAX = 50
_NOT_AN_ADDRESS = re.compile(r"[\s<>,;]")


def is_address(text: str) -> bool:
    """A bare address, nothing around it: `Ana <ana@x.com>` and `a b@x.com` are not addresses."""
    if not text or _NOT_AN_ADDRESS.search(text):
        return False
    try:
        _, address = validate_email(text)
    except (PydanticCustomError, ValueError):
        return False
    return address.lower() == text


def entries(raw: Sequence[str]) -> List[Tuple[str, Optional[str]]]:
    """(input, address or None) per distinct entry, in the order given; duplicates collapse."""
    seen: Set[str] = set()
    kept: List[Tuple[str, Optional[str]]] = []
    for value in raw:
        normal = rules.normalize_email(value)
        if normal in seen:
            continue
        seen.add(normal)
        kept.append((value, normal if is_address(normal) else None))
    return kept


@dataclass
class Facts:
    """What the database says about a batch's addresses, read in three bounded queries."""

    left: Set[str] = field(default_factory=set)
    accounts: Set[str] = field(default_factory=set)
    open_invites: Dict[str, str] = field(default_factory=dict)
    locales: Dict[str, str] = field(default_factory=dict)


def classify(address: Optional[str], facts: Facts) -> str:
    """The outcome a dry run gives one address."""
    if address is None:
        return INVALID
    if address in facts.left:
        return LEFT_LIST
    if address in facts.accounts:
        return HAS_ACCOUNT
    if address in facts.open_invites:
        return OPEN_INVITE
    return WILL_INVITE


async def gather_facts(addresses: List[str], now: datetime, users: Any = None, waitlist_rows: Any = None, invites: Any = None) -> Facts:
    """Read the waitlist, the accounts and the open invitations for these addresses."""
    users = users_collection if users is None else users
    waitlist_rows = waitlist_collection if waitlist_rows is None else waitlist_rows
    invites = invites_collection if invites is None else invites
    bound = max(1, len(addresses))
    facts = Facts()
    for row in await waitlist_rows.find({"email": {"$in": addresses}}, {"email": 1, "status": 1, "locale": 1, "suppressed": 1}).to_list(length=bound):
        if row.get("status") == "left" or row.get("suppressed"):
            facts.left.add(row["email"])
        if row.get("locale"):
            facts.locales[row["email"]] = resolve_locale(row["locale"])
    for address in addresses:
        if await email_suppressions.is_suppressed(address):
            facts.left.add(address)
    accounts = await users.find({"email": {"$in": addresses}, "deleted_at": None}, {"email": 1}).to_list(length=bound)
    facts.accounts = {row["email"] for row in accounts}
    open_query = {"email": {"$in": addresses}, "status": rules.STATUS_OPEN, "expires_at": {"$gt": now}}
    for row in await invites.find(open_query, {"email": 1}).to_list(length=bound * 2):
        facts.open_invites[row["email"]] = str(row["_id"])
    return facts


async def invitations_left_today() -> int:
    """The invitation class's share of today's budget, less what was sent today (ADR-078 point 9)."""
    share = ceiling("invitation", parse_budget(os.getenv("EMAIL_DAILY_BUDGET")))
    return max(0, share - await email_ledger.sent_today())


def count_outcomes(results: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for result in results:
        counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
    return counts


def spends_budget(address: str) -> bool:
    """Whether a letter to this address would spend the day's budget: not when the dev outbox keeps it (ADR-090)."""
    return not dev_outbox.outbox_only(address)


def refuse_over_budget(needed: int, left: int) -> None:
    """The whole batch is refused when it needs more invitations than today has left."""
    if needed > left:
        raise HTTPException(status_code=409, detail={"code": OVER_BUDGET_CODE, "needed": needed, "left": left})


async def invite_one(address: str, cohort: str, days: int, locale: str, admin_id: str, now: datetime) -> Tuple[str, str]:
    """Create, email and log one invitation; (outcome, invite id)."""
    row, token = await beta_invites.create_invite(address, cohort, admin_id, days, now=now, locale=locale)
    row, outcome = await invitation_email.send_invitation(row, token, now=now)
    result = SENT if delivered(outcome) else SEND_FAILED
    await record_admin_action(admin_id, "invite.send", "invite", str(row["_id"]), address, details={"cohort": cohort, "outcome": outcome})
    return result, str(row["_id"])


async def run_batch(
    raw: Sequence[str], cohort: str, days: int, locale: str, dry_run: bool, admin_id: str, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Sort every entry; unless a dry run, invite the ones to invite. The answer has no link."""
    at = now or datetime.now(timezone.utc)
    pairs = entries(raw)
    facts = await gather_facts([address for _, address in pairs if address], at)
    results = [{"input": value, "email": address, "outcome": classify(address, facts), "invite_id": None} for value, address in pairs]
    for result in results:
        if result["outcome"] == OPEN_INVITE:
            result["invite_id"] = facts.open_invites[result["email"]]
    left = await invitations_left_today()
    if not dry_run:
        to_invite = [result for result in results if result["outcome"] == WILL_INVITE]
        refuse_over_budget(sum(1 for result in to_invite if spends_budget(result["email"])), left)
        for result in to_invite:
            address = result["email"]
            result["outcome"], result["invite_id"] = await invite_one(address, cohort, days, facts.locales.get(address, locale), admin_id, at)
        left = await invitations_left_today()
    return {"dry_run": dry_run, "results": results, "counts": count_outcomes(results), "invitations_left_today": left}
