"""
WAIT-007 — an invited address that signs up makes its waitlist row `joined` (docs/prd-admin.md FR-012).

Pins:

  1. Settling a spent personal invitation ties it to the account (as before) and turns the
     waitlist row with that address, waiting or invited, into `joined` with `joined_at`.
  2. A `left` row stays `left`; a row already `joined` keeps its first `joined_at`.
  3. No row, or a cohort code (no invitation), changes nothing.
  4. A failing waitlist write never reaches sign-up: it is logged as a warning.
"""
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.services import beta_invites, waitlist
from tests._fake_collection import FakeCollection

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
ADMIN = "65f0a1b2c3d4e5f6a7b8c9d0"
USER = "65f0a1b2c3d4e5f6a7b8c9d9"


@pytest.fixture
def stores(monkeypatch):
    invites, rows = FakeCollection(), FakeCollection()
    monkeypatch.setattr(beta_invites, "invites_collection", invites)
    monkeypatch.setattr(waitlist, "waitlist_collection", rows)
    return invites, rows


async def spent(invites, email="ana@x.com"):
    row, _ = await beta_invites.create_invite(email, "c1-waitlist", ADMIN, now=NOW - timedelta(days=2))
    return {"cohort": "c1-waitlist", "invite_id": str(row["_id"]), "joined_at": NOW}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting", "invited"])
async def test_a_spent_invitation_makes_the_row_joined(stores, status):
    invites, rows = stores
    await rows.insert_one({"email": "ana@x.com", "status": status})
    stamp = await spent(invites)
    await beta_invites.settle_personal_invite(stamp, USER, "uid-1", now=NOW)
    assert invites.rows[0]["status"] == "used" and invites.rows[0]["used_by"] == USER
    assert rows.rows[0]["status"] == "joined" and rows.rows[0]["joined_at"] == NOW and rows.rows[0]["updated_at"] == NOW


@pytest.mark.asyncio
async def test_a_left_row_stays_left(stores):
    invites, rows = stores
    await rows.insert_one({"email": "ana@x.com", "status": "left", "left_at": NOW - timedelta(days=1)})
    await beta_invites.settle_personal_invite(await spent(invites), USER, "uid-1", now=NOW)
    assert rows.rows[0]["status"] == "left" and "joined_at" not in rows.rows[0]
    assert invites.rows[0]["status"] == "used"


@pytest.mark.asyncio
async def test_a_joined_row_keeps_its_first_date(stores):
    invites, rows = stores
    first = NOW - timedelta(days=30)
    await rows.insert_one({"email": "ana@x.com", "status": "joined", "joined_at": first})
    await beta_invites.settle_personal_invite(await spent(invites), USER, "uid-1", now=NOW)
    assert rows.rows[0]["joined_at"] == first


@pytest.mark.asyncio
async def test_only_the_invited_address_is_touched_and_no_row_is_made(stores):
    invites, rows = stores
    await rows.insert_one({"email": "other@x.com", "status": "invited"})
    await beta_invites.settle_personal_invite(await spent(invites), USER, "uid-1", now=NOW)
    assert len(rows.rows) == 1 and rows.rows[0]["status"] == "invited" and "joined_at" not in rows.rows[0]


@pytest.mark.asyncio
async def test_a_cohort_code_or_no_gate_changes_nothing(stores):
    invites, rows = stores
    await rows.insert_one({"email": "ana@x.com", "status": "waiting"})
    await beta_invites.settle_personal_invite(None, USER, "uid-1", now=NOW)
    await beta_invites.settle_personal_invite({"cohort": "friends", "joined_at": NOW}, USER, "uid-1", now=NOW)
    assert rows.rows[0]["status"] == "waiting"


@pytest.mark.asyncio
async def test_a_failing_waitlist_write_never_reaches_sign_up(stores, monkeypatch, caplog):
    invites, rows = stores
    monkeypatch.setattr(rows, "update_one", AsyncMock(side_effect=RuntimeError("mongo down")))
    stamp = await spent(invites)
    with caplog.at_level(logging.WARNING):
        await beta_invites.settle_personal_invite(stamp, USER, "uid-1", now=NOW)
    assert invites.rows[0]["status"] == "used"
    assert any("joined" in record.getMessage() and record.levelno == logging.WARNING for record in caplog.records)
    assert "ana@x.com" not in caplog.text
