"""
A day belongs to the learner, not to Greenwich.

`/study-cards/statistics` bucketed reviews by UTC calendar day and walked the
streak back from UTC midnight. `/users/profile` did the same for its own streak.
`/study-cards/forecast` has taken an IANA zone since STUDY-001, so the two
halves of the same screen disagreed about when a day ends.

Found on a device in Tokyo. The most recent reviews were stamped 23:20–23:25
UTC, which is 08:20 local — the learner's morning, and the server's yesterday.
At 09:00 local the UTC day rolled over and the Study Center's "reviewed today"
went from five to zero while nothing was happening, and the streak reset on a
day the learner had already studied. It reads as the app losing your work.

These tests assert the QUERY, not the count: what a day is depends on where you
are, and the zone the aggregation is told is the whole of the fix.
"""
import sys

from tests._stubs import stub_if_missing
from unittest.mock import AsyncMock, MagicMock, patch

stub_if_missing("bcrypt")

if "app.auth.firebase_auth" not in sys.modules:
    _mock_firebase_auth_mod = MagicMock()
    _mock_firebase_auth_mod.get_firebase_user = MagicMock()
    sys.modules["app.auth.firebase_auth"] = _mock_firebase_auth_mod

import pytest

USER_ID = "507f1f77bcf86cd799439011"
TOKYO = "Asia/Tokyo"


def _cards_collection():
    collection = MagicMock()
    collection.count_documents = AsyncMock(return_value=0)
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=[])
    collection.aggregate = MagicMock(return_value=cursor)
    find_cursor = MagicMock()
    find_cursor.sort = MagicMock(return_value=find_cursor)
    find_cursor.limit = MagicMock(return_value=find_cursor)
    find_cursor.to_list = AsyncMock(return_value=[])
    collection.find = MagicMock(return_value=find_cursor)
    return collection


def _grouping_timezone(aggregate_mock):
    """The zone the day-bucketing `$dateToString` was told to use."""
    pipeline = aggregate_mock.call_args.args[0]
    for stage in pipeline:
        group = stage.get("$group")
        if not group:
            continue
        spec = group["_id"]
        # `/statistics` groups by {day, type}; the profile groups by the day alone.
        day = spec.get("day", spec) if isinstance(spec, dict) else spec
        return day["$dateToString"].get("timezone")
    return None


# ── /users/profile ──────────────────────────────────────────────────────────


async def _profile_stats(tz):
    from app.routers.users import get_user_stats

    cards = _cards_collection()
    books = MagicMock()
    books.count_documents = AsyncMock(return_value=0)
    users = MagicMock()
    users.find_one = AsyncMock(return_value={})

    with patch("app.routers.users.study_cards_collection", cards), \
         patch("app.routers.users.books_collection", books), \
         patch("app.routers.users.users_collection", users):
        await get_user_stats(USER_ID, tz)
    return cards


@pytest.mark.asyncio
async def test_the_profile_streak_buckets_days_in_the_learners_zone():
    cards = await _profile_stats(TOKYO)
    assert _grouping_timezone(cards.aggregate) == TOKYO


@pytest.mark.asyncio
async def test_the_profile_streak_still_answers_for_a_zone_it_does_not_know():
    """A bad zone costs the streak its accuracy; it must not cost the page.

    Unlike `/statistics`, whose whole job is day boundaries, a profile is mostly
    counts with no day in them.
    """
    cards = await _profile_stats("Mars/Olympus_Mons")
    assert cards.aggregate.called


# ── /study-cards/statistics ─────────────────────────────────────────────────


async def _statistics(tz):
    from app.routers.study_cards import get_statistics

    cards = _cards_collection()
    books = MagicMock()
    books_cursor = MagicMock()
    books_cursor.to_list = AsyncMock(return_value=[])
    books.find = MagicMock(return_value=books_cursor)

    with patch("app.routers.study_cards.books_collection", books), \
         patch("app.routers.study_cards._active_deck_or", AsyncMock(return_value=[{"deck_id": None}])):
        result = await get_statistics(tz=tz, collection=cards, current_user={"user_id": USER_ID})
    return cards, result


@pytest.mark.asyncio
async def test_statistics_buckets_the_week_in_the_learners_zone():
    cards, _ = await _statistics(TOKYO)
    assert _grouping_timezone(cards.aggregate) == TOKYO


@pytest.mark.asyncio
async def test_statistics_defaults_to_utc_for_a_client_that_says_nothing():
    """The old behaviour, kept as the default so an un-updated client is no
    worse off than it was — and no better, which is why both clients send it."""
    cards, _ = await _statistics("UTC")
    assert _grouping_timezone(cards.aggregate) == "UTC"


@pytest.mark.asyncio
async def test_the_week_is_seven_local_days_ending_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _, result = await _statistics(TOKYO)
    days = [row["date"] for row in result["weekly_progress"]]

    assert len(days) == 7
    assert days == sorted(days), "the week reads oldest to newest"
    assert days[-1] == datetime.now(tz=ZoneInfo(TOKYO)).strftime("%Y-%m-%d"), (
        "the last bucket is the learner's today, not Greenwich's"
    )


@pytest.mark.asyncio
async def test_statistics_refuses_a_zone_it_does_not_know():
    """Here a wrong zone IS a wrong answer, so it is named rather than guessed."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        await _statistics("Mars/Olympus_Mons")
    assert raised.value.status_code == 400
