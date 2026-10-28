"""EVO-001 (ADR-052): a portrait belongs to its stage; a free account keeps the default line."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers.agent import _companion_portrait, _is_default_companion, _xp_for_level, grant_xp

STAGE_1_ART = "https://x/stage-1.png"
STAGE_3_ART = "https://x/stage-3.png"


class TestCompanionPortrait:
    def test_a_free_accounts_reveal_portrait_is_worn_at_its_own_stage(self) -> None:
        pet = {"avatar_url": STAGE_1_ART, "stage_avatars": {"1": [STAGE_1_ART]}}
        assert _companion_portrait(pet, 1) == (STAGE_1_ART, False)

    def test_at_a_later_stage_it_wears_the_default_line_instead(self) -> None:
        pet = {"avatar_url": STAGE_1_ART, "stage_avatars": {"1": [STAGE_1_ART]}, "pet_species": "spiral"}
        assert _companion_portrait(pet, 3) == (None, True)

    def test_a_paid_account_wears_each_stages_own_portrait(self) -> None:
        pet = {"avatar_url": STAGE_3_ART, "stage_avatars": {"1": [STAGE_1_ART], "3": [STAGE_3_ART]}}
        assert _companion_portrait(pet, 3) == (STAGE_3_ART, False)

    def test_a_legacy_single_portrait_is_still_worn_at_every_stage(self) -> None:
        assert _companion_portrait({"avatar_url": STAGE_1_ART}, 4) == (STAGE_1_ART, False)

    def test_another_species_is_not_shown_nowrys_form(self) -> None:
        pet = {"avatar_url": STAGE_1_ART, "stage_avatars": {"1": [STAGE_1_ART]}, "pet_species": "fox"}
        assert _companion_portrait(pet, 3) == (None, False)

    def test_an_account_that_never_generated_is_nowry_whatever_it_stored(self) -> None:
        assert _is_default_companion({}, 5) is True
        assert _is_default_companion({"pet_species": "fox"}, 2) is True


def _collection(tier: str, xp: int, pet: dict) -> MagicMock:
    collection = MagicMock()
    collection.find_one = AsyncMock(
        return_value={"agent": {"xp": xp}, "subscription": {"tier": tier}, "preferences": {"pet": pet}}
    )
    collection.update_one = AsyncMock()
    return collection


def _regen_was_raised(collection: MagicMock) -> bool:
    return any(
        "preferences.pet.avatar_regen_pending" in (call.args[1].get("$set") or {})
        for call in collection.update_one.call_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tier,raised", [("free", False), ("plus", True), ("pro", True)])
async def test_only_a_paid_account_is_sent_to_regenerate_on_evolving(tier: str, raised: bool) -> None:
    pet = {"avatar_url": STAGE_1_ART, "stage_avatars": {"1": [STAGE_1_ART]}}
    just_short_of_stage_2 = _xp_for_level(3) - 1
    collection = _collection(tier, just_short_of_stage_2, pet)
    # Patched where grant_xp reads it: another suite reloads the router module,
    # so the module path can name a different object from the one imported here.
    with patch.dict(grant_xp.__globals__, {"users_collection": collection}):
        result = await grant_xp("64b7f0000000000000000001", 2)
    assert result["new_stage"] == 2
    assert result["avatar_regen_pending"] is raised
    assert _regen_was_raised(collection) is raised
