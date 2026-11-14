"""
ADMIN-005 — the admin action log (ADR-086 point 4, docs/prd-admin.md FR-003).

Pins:

  1. A row holds actor, action, target, title, reason, details and the time, and nothing else.
  2. The action list is fixed: an action outside it is never written.
  3. Writing never raises into the caller: a refused insert is logged and answered False.
  4. Long free text is cut, so a row stays small.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import admin_actions
from tests._fake_collection import FakeCollection

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def test_the_action_list_is_the_one_batch_1_writes():
    assert set(admin_actions.ADMIN_ACTIONS) == {
        "invite.create", "invite.send", "invite.resend", "invite.revoke",
        "curation.review", "admin.grant", "admin.revoke", "bug.status",
    }


def test_a_row_holds_who_what_which_why_and_when():
    row = admin_actions.admin_action_row(
        "65f0a1b2c3d4e5f6a7b8c9d0", "invite.revoke", "invite", "abc", target_title="ana@example.com",
        reason="asked", details={"cohort": "friends"}, now=NOW,
    )
    assert row == {
        "actor_id": "65f0a1b2c3d4e5f6a7b8c9d0", "action": "invite.revoke", "target_type": "invite",
        "target_id": "abc", "target_title": "ana@example.com", "reason": "asked",
        "details": {"cohort": "friends"}, "created_at": NOW,
    }


def test_an_action_outside_the_list_is_refused_by_the_row_builder():
    with pytest.raises(ValueError):
        admin_actions.admin_action_row("script", "admin.delete_everything", "user", "x", now=NOW)


def test_free_text_is_cut_to_its_limit():
    row = admin_actions.admin_action_row("a", "bug.status", "bug", "b", target_title="t" * 900, reason="r" * 900, now=NOW)
    assert len(row["target_title"]) == admin_actions.TEXT_MAX and len(row["reason"]) == admin_actions.TEXT_MAX


@pytest.mark.asyncio
async def test_recording_writes_one_row():
    collection = FakeCollection()
    assert await admin_actions.record_admin_action("script", "admin.grant", "user", "u1", collection=collection, now=NOW)
    assert len(collection.rows) == 1
    assert collection.rows[0]["action"] == "admin.grant" and collection.rows[0]["created_at"] == NOW
    assert collection.rows[0]["details"] is None


@pytest.mark.asyncio
async def test_recording_never_raises_and_says_it_failed(caplog):
    collection = MagicMock()
    collection.insert_one = AsyncMock(side_effect=RuntimeError("database down"))
    assert await admin_actions.record_admin_action("a", "invite.create", "invite", "i", collection=collection) is False
    assert "admin log" in caplog.text


@pytest.mark.asyncio
async def test_an_unknown_action_is_logged_not_raised(caplog):
    collection = FakeCollection()
    assert await admin_actions.record_admin_action("a", "nonsense", "invite", "i", collection=collection) is False
    assert collection.rows == []


@pytest.mark.asyncio
async def test_the_default_collection_is_the_admin_log(monkeypatch):
    collection = FakeCollection()
    monkeypatch.setattr(admin_actions, "admin_actions_collection", collection)
    await admin_actions.record_admin_action("a", "curation.review", "deck", "d1", target_title="Biology")
    assert collection.rows[0]["target_type"] == "deck"


# ── The writes that call it ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_curation_review_is_logged(monkeypatch):
    from app.routers import public_content
    from app.routers.public_content import DeckCurationRequest

    log = FakeCollection()
    monkeypatch.setattr(admin_actions, "admin_actions_collection", log)
    service = MagicMock()
    service.set_deck_curation = AsyncMock(return_value={
        "deck_id": "d1", "status": "approved", "topic": "science", "learning_outcome": "x", "rank": 2, "is_official": True,
    })
    body = DeckCurationRequest(status="approved", topic="science", learning_outcome="x", rank=2)
    await public_content.set_deck_curation("d1", body, current_user={"user_id": "admin-1"}, service=service)
    assert [(r["actor_id"], r["action"], r["target_type"], r["target_id"]) for r in log.rows] == [("admin-1", "curation.review", "deck", "d1")]
    assert log.rows[0]["details"] == {"status": "approved", "topic": "science", "rank": 2}


@pytest.mark.asyncio
async def test_a_single_invitation_and_a_revoke_are_logged(monkeypatch):
    from app.models.Beta import InviteCreate
    from app.routers import beta_admin
    from app.services import beta_invites

    log = FakeCollection()
    monkeypatch.setattr(admin_actions, "admin_actions_collection", log)
    monkeypatch.setattr(beta_invites, "invites_collection", FakeCollection())
    created = await beta_admin.create_invite(InviteCreate(email="ana@example.com", cohort="friends"), admin={"user_id": "admin-1"})
    await beta_admin.revoke_invite(created.id, admin={"user_id": "admin-1"})
    assert [(r["action"], r["target_type"], r["target_id"], r["target_title"]) for r in log.rows] == [
        ("invite.create", "invite", created.id, "ana@example.com"),
        ("invite.revoke", "invite", created.id, "ana@example.com"),
    ]
    assert log.rows[0]["details"] == {"cohort": "friends"}
