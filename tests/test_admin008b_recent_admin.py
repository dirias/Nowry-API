"""
ADMIN-008b — an admin write needs a sign-in less than 12 hours old
(ADR-086 point 5, docs/prd-admin.md FR-010).

Pins:

  1. `require_recent_admin` is `require_admin` plus `auth_time` within 12 hours; older,
     missing or unreadable → `401 {"code": "reauth_required"}`. A non-admin is still 403 first.
  2. Every admin write takes it: invite create, revoke and resend, curation, the bug status
     change, the moderation review and removal. The batch send takes it inline, only when
     it is not a dry run. Reads stay on `require_admin`.
"""
import time

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.auth import dependencies
from app.auth.dependencies import ADMIN_SIGN_IN_MAX_AGE_SECONDS, require_admin, require_recent_admin, signed_in_within

NOW = 1_800_000_000.0


def test_twelve_hours_is_the_limit():
    assert ADMIN_SIGN_IN_MAX_AGE_SECONDS == 12 * 3600
    assert signed_in_within(NOW - 12 * 3600, ADMIN_SIGN_IN_MAX_AGE_SECONDS, NOW)
    assert not signed_in_within(NOW - 12 * 3600 - 1, ADMIN_SIGN_IN_MAX_AGE_SECONDS, NOW)


@pytest.mark.parametrize("auth_time", [None, "1800000000", True])
def test_a_missing_or_unreadable_auth_time_is_not_recent(auth_time):
    assert not signed_in_within(auth_time, ADMIN_SIGN_IN_MAX_AGE_SECONDS, NOW)


@pytest.mark.asyncio
async def test_a_recent_admin_passes_and_a_stale_one_is_asked_to_sign_in_again():
    fresh = {"user_id": "a", "auth_time": int(time.time()) - 60}
    assert await require_recent_admin(fresh) is fresh
    with pytest.raises(HTTPException) as refused:
        await require_recent_admin({"user_id": "a", "auth_time": int(time.time()) - 13 * 3600})
    assert refused.value.status_code == 401 and refused.value.detail == {"code": "reauth_required"}


@pytest.mark.asyncio
async def test_a_non_admin_is_refused_403_before_the_sign_in_age_is_read(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    users = MagicMock()
    users.find_one = AsyncMock(return_value={"_id": ObjectId(), "is_admin": False})
    monkeypatch.setattr(dependencies, "users_collection", users)
    with pytest.raises(HTTPException) as refused:
        await require_admin({"user_id": str(ObjectId()), "auth_time": 0})
    assert refused.value.status_code == 403


def _calls(dependant) -> set:
    calls = {dependant.call}
    for sub in dependant.dependencies:
        calls |= _calls(sub)
    return calls


def _route(router, method: str, suffix: str):
    return next(r for r in router.routes if method in r.methods and r.path.endswith(suffix))


WRITES = [
    ("app.routers.beta_admin", "POST", "/beta/admin/invites"),
    ("app.routers.beta_admin", "POST", "/invites/{invite_id}/revoke"),
    ("app.routers.beta_admin", "POST", "/invites/{invite_id}/resend"),
    ("app.routers.public_content", "PUT", "/decks/{deck_id}/curation"),
    ("app.routers.bugs", "PATCH", "/{bug_id}/status"),
    ("app.routers.moderation", "PUT", "/admin/reports/{report_id}/review"),
    ("app.routers.moderation", "DELETE", "/admin/content/{content_type}/{content_id}"),
    ("app.routers.moderation", "POST", "/admin/queue/{content_type}/{content_id}/keep"),
    ("app.routers.moderation", "POST", "/admin/queue/{content_type}/{content_id}/remove"),
]

READS = [
    ("app.routers.beta_admin", "GET", "/beta/admin/invites"),
    ("app.routers.beta_admin", "GET", "/beta/admin/cohorts"),
    ("app.routers.beta_admin", "POST", "/invites/batch"),
    ("app.routers.bugs", "GET", "/bugs/all"),
    ("app.routers.bugs", "GET", "/bugs/stats"),
    ("app.routers.moderation", "GET", "/admin/reports"),
    ("app.routers.moderation", "GET", "/admin/queue"),
]


@pytest.mark.parametrize("module, method, suffix", WRITES)
def test_every_admin_write_needs_the_recent_sign_in(module, method, suffix):
    import importlib

    route = _route(importlib.import_module(module).router, method, suffix)
    calls = _calls(route.dependant)
    assert require_recent_admin in calls and require_admin in calls


@pytest.mark.parametrize("module, method, suffix", READS)
def test_reads_and_the_batch_check_stay_on_require_admin(module, method, suffix):
    import importlib

    route = _route(importlib.import_module(module).router, method, suffix)
    calls = _calls(route.dependant)
    assert require_admin in calls and require_recent_admin not in calls
