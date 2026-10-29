"""EVO-010 (ADR-053): a companion's name and species are paid; showing it is not."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.routers import users

USER = "507f1f77bcf86cd799439011"


def _collection(tier: str) -> MagicMock:
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value={"_id": ObjectId(USER), "subscription": {"tier": tier}, "preferences": {"pet": {}}})
    collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    return collection


async def _update(tier: str, **fields):
    collection = _collection(tier)
    with patch.dict(users.update_pet_preferences.__globals__, {"users_collection": collection}):
        await users.update_pet_preferences(users.PetPreferencesUpdate(**fields), current_user={"user_id": USER})
    return collection


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{"pet_name": "Kiwi"}, {"pet_species": "cat"}, {"pet_name": "Kiwi", "pet_active": True}])
async def test_a_free_account_cannot_rename_or_change_species(fields):
    with pytest.raises(HTTPException) as refused:
        await _update("free", **fields)
    assert refused.value.status_code == 403
    assert refused.value.detail == "pet_customization_requires_plus"


@pytest.mark.asyncio
async def test_nothing_is_written_when_refused():
    collection = _collection("free")
    with patch.dict(users.update_pet_preferences.__globals__, {"users_collection": collection}):
        with pytest.raises(HTTPException):
            await users.update_pet_preferences(users.PetPreferencesUpdate(pet_name="Kiwi"), current_user={"user_id": USER})
    collection.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_free_account_can_still_show_or_hide_its_companion():
    collection = await _update("free", pet_active=False)
    collection.update_one.assert_awaited_once()
    assert collection.update_one.await_args.args[1] == {"$set": {"preferences.pet.pet_active": False}}


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["plus", "pro"])
async def test_a_paid_account_names_its_companion(tier):
    collection = await _update(tier, pet_name="Kiwi")
    assert collection.update_one.await_args.args[1] == {"$set": {"preferences.pet.pet_name": "Kiwi"}}
