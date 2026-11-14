"""
INVITE-009 — Resend, Invite again, Revoke (ADR-077 amendment, docs/prd-admin.md FR-012, D4).

Pins:

  1. Resend (open or expired) and Invite again (revoked) make a new invitation for the same
     address, cohort, language and lifetime, email it and log `invite.resend`; the old link
     stops working.
  2. A used row is refused: 409 `invite_used`.
  3. At most one letter an hour per address, counted from the newest invitation's letter:
     429 `resend_too_soon` with `retry_at`. A letter that never left holds nothing back.
  4. With no invitation left today: 409 `over_budget`, and the old link still works.
  5. A malformed or unknown id is 404, like revoke.
  6. Revoke is logged as `invite.revoke` only when it closed something.
"""
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.config import invites as rules
from app.routers import beta_admin
from app.services import admin_actions, beta_invites, email as email_service, invitation_email, invite_batch, invite_resend
from tests._fake_collection import FakeCollection

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
ADMIN = "65f0a1b2c3d4e5f6a7b8c9d0"


@pytest.fixture
def world(monkeypatch):
    stores = {name: FakeCollection() for name in ("invites", "waitlist", "log")}
    monkeypatch.setattr(beta_invites, "invites_collection", stores["invites"])
    monkeypatch.setattr(invite_resend, "invites_collection", stores["invites"])
    monkeypatch.setattr(invitation_email, "invites_collection", stores["invites"])
    monkeypatch.setattr(invitation_email, "waitlist_collection", stores["waitlist"])
    monkeypatch.setattr(admin_actions, "admin_actions_collection", stores["log"])
    monkeypatch.setattr(invite_batch.email_ledger, "sent_today", AsyncMock(return_value=0))
    monkeypatch.setenv("EMAIL_DAILY_BUDGET", "125")
    monkeypatch.setenv("API_PUBLIC_URL", "https://api.nowry.app")
    send = AsyncMock(return_value="sent")
    monkeypatch.setattr(email_service, "send_email", send)
    stores["send"] = send
    return stores


async def sent_invitation(world, when, days=30, locale="ja", **changes):
    row, token = await beta_invites.create_invite("ana@x.com", "spring", ADMIN, days, "from Ana's class", now=when, locale=locale)
    await world["invites"].update_one({"_id": row["_id"]}, {"$set": {"sent_at": when, "send_count": 1, **changes}})
    return str(row["_id"]), token


async def claim(world, token, when):
    return await world["invites"].find_one_and_update(rules.claim_query(token, "ana@x.com", "uid-ana", when), rules.claim_update("uid-ana", when))


@pytest.mark.asyncio
async def test_resend_replaces_an_open_invitation_and_the_old_link_stops_working(world):
    old_id, old_token = await sent_invitation(world, NOW - timedelta(hours=2))
    fresh = await invite_resend.resend_invite(old_id, ADMIN, now=NOW)
    assert (fresh["email"], fresh["cohort"], fresh["locale"], fresh["note"]) == ("ana@x.com", "spring", "ja", "from Ana's class")
    assert fresh["expires_at"] == NOW + timedelta(days=30) and fresh["sent_at"] == NOW and fresh["send_count"] == 1
    old = next(r for r in world["invites"].rows if str(r["_id"]) == old_id)
    assert old["status"] == "revoked"
    assert await claim(world, old_token, NOW) is None
    kind, to, locale, values = world["send"].await_args.args
    assert (kind, to, locale) == ("beta.invitation", "ana@x.com", "ja")
    assert await claim(world, values["link"].split("invite=")[1], NOW) is not None
    log = world["log"].rows[0]
    assert (log["action"], log["target_id"], log["details"]["replaces"], log["details"]["state_before"]) == ("invite.resend", str(fresh["_id"]), old_id, "open")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["expired", "revoked"])
async def test_an_expired_row_is_resent_and_a_revoked_row_invited_again(world, state):
    if state == "expired":
        old_id, _ = await sent_invitation(world, NOW - timedelta(days=40), days=7)
    else:
        old_id, _ = await sent_invitation(world, NOW - timedelta(days=2), days=7)
        await beta_invites.revoke_invite(old_id, ADMIN, now=NOW - timedelta(days=1))
    fresh = await invite_resend.resend_invite(old_id, ADMIN, now=NOW)
    assert rules.invite_state(fresh, NOW) == "open" and fresh["expires_at"] == NOW + timedelta(days=7)
    assert world["log"].rows[0]["details"]["state_before"] == state


