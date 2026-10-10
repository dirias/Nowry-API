"""
SR-004 — the leech record.

An Again is recorded on the card as a lapse so the clients can count the ones
inside the Struggling window and offer a way out on the fourth. Any other grade
records nothing: the schedule is SM-2's and the memory is only of misses.
"""
import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.routers import study_cards
from tests.test_study_cards_review_mode import (
    CARD_ID,
    _make_card_doc,
    _make_cards_collection,
    _mock_owner_user,
)

_app = FastAPI()
_app.include_router(study_cards.router)


async def _review(grade: str):
    collection = _make_cards_collection(_make_card_doc())
    _app.dependency_overrides[study_cards.get_firebase_user] = _mock_owner_user
    _app.dependency_overrides[study_cards.get_cards_collection] = lambda: collection
    try:
        transport = httpx.ASGITransport(app=_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(f"/study-cards/{CARD_ID}/review?grade={grade}&mode=study")
    finally:
        _app.dependency_overrides.clear()
    return response, collection


@pytest.mark.asyncio
async def test_again_records_a_lapse_capped_to_the_history():
    response, collection = await _review("again")
    assert response.status_code == 200
    query, update = collection.update_one.call_args.args
    assert query == {"_id": ObjectId(CARD_ID)}
    push = update["$push"]["lapses"]
    assert push["$each"] == [update["$set"]["last_reviewed"]]
    assert push["$slice"] == -study_cards.LAPSE_HISTORY
    assert update["$set"]["interval"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("grade", ["hard", "good", "easy"])
async def test_other_grades_record_no_lapse(grade: str):
    response, collection = await _review(grade)
    assert response.status_code == 200
    _, update = collection.update_one.call_args.args
    assert "$push" not in update
    assert set(update) == {"$set"}


def test_the_card_model_carries_lapses():
    from app.models.StudyCard import StudyCard

    assert StudyCard.model_fields["lapses"].default == []
