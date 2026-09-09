"""
MOB-027 — the push device registry.

Nothing sends notifications yet; this is the table the send path will read.
What is pinned here is the part that is easy to get wrong and impossible to
notice: a token identifies a DEVICE, so it may belong to only one account at a
time, and deregistering must never be able to reach somebody else's row.

Tests call the handlers directly and pass the `Depends()` parameter as a plain
kwarg, the same pattern as `test_users.py`.
"""
import sys

from tests._stubs import stub_if_missing
from unittest.mock import MagicMock, AsyncMock, patch

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest


def make_devices(docs=None) -> MagicMock:
    """A Motor collection mock covering every op the registry performs."""
    collection = MagicMock()
    collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    collection.delete_many = AsyncMock(return_value=MagicMock(deleted_count=1))
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=list(docs or []))
    collection.find = MagicMock(return_value=cursor)
    return collection


@pytest.mark.asyncio
async def test_register_upserts_on_user_and_token(mock_firebase_user):
    """Registering the same device twice must not create a second row."""
    from app.routers.users import DeviceRegisterRequest, register_device

    devices = make_devices()
    body = DeviceRegisterRequest(token="ExponentPushToken[abc]", platform="android", locale="es-ES")

    with patch("app.routers.users.device_tokens_collection", devices):
        await register_device(body=body, current_user=mock_firebase_user)

    query, update = devices.update_one.call_args.args
    assert query == {"user_id": mock_firebase_user["user_id"], "token": "ExponentPushToken[abc]"}
    assert devices.update_one.call_args.kwargs["upsert"] is True

    # The identity of the row is set once; only the mutable facts are rewritten.
    assert update["$setOnInsert"]["user_id"] == mock_firebase_user["user_id"]
    assert update["$setOnInsert"]["token"] == "ExponentPushToken[abc]"
    assert update["$set"]["platform"] == "android"
    assert update["$set"]["locale"] == "es-ES"
    assert "user_id" not in update["$set"]


@pytest.mark.asyncio
async def test_register_takes_the_token_from_any_other_account(mock_firebase_user):
    """Two people on one phone: the previous account's claim is released.

    Without this, a push meant for the first account would arrive on a device
    now showing the second account's data.
    """
    from app.routers.users import DeviceRegisterRequest, register_device

    devices = make_devices()
    body = DeviceRegisterRequest(token="shared-device", platform="ios", locale="en")

    with patch("app.routers.users.device_tokens_collection", devices):
        await register_device(body=body, current_user=mock_firebase_user)

    (released,) = devices.delete_many.call_args.args
    assert released["token"] == "shared-device"
    # Everyone else's row, and never the caller's own.
    assert released["user_id"] == {"$ne": mock_firebase_user["user_id"]}


@pytest.mark.asyncio
async def test_deregister_is_scoped_to_the_caller(mock_firebase_user):
    """A token is not a capability: it cannot unregister another user's device."""
    from app.routers.users import deregister_device

    devices = make_devices()

    with patch("app.routers.users.device_tokens_collection", devices):
        result = await deregister_device(token="somebody-elses", current_user=mock_firebase_user)

    (query,) = devices.delete_many.call_args.args
    assert query == {"user_id": mock_firebase_user["user_id"], "token": "somebody-elses"}
    # 204: no body, and no error for a token that was not there.
    assert result is None


@pytest.mark.asyncio
async def test_tokens_are_read_back_bounded(mock_firebase_user):
    """The send path reads through here, so the bound lives with the registry."""
    from app.routers.users import MAX_DEVICES_PER_USER, device_tokens_for_user

    devices = make_devices(docs=[{"token": "a"}, {"token": "b"}])

    with patch("app.routers.users.device_tokens_collection", devices):
        found = await device_tokens_for_user(mock_firebase_user["user_id"])

    devices.find.assert_called_once_with({"user_id": mock_firebase_user["user_id"]})
    devices.find.return_value.to_list.assert_awaited_once_with(length=MAX_DEVICES_PER_USER)
    assert [doc["token"] for doc in found] == ["a", "b"]


@pytest.mark.asyncio
async def test_both_endpoints_require_authentication():
    """The dependency is the router's, so every path under /users carries it."""
    from app.auth.firebase_auth import get_firebase_user
    from app.routers.users import router

    paths = {"/users/me/devices", "/users/me/devices/{token}"}
    routes = [route for route in router.routes if route.path in paths]
    assert {route.path for route in routes} == paths

    router_dependencies = {dependency.dependency for dependency in router.dependencies}
    assert get_firebase_user in router_dependencies

    for route in routes:
        assert route.status_code == 204, route.path


@pytest.mark.asyncio
async def test_account_deletion_removes_the_user_s_device_tokens(mock_firebase_user):
    """A deleted account must stop being a place a notification can reach.

    Rows here are removed outright rather than soft-deleted: the 30-day recovery
    window applies to the user's content, not to a routing address.
    """
    from app.routers.users import delete_account
    from tests.test_users import make_collection

    devices = make_devices()
    db_module = sys.modules["app.config.database"]

    with patch.dict(sys.modules, {"firebase_admin": MagicMock(auth=MagicMock())}), \
         patch.object(db_module, "priorities_collection", make_collection()), \
         patch.object(db_module, "activities_collection", make_collection()), \
         patch.object(db_module, "daily_routines_collection", make_collection()), \
         patch("app.routers.users.users_collection", make_collection()), \
         patch("app.routers.users.decks_collection", make_collection()), \
         patch("app.routers.users.books_collection", make_collection()), \
         patch("app.routers.users.study_cards_collection", make_collection()), \
         patch("app.routers.users.study_sessions_collection", make_collection()), \
         patch("app.routers.users.annual_plans_collection", make_collection()), \
         patch("app.routers.users.focus_areas_collection", make_collection()), \
         patch("app.routers.users.goals_collection", make_collection()), \
         patch("app.routers.users.tasks_collection", make_collection()), \
         patch("app.routers.users.blackboards_collection", make_collection()), \
         patch("app.routers.users.device_tokens_collection", devices):

        await delete_account(current_user=mock_firebase_user)

    (query,) = devices.delete_many.call_args.args
    assert query == {"user_id": mock_firebase_user["user_id"]}
