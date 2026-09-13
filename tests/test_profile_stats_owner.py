"""
A profile's card counts and streak are queried by the wrong owner key.

`get_user_stats` converts the user id to an `ObjectId` and matches every
`study_cards_collection` query on it. Nothing writes an ObjectId there:
`study_cards.py` stores and reads `user_id` as the STRING throughout — the
insert at `create_study_card`, the library's `base_match`, the due queue, the
review write, all of them. `books_collection` is queried with the string in this
same function and returns a real number, which is exactly why the fault reads as
a product decision rather than a bug: the profile shows a real book count beside
a card count of zero, on every account, in both clients.

So the cards, the flashcards, the reviewed count, the quiz and diagram counts,
the streak and the plan's flashcard meter are all permanently nought, and none
of them fails anything. That is the same shape as the five client-side bugs
`apiFields.test.js` was written for: a wrong key is not an error, it is a
confident zero.

These tests pin the owner key the queries actually use, rather than the number
they return, because the number depends on the database and the key does not.
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


def _cards_collection():
    collection = MagicMock()
    collection.count_documents = AsyncMock(return_value=7)
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=[])
    collection.aggregate = MagicMock(return_value=cursor)
    return collection


async def _run(cards, books, users):
    from app.routers.users import get_user_stats

    with patch("app.routers.users.study_cards_collection", cards), \
         patch("app.routers.users.books_collection", books), \
         patch("app.routers.users.users_collection", users):
        return await get_user_stats(USER_ID)


@pytest.fixture
def books_collection():
    collection = MagicMock()
    collection.count_documents = AsyncMock(return_value=3)
    return collection


@pytest.mark.asyncio
async def test_card_counts_match_the_owner_cards_are_stored_under(mock_users_collection, books_collection):
    """Every study-card count must filter on the id as a string."""
    cards = _cards_collection()
    await _run(cards, books_collection, mock_users_collection)

    assert cards.count_documents.await_count >= 1
    for call in cards.count_documents.await_args_list:
        assert call.args[0]["user_id"] == USER_ID, (
            f"counted cards owned by {call.args[0]['user_id']!r}; "
            "study_cards stores user_id as the string"
        )


@pytest.mark.asyncio
async def test_the_streak_aggregation_matches_the_same_owner(mock_users_collection, books_collection):
    """And so must the streak, which reads the same collection."""
    cards = _cards_collection()
    await _run(cards, books_collection, mock_users_collection)

    pipeline = cards.aggregate.call_args.args[0]
    assert pipeline[0]["$match"]["user_id"] == USER_ID


@pytest.mark.asyncio
async def test_deleted_cards_are_not_counted(mock_users_collection, books_collection):
    """A deleted card is not a card the account has.

    Every other count of this collection in the app carries `deleted_at: None`
    — the library's `base_match`, the due queue, the deck totals. A profile that
    counted deleted ones would disagree with the number the Study Center shows
    on the screen next door, which is the readout a person would compare it to.
    """
    cards = _cards_collection()
    await _run(cards, books_collection, mock_users_collection)

    for call in cards.count_documents.await_args_list:
        assert call.args[0].get("deleted_at", "missing") is None, (
            "a profile count included deleted cards"
        )


@pytest.mark.asyncio
async def test_the_user_document_is_still_read_by_object_id(mock_users_collection, books_collection):
    """The one place the ObjectId is correct: the users collection's own key."""
    cards = _cards_collection()
    await _run(cards, books_collection, mock_users_collection)

    from bson import ObjectId

    assert mock_users_collection.find_one.await_args.args[0]["_id"] == ObjectId(USER_ID)
