"""
The attempts ledger (ADR-059 §1): one row per graded showing.

Written by the review endpoint; read by the sessions router. Kept out of
both so the routers share one shape of row and neither grows a second one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any

from app.config.database import attempts_collection
from app.models.attempts import AttemptBody

GRADE_TO_EVALUATION: dict[str, str] = {
    "again": "incorrect",
    "hard": "partial",
    "good": "correct",
    "easy": "correct",
}

# How far back the learner's own median think time looks, per deck.
MEDIAN_WINDOW = 200
# A session never holds more rows than this; the queue is capped at 500 cards.
SESSION_ROWS = 600


def _naive_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def _days_overdue(card: dict, now: datetime) -> int | None:
    due = card.get("next_review")
    if not isinstance(due, datetime):
        return None
    return max(0, (_naive_utc(now) - _naive_utc(due)).days)


def attempt_row(
    user_id: str,
    card: dict,
    grade: str,
    body: AttemptBody | None,
    sm2_result: dict | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    The row for one grade. `sm2_result` is None for a practice showing, whose
    schedule is set by the first answer (ADR-057 §2).
    """
    now = now or datetime.now(timezone.utc)
    body = body or AttemptBody()
    deck_id = card.get("deck_id")
    row: dict[str, Any] = {
        "user_id": user_id,
        "card_id": str(card["_id"]),
        "card_title": card.get("title") or card.get("front") or None,
        "deck_id": str(deck_id) if deck_id else None,
        "tags": list(card.get("tags") or []),
        "card_type": card.get("card_type") or "flashcard",
        "grade": grade,
        "evaluation": GRADE_TO_EVALUATION.get(grade, "incorrect"),
        "showing": body.showing,
        "side": body.side,
        "think_ms": body.think_ms,
        "judge_ms": body.judge_ms,
        "confidence": body.confidence,
        "chosen_option": body.chosen_option,
        "help": body.help.model_dump(),
        "before": {
            "interval": card.get("interval", 1),
            "ease_factor": card.get("ease_factor", 2.5),
            "repetitions": card.get("repetitions", 0),
            "lapses": len(card.get("lapses") or []),
            "days_overdue": _days_overdue(card, now),
        },
        "after": (
            {"interval": sm2_result["interval"], "ease_factor": sm2_result["ease_factor"]}
            if sm2_result
            else None
        ),
        "client_session_id": body.client_session_id,
        "position": body.position,
        "total": body.total,
        "scope": body.scope,
        "block_id": body.block_id,
        "local_hour": body.local_hour,
        "client": body.client,
        "graded_at": now,
    }
    return row


async def record_attempt(
    user_id: str, card: dict, grade: str, body: AttemptBody | None, sm2_result: dict | None
) -> dict[str, Any]:
    row = attempt_row(user_id, card, grade, body, sm2_result)
    await attempts_collection.insert_one(row)
    return row


async def session_attempts(user_id: str, client_session_id: str) -> list[dict]:
    return await (
        attempts_collection
        .find({"user_id": user_id, "client_session_id": client_session_id})
        .sort("graded_at", 1)
        .to_list(length=SESSION_ROWS)
    )


async def deck_median_think_ms(user_id: str, deck_id: str | None) -> int | None:
    """The learner's own median over their last showings of this deck (ADR-059 §3)."""
    query: dict[str, Any] = {"user_id": user_id, "think_ms": {"$ne": None}}
    if deck_id:
        query["deck_id"] = deck_id
    rows = await (
        attempts_collection
        .find(query, {"think_ms": 1})
        .sort("graded_at", -1)
        .to_list(length=MEDIAN_WINDOW)
    )
    values = [r["think_ms"] for r in rows if isinstance(r.get("think_ms"), int)]
    return int(median(values)) if values else None
