"""
Which plan an account is answered as (ADR-079, docs/prd-road-to-market.md FR-025).

This is the one place that turns an account document into a plan. Routers ask
here, or the dependencies built on it (`get_access`, `get_subscription_tier`,
`track_ai_usage`); none reads `subscription.tier` itself, and a test fails when
one does.

The rule:

    stored plan is Pro                         → Pro,   "paid"
    a beta stamp, while the grant is open      → Pro,   "beta"
    otherwise the stored plan                  → "paid" for Plus, "free" for Free

A beta stamp is the `beta` object account creation writes when an invitation
let the account in (ADR-038, ADR-077). Its cohort's name is not read: a bare
code stamps an empty cohort, and that account is a tester like any other.

The stored plan never changes here, so closing the grant answers every tester
by their stored plan from the next request.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from app.config.beta import full_access_open
from app.config.subscription_plans import SUBSCRIPTION_PLANS, SubscriptionTier, plan_limit

ACCESS_PAID: str = "paid"
ACCESS_BETA: str = "beta"
ACCESS_FREE: str = "free"

#: The fields a query must fetch for `resolve_access` to answer.
ACCESS_PROJECTION: Dict[str, int] = {"subscription": 1, "beta": 1}

#: A tester's month (ADR-079 point 4). Every limit not named here is Pro's.
BETA_LIMITS: Dict[str, int] = {
    "ai_calls_per_month": 200,
    "tts_chars_per_month": 150_000,
    "agent_messages_per_month": 500,
    "images_per_month": 2000,
}

UNLIMITED: int = -1


@dataclass(frozen=True)
class Access:
    """The plan in effect, why, and what the account pays for."""

    tier: str
    source: str
    paid_tier: str

    @property
    def plan(self) -> SubscriptionTier:
        return SubscriptionTier(self.tier)


def stored_tier(user: Optional[Mapping[str, Any]]) -> str:
    """The plan the account document stores; anything unknown reads as free."""
    raw = ((user or {}).get("subscription") or {}).get("tier", SubscriptionTier.FREE.value)
    try:
        return SubscriptionTier(raw).value
    except ValueError:
        return SubscriptionTier.FREE.value


def has_beta_stamp(user: Optional[Mapping[str, Any]]) -> bool:
    """True when account creation stamped this account as invited, whatever the cohort."""
    stamp = (user or {}).get("beta")
    return isinstance(stamp, Mapping) and len(stamp) > 0


def resolve_access(user: Optional[Mapping[str, Any]], now: Optional[datetime] = None) -> Access:
    """The plan this account is answered as. Pure: the document and the clock in, the answer out."""
    paid = stored_tier(user)
    pro = SubscriptionTier.PRO.value
    if paid == pro:
        return Access(tier=pro, source=ACCESS_PAID, paid_tier=paid)
    if has_beta_stamp(user) and full_access_open(now):
        return Access(tier=pro, source=ACCESS_BETA, paid_tier=paid)
    source = ACCESS_FREE if paid == SubscriptionTier.FREE.value else ACCESS_PAID
    return Access(tier=paid, source=source, paid_tier=paid)


def effective_tier(user: Optional[Mapping[str, Any]], now: Optional[datetime] = None) -> str:
    """The plan in effect as a string: 'free', 'plus' or 'pro'."""
    return resolve_access(user, now).tier


def _larger(a: int, b: int) -> int:
    return UNLIMITED if UNLIMITED in (a, b) else max(a, b)


def access_limit(access: Access, key: str) -> int:
    """A limit for this access; -1 is unlimited.

    A tester gets the cap for the four capped keys, or their own paid limit
    when that is larger, and Pro's limit for everything else.
    """
    if access.source == ACCESS_BETA and key in BETA_LIMITS:
        return _larger(BETA_LIMITS[key], plan_limit(access.paid_tier, key))
    return plan_limit(access.tier, key)


def access_limits(access: Access) -> Dict[str, int]:
    """Every limit of the plan in effect, with a tester's caps applied."""
    return {key: access_limit(access, key) for key in SUBSCRIPTION_PLANS[access.plan]["limits"]}


def access_fields(access: Access) -> Dict[str, str]:
    """What an account endpoint says about the plan (ADR-079 point 6)."""
    return {"tier": access.tier, "access": access.source, "paid_tier": access.paid_tier}
