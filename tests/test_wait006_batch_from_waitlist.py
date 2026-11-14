"""
WAIT-006 — ticked waitlist rows are invited as one batch (docs/prd-admin.md FR-012, board E).

Pins:

  1. The batch request takes `waitlist_ids` (0..50); `addresses` may be empty, but not both,
     and the two together are at most 50.
  2. Each id answers with its row's address (`input` is the id); an unknown or malformed id
     is `invalid`. An address both pasted and ticked is invited once.
  3. Each row is written to in its own language; the rest of the batch is unchanged
     (dry run, outcomes, budget, logging as `invite.send`).
  4. A ticked `waiting` row becomes `invited` with `invited_at` once its letter left.
"""
import sys
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.Beta import InviteBatchRequest
from app.services import admin_actions, beta_invites, email as email_service, invitation_email, invite_batch
from tests._fake_collection import FakeCollection

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
ADMIN = "65f0a1b2c3d4e5f6a7b8c9d0"


@pytest.fixture
def world(monkeypatch):
    stores = {name: FakeCollection() for name in ("invites", "waitlist", "users", "log")}
    monkeypatch.setattr(beta_invites, "invites_collection", stores["invites"])
    monkeypatch.setattr(invite_batch, "invites_collection", stores["invites"])
    monkeypatch.setattr(invite_batch, "waitlist_collection", stores["waitlist"])
    monkeypatch.setattr(invite_batch, "users_collection", stores["users"])
    monkeypatch.setattr(invitation_email, "invites_collection", stores["invites"])
    monkeypatch.setattr(invitation_email, "waitlist_collection", stores["waitlist"])
    monkeypatch.setattr(admin_actions, "admin_actions_collection", stores["log"])
    monkeypatch.setattr(invite_batch.email_suppressions, "is_suppressed", AsyncMock(return_value=False))
    monkeypatch.setattr(invite_batch.email_ledger, "sent_today", AsyncMock(return_value=0))
    monkeypatch.setattr(invite_batch.dev_outbox, "outbox_only", lambda address: False)
    monkeypatch.setenv("EMAIL_DAILY_BUDGET", "125")  # invitation share: 100
    monkeypatch.setenv("API_PUBLIC_URL", "https://api.nowry.app")
    monkeypatch.setenv("FRONTEND_URL", "https://nowry.app")
    send = AsyncMock(return_value="sent")
    monkeypatch.setattr(email_service, "send_email", send)
    stores["send"] = send
    return stores


async def row(world, email, status="waiting", locale=None):
    result = await world["waitlist"].insert_one({"email": email, "status": status, "locale": locale, "leave_token": email * 3, "created_at": NOW})
    return str(result.inserted_id)


# ── 1. The request ───────────────────────────────────────────────────────────


def test_ids_alone_are_a_batch_and_addresses_default_to_none():
    payload = InviteBatchRequest(waitlist_ids=["65f0a1b2c3d4e5f6a7b8c9d1"], cohort="c1-waitlist")
    assert payload.addresses == [] and payload.waitlist_ids == ["65f0a1b2c3d4e5f6a7b8c9d1"]
    assert InviteBatchRequest(addresses=["a@x.com"], cohort="c1-waitlist").waitlist_ids == []


@pytest.mark.parametrize("fields", [
    {}, {"addresses": [], "waitlist_ids": []},
    {"waitlist_ids": ["x"] * 51},
    {"addresses": ["a@x.com"] * 25, "waitlist_ids": ["x"] * 26},
    {"waitlist_ids": ["x" * 65]},
])
def test_the_request_refuses_an_empty_or_oversized_batch(fields):
    with pytest.raises(ValidationError):
        InviteBatchRequest(cohort="c1-waitlist", **fields)


def test_fifty_in_all_is_allowed():
    InviteBatchRequest(cohort="c1-waitlist", addresses=["a@x.com"] * 25, waitlist_ids=["x"] * 25)


