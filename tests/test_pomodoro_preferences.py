"""
Pomodoro preferences round-trip through `PUT/GET /users/preferences/general`.

The settings page saves each `pomodoro_*` key one at a time through the
general-preferences partial update. Before this test the route's model
silently discarded those keys (``extra='ignore'``), so the timer could never be
enabled. Handlers are called directly, as in test_onboarding_state.py.
"""
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from bson import ObjectId
from pydantic import ValidationError

USER_OID = ObjectId("507f1f77bcf86cd799439011")


def user_doc(preferences: dict | None = None) -> dict:
    return {
        "_id": USER_OID,
        "email": "test@example.com",
        "preferences": preferences if preferences is not None else {"general": {"language": "en"}},
    }


def mock_users_collection(found=None, updated=None) -> MagicMock:
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=found)
    collection.find_one_and_update = AsyncMock(return_value=updated)
    return collection


async def call_put(collection, current_user, body: dict):
    from app.routers.users import GeneralPreferencesUpdate, update_general_preferences

    with patch("app.routers.users.users_collection", collection):
        return await update_general_preferences(
            data=GeneralPreferencesUpdate(**body),
            current_user=current_user,
        )


async def call_get(collection, current_user):
    from app.routers.users import get_general_preferences

    with patch("app.routers.users.users_collection", collection):
        return await get_general_preferences(current_user=current_user)


def applied_set(collection) -> dict:
    _, update = collection.find_one_and_update.call_args[0]
    return update["$set"]


@pytest.mark.asyncio
async def test_enabling_the_timer_is_written_under_preferences_pomodoro(mock_firebase_user):
    stored = user_doc({"general": {"language": "en"}, "pomodoro": {"enabled": True}})
    collection = mock_users_collection(found=user_doc(), updated=stored)

    response = await call_put(collection, mock_firebase_user, {"pomodoro_enabled": True})

    set_doc = applied_set(collection)
    assert set_doc["preferences.pomodoro.enabled"] is True
    # Only the key that was sent is written — the partial-update contract holds.
    assert "preferences.pomodoro.work_minutes" not in set_doc
    assert "preferences.general.language" not in set_doc
    assert response.pomodoro_enabled is True
    assert response.pomodoro_work_minutes == 25


@pytest.mark.asyncio
async def test_every_pomodoro_field_maps_to_its_own_path(mock_firebase_user):
    body = {
        "pomodoro_enabled": True,
        "pomodoro_work_minutes": 50,
        "pomodoro_short_break_minutes": 10,
        "pomodoro_long_break_minutes": 20,
        "pomodoro_auto_start": True,
    }
    stored = user_doc({
        "general": {},
        "pomodoro": {
            "enabled": True,
            "work_minutes": 50,
            "short_break_minutes": 10,
            "long_break_minutes": 20,
            "auto_start": True,
        },
    })
    collection = mock_users_collection(found=user_doc(), updated=stored)

    response = await call_put(collection, mock_firebase_user, body)

    set_doc = applied_set(collection)
    assert set_doc["preferences.pomodoro.work_minutes"] == 50
    assert set_doc["preferences.pomodoro.short_break_minutes"] == 10
    assert set_doc["preferences.pomodoro.long_break_minutes"] == 20
    assert set_doc["preferences.pomodoro.auto_start"] is True
    assert response.pomodoro_work_minutes == 50
    assert response.pomodoro_short_break_minutes == 10
    assert response.pomodoro_long_break_minutes == 20
    assert response.pomodoro_auto_start is True


@pytest.mark.asyncio
async def test_get_reads_the_stored_pomodoro_settings_with_defaults(mock_firebase_user):
    stored = user_doc({"general": {"language": "en"}, "pomodoro": {"enabled": True, "work_minutes": 45}})
    collection = mock_users_collection(found=stored)

    response = await call_get(collection, mock_firebase_user)

    assert response.pomodoro_enabled is True
    assert response.pomodoro_work_minutes == 45
    assert response.pomodoro_short_break_minutes == 5
    assert response.pomodoro_long_break_minutes == 15
    assert response.pomodoro_auto_start is False


@pytest.mark.asyncio
async def test_get_without_pomodoro_subdocument_returns_defaults(mock_firebase_user):
    collection = mock_users_collection(found=user_doc())

    response = await call_get(collection, mock_firebase_user)

    assert response.pomodoro_enabled is False
    assert response.pomodoro_work_minutes == 25


def test_durations_are_bounded():
    from app.routers.users import GeneralPreferencesUpdate

    with pytest.raises(ValidationError):
        GeneralPreferencesUpdate(pomodoro_work_minutes=0)
    with pytest.raises(ValidationError):
        GeneralPreferencesUpdate(pomodoro_short_break_minutes=61)
    assert GeneralPreferencesUpdate(pomodoro_long_break_minutes=120).pomodoro_long_break_minutes == 120
