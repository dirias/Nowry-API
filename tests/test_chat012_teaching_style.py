"""CHAT-012 — Teaching style is its own setting; Socratic moves out of the tone list (ADR-056 §2)."""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from bson import ObjectId
from pydantic import ValidationError

USER_OID = ObjectId("507f1f77bcf86cd799439011")


def user_doc(agent: dict) -> dict:
    return {"_id": USER_OID, "email": "t@example.com", "preferences": {"general": {"language": "en"}, "agent": agent}}


def collection_for(stored: dict, updated: dict | None = None) -> MagicMock:
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=user_doc(stored))
    collection.find_one_and_update = AsyncMock(return_value=user_doc(updated if updated is not None else stored))
    return collection


async def put(collection, user, body: dict):
    from app.routers.users import GeneralPreferencesUpdate, update_general_preferences

    with patch("app.routers.users.users_collection", collection):
        return await update_general_preferences(data=GeneralPreferencesUpdate(**body), current_user=user)


async def get(collection, user):
    from app.routers.users import get_general_preferences

    with patch("app.routers.users.users_collection", collection):
        return await get_general_preferences(current_user=user)


def written(collection) -> dict:
    return collection.find_one_and_update.call_args[0][1]["$set"]


@pytest.mark.asyncio
async def test_the_default_is_show_me(mock_firebase_user):
    response = await get(collection_for({}), mock_firebase_user)
    assert response.agent_teaching_style == "show"


@pytest.mark.asyncio
async def test_the_style_is_saved_under_preferences_agent(mock_firebase_user):
    collection = collection_for({}, {"teaching_style": "guide"})
    response = await put(collection, mock_firebase_user, {"agent_teaching_style": "guide"})
    assert written(collection)["preferences.agent.teaching_style"] == "guide"
    assert response.agent_teaching_style == "guide"


def test_an_unknown_style_is_refused():
    from app.routers.users import GeneralPreferencesUpdate

    with pytest.raises(ValidationError):
        GeneralPreferencesUpdate(agent_teaching_style="quiz")


@pytest.mark.asyncio
async def test_a_socratic_learner_reads_friendly_and_let_me_work_it_out(mock_firebase_user):
    response = await get(collection_for({"tone": "socratic"}), mock_firebase_user)
    assert (response.agent_tone, response.agent_teaching_style) == ("friendly", "guide")


@pytest.mark.asyncio
async def test_picking_a_voice_keeps_the_socratic_method(mock_firebase_user):
    collection = collection_for({"tone": "socratic"}, {"tone": "strict", "teaching_style": "guide"})
    await put(collection, mock_firebase_user, {"agent_tone": "strict"})
    assert written(collection)["preferences.agent.teaching_style"] == "guide"


@pytest.mark.asyncio
async def test_a_learner_who_chose_a_style_keeps_it(mock_firebase_user):
    collection = collection_for({"tone": "friendly", "teaching_style": "show"})
    await put(collection, mock_firebase_user, {"agent_tone": "strict"})
    assert "preferences.agent.teaching_style" not in written(collection)
