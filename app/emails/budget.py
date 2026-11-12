"""
Who gets the day's sends (MAIL-004, ADR-078 point 9). Pure.

Resend's Free plan allows 100 emails a day for everything. A class sends while
the day's total is under its ceiling, so the lower classes stop early and
account email never finds the allowance spent: a fifth of the day is held for it.
"""
from __future__ import annotations

DEFAULT_DAILY_BUDGET = 90  # production's share; development sets 10 (one Resend account serves both)

# Share of the day's budget a class may send into.
CEILINGS: dict[str, float] = {
    "account": 1.0,       # verification, reset, security notices
    "invitation": 0.8,    # WAIT-006, "early access is open"
    "notice": 0.8,        # to the operator, through notify()
    "confirmation": 0.6,  # WAIT-004
}


def parse_budget(raw: str | None) -> int:
    """`EMAIL_DAILY_BUDGET` as a whole number of sends; unset or unreadable is the default."""
    try:
        value = int((raw or "").strip())
    except ValueError:
        return DEFAULT_DAILY_BUDGET
    return value if value >= 0 else DEFAULT_DAILY_BUDGET


def ceiling(email_class: str, budget: int) -> int:
    """How many sends the day may hold before this class stops. An unknown class gets the lowest."""
    share = CEILINGS.get(email_class, min(CEILINGS.values()))
    return int(budget * share)


def may_send(email_class: str, sent_today: int, budget: int) -> bool:
    return sent_today < ceiling(email_class, budget)
