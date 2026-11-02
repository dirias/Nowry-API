"""LOG-001 — the attempts ledger: one row per graded showing (ADR-059 §1)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

import importlib

from app.models.attempts import AttemptBody, AttemptHelp
from app.routers import study_cards, study_sessions
from app.services import attempts as ledger

agent_module = importlib.import_module("app.routers.agent")

USER = {"user_id": "user-1"}
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def card(**over) -> dict:
    base = {
        "_id": ObjectId(), "user_id": "user-1", "deck_id": ObjectId(), "title": "果物", "tags": ["N3", "food"],
        "card_type": "flashcard", "interval": 21, "ease_factor": 2.3, "repetitions": 6,
        "lapses": [datetime(2026, 10, 1)], "next_review": datetime(2026, 10, 30),
    }
    base.update(over)
    return base


def cursor(docs: list) -> MagicMock:
    c = MagicMock()
    c.to_list = AsyncMock(return_value=docs)
    c.sort = MagicMock(return_value=c)
    return c


# ----------------------------------------------------------------- the row


def test_the_row_carries_the_state_before_the_grade_and_the_attempt() -> None:
    body = AttemptBody(
        client_session_id="s1", side="front", think_ms=1800, judge_ms=900, position=3, total=16,
        scope="deck", local_hour=23, client="phone", help=AttemptHelp(companion="explain", audio=2),
    )
    sm2 = {"interval": 1, "ease_factor": 2.1, "repetitions": 0, "next_review": NOW, "last_reviewed": NOW}
    row = ledger.attempt_row("user-1", card(), "again", body, sm2, now=NOW)

    assert row["evaluation"] == "incorrect" and row["showing"] == "scheduled"
    assert row["before"] == {"interval": 21, "ease_factor": 2.3, "repetitions": 6, "lapses": 1, "days_overdue": 3}
    assert row["after"] == {"interval": 1, "ease_factor": 2.1}
    assert row["help"] == {"companion": "explain", "audio": 2, "hint": False, "source": False}
    assert row["client_session_id"] == "s1" and row["position"] == 3 and row["local_hour"] == 23
    assert row["card_title"] == "果物" and row["tags"] == ["N3", "food"]


def test_a_review_without_a_body_still_makes_a_row() -> None:
    row = ledger.attempt_row("user-1", card(), "good", None, {"interval": 30, "ease_factor": 2.4}, now=NOW)
    assert row["evaluation"] == "correct" and row["think_ms"] is None and row["after"]["interval"] == 30


# ----------------------------------------------------------- the endpoint


@pytest.mark.asyncio
async def test_a_practice_showing_writes_a_row_and_touches_nothing_else() -> None:
    collection = MagicMock()
    collection.update_one = AsyncMock()
    recorded = AsyncMock()
    with patch.object(study_cards, "record_attempt", recorded):
        result = await study_cards.review_card(
            id="x", grade="again", mode="study", attempt=AttemptBody(showing="practice", client_session_id="s1"),
            collection=collection, card=card(), user=USER,
        )
    collection.update_one.assert_not_called()
    recorded.assert_awaited_once()
    assert recorded.call_args.args[4] is None  # no SM-2 result for a practice showing
    assert result == {"message": "Practice showing recorded", "sm2_data": None, "xp": None}


@pytest.mark.asyncio
async def test_a_scheduled_review_grades_the_card_and_writes_the_row_from_the_state_before() -> None:
    collection = MagicMock()
    collection.update_one = AsyncMock()
    recorded = AsyncMock()
    the_card = card()
    with patch.object(study_cards, "record_attempt", recorded), patch.object(
        agent_module, "grant_xp", AsyncMock(return_value=None)
    ):
        result = await study_cards.review_card(
            id=str(the_card["_id"]), grade="good", mode="study",
            attempt=AttemptBody(client_session_id="s1", think_ms=1200),
            collection=collection, card=the_card, user=USER,
        )
    collection.update_one.assert_awaited_once()
    args = recorded.call_args.args
    assert args[1] is the_card and args[2] == "good" and args[3].think_ms == 1200
    assert args[4]["interval"] > 21  # the SM-2 result travels into the row's `after`
    assert result["sm2_data"]["interval"] == args[4]["interval"]


@pytest.mark.asyncio
async def test_browse_is_still_refused_before_any_write() -> None:
    recorded = AsyncMock()
    with patch.object(study_cards, "record_attempt", recorded), pytest.raises(Exception):
        await study_cards.review_card(
            id="x", grade="good", mode="browse", attempt=AttemptBody(showing="practice"),
            collection=MagicMock(), card=card(), user=USER,
        )
    recorded.assert_not_called()


# ----------------------------------------------------------- the session read


@pytest.mark.asyncio
async def test_a_sessions_attempts_come_with_the_deck_median_and_the_gap_to_the_previous_session() -> None:
    sid = ObjectId()
    started = datetime(2026, 11, 2, 8, 50)
    session_doc = {"_id": sid, "client_session_id": "s1", "deck_id": "d1", "started_at": started, "completed_at": NOW}
    previous = {"_id": ObjectId(), "completed_at": started - timedelta(days=19, hours=2)}
    sessions = MagicMock()
    sessions.find_one = AsyncMock(side_effect=[session_doc, previous])
    rows = [
        {"_id": ObjectId(), "card_id": "c1", "grade": "again", "evaluation": "incorrect", "think_ms": 1800,
         "help": {"companion": "hint"}, "before": {"interval": 21}, "after": None, "graded_at": NOW},
        {"_id": ObjectId(), "card_id": "c2", "grade": "good", "evaluation": "correct", "think_ms": 900,
         "before": {}, "after": {"interval": 30, "ease_factor": 2.4}, "graded_at": NOW},
    ]
    with patch.object(study_sessions, "study_sessions_collection", sessions), patch.object(
        study_sessions, "session_attempts", AsyncMock(return_value=rows)
    ), patch.object(study_sessions, "deck_median_think_ms", AsyncMock(return_value=1350)):
        response = await study_sessions.get_session_attempts(str(sid), current_user=USER)

    assert [a.card_id for a in response.attempts] == ["c1", "c2"]
    assert response.attempts[0].help.companion == "hint" and response.attempts[0].before.interval == 21
    assert response.attempts[1].after.interval == 30
    assert response.median_think_ms == 1350 and response.days_since_previous == 19


@pytest.mark.asyncio
async def test_a_session_logged_before_the_ledger_has_no_rows() -> None:
    sid = ObjectId()
    sessions = MagicMock()
    sessions.find_one = AsyncMock(side_effect=[{"_id": sid, "deck_id": "d1", "started_at": NOW, "completed_at": NOW}, None])
    with patch.object(study_sessions, "study_sessions_collection", sessions):
        response = await study_sessions.get_session_attempts(str(sid), current_user=USER)
    assert response.attempts == [] and response.median_think_ms is None and response.days_since_previous is None


@pytest.mark.asyncio
async def test_the_deck_median_is_the_learners_own() -> None:
    col = MagicMock()
    col.find = MagicMock(return_value=cursor([{"think_ms": 900}, {"think_ms": 3000}, {"think_ms": 1500}]))
    with patch.object(ledger, "attempts_collection", col):
        assert await ledger.deck_median_think_ms("user-1", "d1") == 1500
    col.find = MagicMock(return_value=cursor([]))
    with patch.object(ledger, "attempts_collection", col):
        assert await ledger.deck_median_think_ms("user-1", "d1") is None