# ── 2. Ids become addresses ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_dry_run_answers_each_id_with_its_address(world):
    ana = await row(world, "ana@x.com", locale="es-MX")
    left = await row(world, "lea@x.com", status="left")
    await world["users"].insert_one({"email": "mem@x.com", "deleted_at": None})
    member = await row(world, "mem@x.com", status="joined")
    unknown, malformed = str(ObjectId()), "not-an-id"
    answer = await invite_batch.run_batch(["bo@x.com", "ANA@x.com"], "c1-waitlist", 14, "en", True, ADMIN, NOW,
                                          waitlist_ids=[ana, left, member, unknown, malformed, ana])
    results = [(r["input"], r["email"], r["outcome"]) for r in answer["results"]]
    assert results == [
        ("bo@x.com", "bo@x.com", "will_invite"), ("ANA@x.com", "ana@x.com", "will_invite"),
        (left, "lea@x.com", "left_list"), (member, "mem@x.com", "has_account"),
        (unknown, None, "invalid"), (malformed, None, "invalid"),
    ]
    assert world["invites"].rows == [] and world["send"].await_count == 0 and world["log"].rows == []


@pytest.mark.asyncio
async def test_a_send_from_ticked_rows_writes_in_each_rows_language_and_marks_them_invited(world):
    ana = await row(world, "ana@x.com", locale="es-MX")
    yu = await row(world, "yu@x.com", locale="ja")
    again = await row(world, "re@x.com", status="invited", locale="fr")
    answer = await invite_batch.run_batch([], "c1-waitlist", 14, "de", False, ADMIN, NOW, waitlist_ids=[ana, yu, again])
    assert [(r["input"], r["outcome"]) for r in answer["results"]] == [(ana, "sent"), (yu, "sent"), (again, "sent")]
    languages = {call.args[1]: call.args[2] for call in world["send"].await_args_list}
    assert languages == {"ana@x.com": "es", "yu@x.com": "ja", "re@x.com": "fr"}
    rows = {r["email"]: r for r in world["waitlist"].rows}
    assert rows["ana@x.com"]["status"] == "invited" and rows["ana@x.com"]["invited_at"] == NOW
    assert rows["yu@x.com"]["status"] == "invited" and rows["re@x.com"]["status"] == "invited"
    assert [r["action"] for r in world["log"].rows] == ["invite.send"] * 3
    assert {r["target_title"] for r in world["log"].rows} == {"ana@x.com", "yu@x.com", "re@x.com"}


@pytest.mark.asyncio
async def test_a_ticked_row_whose_letter_failed_stays_waiting(world):
    ana = await row(world, "ana@x.com")
    world["send"].return_value = "failed"
    answer = await invite_batch.run_batch([], "c1-waitlist", 14, "en", False, ADMIN, NOW, waitlist_ids=[ana])
    assert answer["results"][0]["outcome"] == "send_failed"
    assert world["waitlist"].rows[0]["status"] == "waiting" and "invited_at" not in world["waitlist"].rows[0]


@pytest.mark.asyncio
async def test_ticked_rows_count_against_todays_share(world, monkeypatch):
    ids = [await row(world, f"p{n}@x.com") for n in range(3)]
    monkeypatch.setattr(invite_batch.email_ledger, "sent_today", AsyncMock(return_value=98))
    with pytest.raises(HTTPException) as refused:
        await invite_batch.run_batch(["q@x.com"], "c1-waitlist", 14, "en", False, ADMIN, NOW, waitlist_ids=ids)
    assert refused.value.detail == {"code": "over_budget", "needed": 4, "left": 2}
    assert world["invites"].rows == []


@pytest.mark.asyncio
async def test_the_route_passes_the_ids_and_a_send_needs_the_recent_sign_in(world):
    from app.routers import beta_admin

    ana = await row(world, "ana@x.com")
    stale = {"user_id": ADMIN, "auth_time": int(time.time()) - 13 * 3600}
    dry = await beta_admin.batch_invites(InviteBatchRequest(waitlist_ids=[ana], cohort="c1-waitlist"), admin=stale)
    assert (dry.results[0].input, dry.results[0].email, dry.results[0].outcome) == (ana, "ana@x.com", "will_invite")
    with pytest.raises(HTTPException):
        await beta_admin.batch_invites(InviteBatchRequest(waitlist_ids=[ana], cohort="c1-waitlist", dry_run=False), admin=stale)
    fresh = {"user_id": ADMIN, "auth_time": int(time.time())}
    sent = await beta_admin.batch_invites(InviteBatchRequest(waitlist_ids=[ana], cohort="c1-waitlist", dry_run=False), admin=fresh)
    assert sent.results[0].outcome == "sent" and world["waitlist"].rows[0]["status"] == "invited"