@pytest.mark.asyncio
async def test_a_used_invitation_is_refused(world):
    old_id, token = await sent_invitation(world, NOW - timedelta(days=2))
    await claim(world, token, NOW - timedelta(days=1))
    with pytest.raises(HTTPException) as refused:
        await invite_resend.resend_invite(old_id, ADMIN, now=NOW)
    assert refused.value.status_code == 409 and refused.value.detail == {"code": "invite_used"}
    assert len(world["invites"].rows) == 1 and world["send"].await_count == 0


@pytest.mark.asyncio
async def test_once_an_hour_per_address_with_the_time_it_opens(world):
    old_id, _ = await sent_invitation(world, NOW - timedelta(minutes=20))
    with pytest.raises(HTTPException) as refused:
        await invite_resend.resend_invite(old_id, ADMIN, now=NOW)
    assert refused.value.status_code == 429
    assert refused.value.detail == {"code": "resend_too_soon", "retry_at": (NOW + timedelta(minutes=40)).isoformat()}

    fresh = await invite_resend.resend_invite(old_id, ADMIN, now=NOW + timedelta(minutes=40))
    with pytest.raises(HTTPException) as again:  # the newest letter counts, whichever row is pressed
        await invite_resend.resend_invite(old_id, ADMIN, now=NOW + timedelta(minutes=50))
    assert again.value.detail["retry_at"] == (NOW + timedelta(minutes=100)).isoformat()
    assert fresh is not None


@pytest.mark.asyncio
async def test_a_row_made_by_hand_counts_from_its_creation_and_a_failed_letter_not_at_all(world):
    row, _ = await beta_invites.create_invite("ana@x.com", "spring", ADMIN, now=NOW - timedelta(minutes=10))
    with pytest.raises(HTTPException):
        await invite_resend.resend_invite(str(row["_id"]), ADMIN, now=NOW)
    await world["invites"].update_one({"_id": row["_id"]}, {"$set": {"last_error": "failed"}})
    assert await invite_resend.resend_invite(str(row["_id"]), ADMIN, now=NOW) is not None


@pytest.mark.asyncio
async def test_with_nothing_left_today_the_old_link_keeps_working(world, monkeypatch):
    monkeypatch.setattr(invite_batch.email_ledger, "sent_today", AsyncMock(return_value=100))
    old_id, token = await sent_invitation(world, NOW - timedelta(hours=2))
    with pytest.raises(HTTPException) as refused:
        await invite_resend.resend_invite(old_id, ADMIN, now=NOW)
    assert refused.value.status_code == 409 and refused.value.detail == {"code": "over_budget", "needed": 1, "left": 0}
    assert await claim(world, token, NOW) is not None


@pytest.mark.asyncio
async def test_a_failed_letter_keeps_the_new_invitation_open_with_its_error(world):
    world["send"].return_value = "failed"
    old_id, _ = await sent_invitation(world, NOW - timedelta(hours=2))
    fresh = await invite_resend.resend_invite(old_id, ADMIN, now=NOW)
    assert rules.invite_state(fresh, NOW) == "open" and fresh["last_error"] == "failed" and fresh["sent_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invite_id", ["not-an-id", str(ObjectId())])
async def test_a_malformed_or_unknown_id_is_404_on_resend_and_revoke(world, invite_id):
    admin = {"user_id": ADMIN}
    for route in (beta_admin.resend_invite, beta_admin.revoke_invite):
        with pytest.raises(HTTPException) as missing:
            await route(invite_id, admin=admin)
        assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_the_resend_route_answers_the_new_summary_without_a_link(world):
    old_id, _ = await sent_invitation(world, datetime.now(timezone.utc) - timedelta(hours=2))
    answer = await beta_admin.resend_invite(old_id, admin={"user_id": ADMIN})
    assert answer.id != old_id and answer.state == "open" and answer.send_count == 1 and answer.locale == "ja"
    assert "link" not in answer.model_dump() and "nwi_" not in answer.model_dump_json()


@pytest.mark.asyncio
async def test_revoke_is_logged_only_when_it_closed_something(world):
    old_id, token = await sent_invitation(world, NOW - timedelta(days=1))
    await claim(world, token, NOW)
    await beta_admin.revoke_invite(old_id, admin={"user_id": ADMIN})
    assert world["log"].rows == []
    other, _ = await beta_invites.create_invite("bo@x.com", "spring", ADMIN, now=datetime.now(timezone.utc))
    await beta_admin.revoke_invite(str(other["_id"]), admin={"user_id": ADMIN})
    assert [(r["action"], r["target_title"]) for r in world["log"].rows] == [("invite.revoke", "bo@x.com")]
