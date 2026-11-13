"""WAIT-017 (FR-034 reworded) and E2E-005 (ADR-090 amended).

WAIT-017: "an address already waiting is sent nothing" means no second copy of
a confirmation that was delivered. One that never left (budget spent, send
failed) is sent by a later join, at most one try per address in 24 hours.

E2E-005: on development, the suite's own aliases are delivered to the dev
outbox only: never posted to Resend, never counted in the daily budget, and
counted as delivered by every caller. Every other address sends for real.
"""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.services import dev_outbox
from app.services import email as email_service
from app.services import waitlist
from tests.test_mail008_wait004 import ANA, NOW, _form, _resend, world  # noqa: F401 - `world` is a fixture

SUITE = "rikydier+e2e-abc123-wait@gmail.com"


# ── WAIT-017 ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_confirmation_that_never_left_is_sent_by_a_later_join(world):
    await waitlist.join(_form(), world.rows, NOW)
    refused = _resend(status=500)
    with patch.object(email_service.httpx, "AsyncClient", return_value=refused):
        assert await waitlist.send_confirmation(ANA, world.rows, NOW) == email_service.FAILED
    later = NOW + timedelta(hours=25)
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        await waitlist.join(_form(), world.rows, later)
        assert await waitlist.send_confirmation(ANA, world.rows, later) == email_service.SENT
        await waitlist.join(_form(), world.rows, later + timedelta(hours=30))
        assert await waitlist.send_confirmation(ANA, world.rows, later + timedelta(hours=30)) == "skipped"
    assert client.post.await_count == 1
    assert world.rows.rows[0]["confirmation_sent_at"] == later


@pytest.mark.asyncio
async def test_a_retry_waits_24_hours_from_the_last_try(world):
    world.ledger.sent_today = AsyncMock(return_value=10_000)
    with patch.object(email_service.email_ledger, "sent_today", world.ledger.sent_today):
        await waitlist.join(_form(), world.rows, NOW)
        assert await waitlist.send_confirmation(ANA, world.rows, NOW) == email_service.OVER_BUDGET
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        assert await waitlist.send_confirmation(ANA, world.rows, NOW + timedelta(hours=1)) == "limited"
        assert await waitlist.send_confirmation(ANA, world.rows, NOW + timedelta(hours=23)) == "limited"
    client.post.assert_not_awaited()
    assert "confirmation_sent_at" not in world.rows.rows[0]


@pytest.mark.asyncio
async def test_a_delivered_confirmation_is_never_sent_twice(world):
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        for hours in (0, 25, 72):
            await waitlist.join(_form(), world.rows, NOW + timedelta(hours=hours))
            await waitlist.send_confirmation(ANA, world.rows, NOW + timedelta(hours=hours))
    assert client.post.await_count == 1


# ── E2E-005 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "env, address, expected",
    [
        ("development", SUITE, True),
        ("development", "RIKYDIER+E2E-abc-x@gmail.com", True),
        ("development", "rikydier+manual@gmail.com", False),
        ("development", "rikydier@gmail.com", False),
        ("development", "someone+e2e-x@gmail.com", False),
        ("production", SUITE, False),
    ],
)
def test_only_the_suites_aliases_stay_in_the_outbox_and_never_in_production(monkeypatch, env, address, expected):
    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("DEV_OUTBOX", "1")
    assert dev_outbox.outbox_only(address) is expected


def test_without_the_outbox_nothing_is_held_back(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("DEV_OUTBOX", "")
    assert dev_outbox.outbox_only(SUITE) is False


@pytest.mark.asyncio
async def test_a_suite_alias_is_kept_not_sent_and_not_counted(world, monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("DEV_OUTBOX", "1")
    monkeypatch.setenv("EMAIL_DAILY_BUDGET", "1")
    world.ledger.sent_today = AsyncMock(return_value=10_000)  # the day's allowance is spent
    monkeypatch.setattr(email_service.email_ledger, "sent_today", world.ledger.sent_today)
    keep = AsyncMock()
    monkeypatch.setattr(dev_outbox, "keep", keep)
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        outcome = await email_service.send_email("waitlist.confirm", SUITE, "en", {"leave_link": "https://x/l"})
    assert outcome == email_service.OUTBOX
    assert email_service.delivered(outcome)
    client.post.assert_not_awaited()
    keep.assert_awaited_once()
    assert world.ledger.record.await_args.args[2] == email_service.OUTBOX  # recorded, and `sent_today` counts only "sent"


@pytest.mark.asyncio
async def test_a_suite_confirmation_counts_as_delivered(world, monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("DEV_OUTBOX", "1")
    monkeypatch.setattr(dev_outbox, "keep", AsyncMock())
    form = _form(email=SUITE, locale="en")
    await waitlist.join(form, world.rows, NOW)
    assert await waitlist.send_confirmation(SUITE, world.rows, NOW) == email_service.OUTBOX
    assert world.rows.rows[0]["confirmation_sent_at"] == NOW
    await waitlist.join(form, world.rows, NOW + timedelta(hours=30))
    assert await waitlist.send_confirmation(SUITE, world.rows, NOW + timedelta(hours=30)) == "skipped"


@pytest.mark.asyncio
async def test_a_manual_alias_still_sends_for_real(world, monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("DEV_OUTBOX", "1")
    monkeypatch.setattr(dev_outbox, "keep", AsyncMock())
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        outcome = await email_service.send_email("waitlist.confirm", "rikydier+manual@gmail.com", "en", {"leave_link": "https://x/l"})
    assert outcome == email_service.SENT
    client.post.assert_awaited_once()
