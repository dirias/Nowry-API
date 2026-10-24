"""QA-003: the due-card queries are served by an index on the field they read."""
import os
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_fork_idempotency import load_database_module

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


def _collection(existing: dict) -> MagicMock:
    collection = MagicMock()
    collection.create_index = AsyncMock()
    collection.drop_index = AsyncMock()
    collection.index_information = AsyncMock(return_value=existing)
    return collection


def _source(*parts: str) -> str:
    with open(os.path.join(APP_DIR, *parts), encoding="utf-8") as handle:
        return handle.read()


@pytest.mark.asyncio
async def test_both_due_card_indexes_end_on_next_review():
    database = load_database_module()
    collection = _collection({})

    await database.create_due_card_indexes(collection)

    keys = {call.kwargs["name"]: call.args[0] for call in collection.create_index.await_args_list}
    assert keys == {
        database.DUE_BY_USER_INDEX: [("user_id", 1), ("deleted_at", 1), ("next_review", 1)],
        database.DUE_BY_DECK_INDEX: [("deck_id", 1), ("deleted_at", 1), ("next_review", 1)],
    }


@pytest.mark.asyncio
async def test_the_unused_index_is_dropped_only_when_present():
    database = load_database_module()

    present = _collection({database.UNUSED_REVIEW_DATE_INDEX: {}})
    await database.create_due_card_indexes(present)
    present.drop_index.assert_awaited_once_with(database.UNUSED_REVIEW_DATE_INDEX)

    absent = _collection({})
    await database.create_due_card_indexes(absent)
    absent.drop_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_index_failure_is_logged_not_raised():
    database = load_database_module()
    collection = _collection({})
    collection.create_index = AsyncMock(side_effect=RuntimeError("no primary"))

    await database.create_due_card_indexes(collection)


def test_the_queries_read_the_indexed_field():
    """The fault this replaces: the index was on `next_review_date`, which no query reads."""
    for parts in (("routers", "study_cards.py"), ("routers", "quiz.py")):
        source = _source(*parts)
        assert re.search(r'"next_review":\s*\{"\$lte"', source), parts
    for root, _, files in os.walk(APP_DIR):
        for name in files:
            if name.endswith(".py") and name != "database.py":
                assert "next_review_date" not in _source(os.path.relpath(os.path.join(root, name), APP_DIR))
