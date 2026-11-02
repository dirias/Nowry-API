"""STUDY-013 — a session record names itself on the server and is logged once (ADR-058 §1–2)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from app.models.study_sessions import LoggedCard, LogSessionRequest
from app.routers import study_sessions
from app.config import database

USER = {"user_id": "user-1"}
DECK_A, DECK_B = ObjectId(), ObjectId()
CARD_1, CARD_2, CARD_3 = ObjectId(), ObjectId(), ObjectId()


def cursor(docs: list) -> MagicMock:
    c = MagicMock()
    c.to_list = AsyncMock(return_value=docs)
    c.sort = MagicMock(return_value=c)
    return c


def decks_collection() -> MagicMock:
    col = MagicMock()
    col.find = MagicMock(
        return_value=cursor([{"_id": DECK_A, "name": "Vocabulary"}, {"_id": DECK_B, "name": "Kanji"}])
    )
    return col


def cards_collection() -> MagicMock:
    col = MagicMock()
    col.find = MagicMock(
        return_value=cursor(
            [
                {"_id": CARD_1, "deck_id": DECK_A},
                {"_id": CARD_2, "deck_id": str(DECK_B)},
                {"_id": CARD_3, "deck_id": DECK_A},
            ]
        )
    )
    return col


def sessions_collection(existing: dict | None = None) -> MagicMock:
    col = MagicMock()
    col.find_one = AsyncMock(return_value=existing)
    col.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId()))
    col.update_one = AsyncMock()
    col.count_documents = AsyncMock(return_value=1)
    return col


def body(**overrides) -> LogSessionRequest:
    fields = {
        "deck_id": str(DECK_A),
        "started_at": datetime.now(timezone.utc),
        "cards": [
            LoggedCard(card_id=str(CARD_1), grade="good", evaluation="correct"),
            LoggedCard(card_id=str(CARD_2), grade="again", evaluation="incorrect"),
            LoggedCard(card_id=str(CARD_3), grade="hard", evaluation="partial"),
        ],
    }
    fields.update(overrides)
    return LogSessionRequest(**fields)


@pytest.mark.asyncio
async def test_a_deck_session_without_a_name_is_named_from_its_deck() -> None:
    sessions = sessions_collection()
    with patch.object(study_sessions, "study_sessions_collection", sessions), patch.object(
        study_sessions, "decks_collection", decks_collection()
    ):
        result = await study_sessions.log_study_session(body(), current_user=USER)

    doc = sessions.insert_one.call_args.args[0]
    assert doc["deck_name"] == "Vocabulary"
    assert result["logged"] is True and result["score_percentage"] == 50


@pytest.mark.asyncio
async def test_a_cross_deck_session_counts_and_names_its_decks() -> None:
    sessions = sessions_collection()
    with patch.object(study_sessions, "study_sessions_collection", sessions), patch.object(
        study_sessions, "decks_collection", decks_collection()
    ), patch.object(study_sessions, "study_cards_collection", cards_collection()):
        await study_sessions.log_study_session(body(deck_id=None), current_user=USER)

    doc = sessions.insert_one.call_args.args[0]
    assert doc["deck_name"] is None
    assert doc["deck_count"] == 2
    assert doc["deck_names"] == ["Vocabulary", "Kanji"]


@pytest.mark.asyncio
async def test_a_repeated_client_session_id_returns_the_first_record_and_writes_nothing() -> None:
    first = {"_id": ObjectId(), "score_percentage": 83}
    sessions = sessions_collection(existing=first)
    with patch.object(study_sessions, "study_sessions_collection", sessions):
        result = await study_sessions.log_study_session(body(client_session_id="abc"), current_user=USER)

    sessions.insert_one.assert_not_called()
    assert result == {"logged": True, "score_percentage": 83, "session_id": str(first["_id"])}


@pytest.mark.asyncio
async def test_the_client_session_id_is_kept_on_the_record() -> None:
    sessions = sessions_collection()
    with patch.object(study_sessions, "study_sessions_collection", sessions), patch.object(
        study_sessions, "decks_collection", decks_collection()
    ):
        await study_sessions.log_study_session(body(client_session_id="abc"), current_user=USER)
    assert sessions.insert_one.call_args.args[0]["client_session_id"] == "abc"


@pytest.mark.asyncio
async def test_an_old_unnamed_record_is_completed_on_read_and_written_back() -> None:
    now = datetime.now(timezone.utc)
    old = {
        "_id": ObjectId(),
        "session_type": "srs_review",
        "deck_id": None,
        "deck_name": "Daily Review",
        "total_cards": 3,
        "cards": [{"card_id": str(CARD_1)}, {"card_id": str(CARD_2)}],
        "started_at": now,
        "completed_at": now,
    }
    sessions = sessions_collection()
    sessions.find = MagicMock(return_value=cursor([old]))
    with patch.object(study_sessions, "study_sessions_collection", sessions), patch.object(
        study_sessions, "decks_collection", decks_collection()
    ), patch.object(study_sessions, "study_cards_collection", cards_collection()):
        response = await study_sessions.list_study_sessions(limit=20, current_user=USER)

    record = response.sessions[0]
    assert record.deck_count == 2 and record.deck_names == ["Vocabulary", "Kanji"]
    assert record.deck_name is None  # the web's old English literal goes
    sessions.update_one.assert_awaited_once()
    assert sessions.update_one.call_args.args[1]["$set"] == {
        "deck_count": 2, "deck_names": ["Vocabulary", "Kanji"], "deck_name": None,
    }


@pytest.mark.asyncio
async def test_the_phones_old_daily_review_sentinel_is_read_as_a_cross_deck_session() -> None:
    now = datetime.now(timezone.utc)
    old = {"_id": ObjectId(), "session_type": "srs_review", "deck_id": "daily-review", "deck_name": None,
           "total_cards": 2, "cards": [{"card_id": str(CARD_1)}, {"card_id": str(CARD_2)}],
           "started_at": now, "completed_at": now}
    sessions = sessions_collection()
    sessions.find = MagicMock(return_value=cursor([old]))
    with patch.object(study_sessions, "study_sessions_collection", sessions), patch.object(
        study_sessions, "decks_collection", decks_collection()
    ), patch.object(study_sessions, "study_cards_collection", cards_collection()):
        response = await study_sessions.list_study_sessions(limit=20, current_user=USER)
    record = response.sessions[0]
    assert record.deck_id is None and record.deck_count == 2
    assert sessions.update_one.call_args.args[1]["$set"]["deck_id"] is None


@pytest.mark.asyncio
async def test_a_named_record_is_not_touched_on_read() -> None:
    now = datetime.now(timezone.utc)
    named = {"_id": ObjectId(), "session_type": "srs_review", "deck_id": str(DECK_A), "deck_name": "Vocabulary",
             "total_cards": 1, "cards": [], "started_at": now, "completed_at": now}
    sessions = sessions_collection()
    sessions.find = MagicMock(return_value=cursor([named]))
    with patch.object(study_sessions, "study_sessions_collection", sessions):
        response = await study_sessions.list_study_sessions(limit=20, current_user=USER)
    assert response.sessions[0].deck_name == "Vocabulary"
    sessions.update_one.assert_not_called()


def test_the_client_session_index_ignores_records_without_an_id() -> None:
    """A sparse compound index still indexes {user_id, null}; the first start against real data failed on it."""
    import inspect

    source = inspect.getsource(database)
    block = source[source.index('name="study_sessions_client_session"'):][:400]
    assert 'partialFilterExpression={"client_session_id": {"$type": "string"}}' in block
    assert "sparse=True" not in block
