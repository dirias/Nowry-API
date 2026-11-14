"""
Change or set a password through the API (PASS-001, PASS-002, ADR-092).

    POST /auth/password/change   {current_password, new_password, language?}
    POST /auth/password/set      {new_password, language?}   a Google-only account, signed in < 5 minutes ago

Both cut the account's older sessions and send one `account.password` notice.
"""
from __future__ import annotations

import time
from typing import Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.firebase_auth import get_firebase_user
from app.config.database import users_collection
from app.services import account_deletion, account_password
from app.utils.keyed_rate_limit import current_count, seconds_to_wait

router = APIRouter(prefix="/auth/password", tags=["authentication"])


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=account_password.MIN_LENGTH, max_length=128)
    language: Optional[str] = Field(default=None, max_length=12)


class PasswordSet(BaseModel):
    new_password: str = Field(min_length=account_password.MIN_LENGTH, max_length=128)
    language: Optional[str] = Field(default=None, max_length=12)


class PasswordResult(BaseModel):
    status: Literal["changed", "set"]


async def _account(current_user: dict) -> dict:
    user = await users_collection.find_one({"_id": ObjectId(current_user["user_id"]), "deleted_at": None})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _locale(requested: Optional[str], user: dict) -> Optional[str]:
    return account_deletion.notice_locale(requested, user)


def _too_many(wait: int) -> HTTPException:
    return HTTPException(status_code=429, detail={"code": "too_many_attempts", "retry_after": wait})


@router.post("/change", response_model=PasswordResult)
async def change_password(
    payload: PasswordChange, background: BackgroundTasks, current_user: dict = Depends(get_firebase_user)
) -> PasswordResult:
    """The current password proves it; wrong ones count; older sessions end; one notice."""
    uid = current_user["firebase_uid"]
    if await current_count("password_wrong", uid, account_password.WRONG_WINDOW) >= account_password.WRONG_LIMIT:
        raise _too_many(max(1, account_password.WRONG_WINDOW - int(time.time()) % account_password.WRONG_WINDOW))
    user = await _account(current_user)
    if "password" not in await account_password.providers(uid):
        raise HTTPException(status_code=409, detail={"code": "no_password"})
    try:
        right = await account_password.check_current(user["email"], payload.current_password)
    except account_password.PasswordCheckUnavailable:
        raise HTTPException(status_code=503, detail={"code": "check_unavailable"})
    if not right:
        wait = await seconds_to_wait("password_wrong", uid, account_password.WRONG_LIMIT, account_password.WRONG_WINDOW)
        if wait:
            raise _too_many(wait)
        raise HTTPException(status_code=400, detail={"code": "wrong_password"})
    # Taken before the update: the cut is never older than the change itself.
    began = int(time.time())
    await account_password.set_password(uid, payload.new_password)
    await account_password.cut_sessions(current_user["user_id"], uid, await account_password.revocation_time(uid, began))
    background.add_task(account_password.send_notice, user["email"], _locale(payload.language, user))
    return PasswordResult(status="changed")


@router.post("/set", response_model=PasswordResult)
async def set_password(
    payload: PasswordSet, background: BackgroundTasks, current_user: dict = Depends(get_firebase_user)
) -> PasswordResult:
    """A Google-only account adds a password after a fresh Google sign-in; older sessions end; one notice."""
    if not account_deletion.signed_in_recently(current_user.get("auth_time")):
        raise HTTPException(status_code=401, detail={"code": account_deletion.REAUTH_REQUIRED_CODE})
    uid = current_user["firebase_uid"]
    user = await _account(current_user)
    if "password" in await account_password.providers(uid):
        raise HTTPException(status_code=409, detail={"code": "has_password"})
    await account_password.set_password(uid, payload.new_password)
    await account_password.cut_sessions(current_user["user_id"], uid, int(current_user["auth_time"]))
    background.add_task(account_password.send_notice, user["email"], _locale(payload.language, user))
    return PasswordResult(status="set")
