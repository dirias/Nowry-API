"""
INVITE-001 — an admin is recognised (ADR-077, docs/prd-road-to-market.md FR-019).

`require_admin` used to read `is_admin` from the dictionary the auth dependency
returns. That dictionary is built from the Firebase token and never carried the
flag, so every admin route refused everyone, the real admin included.

Pins:

  1. An account whose document says `is_admin: true` passes, given exactly the
     dictionary the auth dependency returns (no `is_admin` key in it).
  2. A missing flag, a false flag, a truthy non-boolean, a deleted account, an
     unknown or unreadable id are all 403.
  3. A flag forged onto the request dictionary opens nothing.
  4. The flag is read from the account on every call, never from the token cache.
"""
import sys
from unittest.mock import AsyncMock, MagicMock

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.auth import dependencies
from app.auth.dependencies import require_admin

USER_OID = ObjectId("65f0a1b2c3d4e5f6a7b8c9d0")


def auth_dict(**extra) -> dict:
    """What `get_firebase_user` returns: token claims plus the account's id."""
    return {
        "firebase_uid": "uid-1",
        "uid": "uid-1",
        "email": "operator@nowry.app",
        "email_verified": True,
        "name": "Operator",
        "picture": None,
        "sign_in_provider": "google.com",
        "auth_time": 1794400000,
        "user_id": str(USER_OID),
        **extra,
    }


def users_with(document, monkeypatch) -> MagicMock:
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=document)
    monkeypatch.setattr(dependencies, "users_collection", collection)
    return collection


@pytest.mark.asyncio
async def test_an_admin_account_passes_with_the_dictionary_the_auth_dependency_returns(monkeypatch):
    collection = users_with({"_id": USER_OID, "is_admin": True}, monkeypatch)
    current = auth_dict()
    assert "is_admin" not in current

    assert await require_admin(current) is current

    query = collection.find_one.await_args.args[0]
    assert query == {"_id": USER_OID, "deleted_at": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        {"_id": USER_OID},
        {"_id": USER_OID, "is_admin": False},
        {"_id": USER_OID, "is_admin": "true"},
        {"_id": USER_OID, "is_admin": 1},
        None,
    ],
)
async def test_anything_but_a_stored_true_is_refused(monkeypatch, document):
    users_with(document, monkeypatch)
    with pytest.raises(HTTPException) as refused:
        await require_admin(auth_dict())
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_a_flag_forged_onto_the_request_opens_nothing(monkeypatch):
    users_with({"_id": USER_OID, "is_admin": False}, monkeypatch)
    with pytest.raises(HTTPException) as refused:
        await require_admin(auth_dict(is_admin=True))
    assert refused.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, "", "not-an-object-id"])
async def test_an_unreadable_account_id_is_refused_without_a_query(monkeypatch, user_id):
    collection = users_with({"_id": USER_OID, "is_admin": True}, monkeypatch)
    with pytest.raises(HTTPException) as refused:
        await require_admin(auth_dict(user_id=user_id))
    assert refused.value.status_code == 403
    collection.find_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_flag_is_read_from_the_account_on_every_call(monkeypatch):
    """Taking the flag away takes effect on the next request, cached token or not."""
    collection = users_with({"_id": USER_OID, "is_admin": True}, monkeypatch)
    current = auth_dict()
    await require_admin(current)

    collection.find_one = AsyncMock(return_value={"_id": USER_OID, "is_admin": False})
    with pytest.raises(HTTPException):
        await require_admin(current)
    assert "is_admin" not in current
