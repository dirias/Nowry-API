"""
WAIT-014..015 — the waitlist answers everyone alike and covers each case after the answer
(docs/prd-road-to-market.md FR-034, ADR-084).

  1. The answer: the same status and body for every address, and nothing is
     looked up or written before it.
  2. The cases, after the answer: new, waiting, left, an account (any case),
     a deleted account.
  3. The account letter: once in a rolling 24 hours, the confirmation share,
     never to a suppressed address.
  4. Its words: five languages, a sign-in button, a Google line, no leave link.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.routers.beta as beta_module
from app.emails.render import KINDS, LOCALES, render_email
from app.models.Beta import WaitlistCreate
from app.services import email as email_service
from app.services import email_suppressions, waitlist
from app.utils import keyed_rate_limit
from tests._fake_collection import FakeCollection

ENV = {
    "RESEND_API_KEY": "re_test",
    "EMAIL_FROM_PERSONAL": "Didier from Nowry <hello@nowry.app>",
    "FRONTEND_URL": "https://www.nowry.app",
    "API_PUBLIC_URL": "https://api.nowry.app",
}
CLEARED = ("EMAIL_ALLOWLIST", "EMAIL_DAILY_BUDGET", "EMAIL_POSTAL_ADDRESS", "EMAIL_MARK_URL", "RAILWAY_PUBLIC_DOMAIN")
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _form(email: str = "Ana@Example.com", locale: str = "es") -> WaitlistCreate:
    return WaitlistCreate(email=email, locale=locale, source="/")


def _resend():
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=MagicMock(status_code=200, text="x", headers={}))
    return client


class _Claims:
    """A rolling-window store: a key is free once its last claim is older than the window."""

    def __init__(self):
        self.last = {}
        self.now = NOW

    async def claim(self, feature, key, window_seconds, now=None):
        at = now or self.now
        seen = self.last.get((feature, key))
        if seen and seen > at - timedelta(seconds=window_seconds):
            return False
        self.last[(feature, key)] = at
        return True


@pytest.fixture
def world(monkeypatch):
    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    ledger = MagicMock(sent_today=AsyncMock(return_value=0), record=AsyncMock(), note_resend_count=AsyncMock())
    for name in ("sent_today", "record", "note_resend_count"):
        monkeypatch.setattr(email_service.email_ledger, name, getattr(ledger, name))
    monkeypatch.setattr(email_suppressions, "email_suppressions_collection", FakeCollection())
    claims = _Claims()
    monkeypatch.setattr(waitlist, "claim_rolling", claims.claim)
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr(beta_module, "notify", notice)
    resend = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=resend):
        yield MagicMock(rows=FakeCollection(), users=FakeCollection(), ledger=ledger, claims=claims, notice=notice, resend=resend)


def _sent_kinds(world):
    """The kinds Resend was asked to send, read from the ledger's records."""
    return [call.args[0] for call in world.ledger.record.await_args_list if call.args[2] == "sent"]


async def _submit(world, form=None):
    await beta_module.after_waitlist_form(form or _form(), world.rows, world.users)


# ── 1. The answer ────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    """The route alone, with stores that fail on any touch: the answer must not need them."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.limiter import limiter

    untouchable = MagicMock(side_effect=AssertionError("looked up before the answer"))
    for name in ("waitlist_collection", "users_collection"):
        store = MagicMock()
        for method in ("find_one", "find_one_and_update", "update_one", "insert_one", "count_documents"):
            setattr(store, method, untouchable)
        monkeypatch.setattr(beta_module, name, store)
    after = AsyncMock()
    monkeypatch.setattr(beta_module, "after_waitlist_form", after)
    # The limiter the route was decorated with (another test may have re-imported the module's).
    monkeypatch.setattr(beta_module.limiter, "enabled", False)  # the per-caller limit is not what this compares
    monkeypatch.setattr(limiter, "enabled", False)
    api = FastAPI()
    api.state.limiter = limiter
    api.include_router(beta_module.router)
    return MagicMock(http=TestClient(api), after=after, untouchable=untouchable)


def test_every_address_gets_one_answer_and_nothing_is_looked_up_before_it(client):
    addresses = ["new@example.com", "waiting@example.com", "left@example.com", "Member@Example.com"]
    answers = [client.http.post("/beta/waitlist", json={"email": a, "locale": "en", "source": "/"}) for a in addresses]
    assert {(a.status_code, a.text) for a in answers} == {(201, answers[0].text)}
    client.untouchable.assert_not_called()
    assert [call.args[0].email.lower() for call in client.after.await_args_list] == [a.lower() for a in addresses]


# ── 2. The cases ─────────────────────────────────────────────────────────────


async def test_a_new_address_is_added_the_operator_told_and_one_confirmation_sent(world):
    await _submit(world)
    assert [row["email"] for row in world.rows.rows] == ["ana@example.com"]
    world.notice.assert_awaited_once()
    assert _sent_kinds(world) == ["waitlist.confirm"]


async def test_an_address_already_waiting_is_sent_nothing_more(world):
    await _submit(world)
    await _submit(world)
    assert len(world.rows.rows) == 1 and world.notice.await_count == 1
    assert _sent_kinds(world) == ["waitlist.confirm"]


async def test_an_address_that_left_is_waiting_again_and_sent_nothing(world):
    await _submit(world)
    world.rows.rows[0]["status"] = waitlist.LEFT
    await _submit(world)
    assert world.rows.rows[0]["status"] == waitlist.WAITING
    assert _sent_kinds(world) == ["waitlist.confirm"]


async def test_an_account_holder_is_not_added_and_is_sent_the_account_letter(world):
    world.users.rows.append({"_id": "u1", "email": "ana@example.com", "deleted_at": None})
    await _submit(world, _form("ANA@example.COM", "ja"))
    assert world.rows.rows == []
    world.notice.assert_not_awaited()
    assert _sent_kinds(world) == ["waitlist.account"]
    record = world.ledger.record.await_args
    assert record.args[1] == "confirmation" and record.args[3] == "ja"


async def test_a_deleted_account_counts_as_none(world):
    world.users.rows.append({"_id": "u1", "email": "ana@example.com", "deleted_at": NOW})
    await _submit(world)
    assert [row["email"] for row in world.rows.rows] == ["ana@example.com"]
    assert _sent_kinds(world) == ["waitlist.confirm"]


async def test_a_failure_after_the_answer_never_raises(world):
    world.users.find_one = AsyncMock(side_effect=RuntimeError("database down"))
    await _submit(world)  # nothing raised
    assert _sent_kinds(world) == []


# ── 3. The letter's limits ───────────────────────────────────────────────────


async def test_the_letter_goes_once_in_a_rolling_24_hours(world):
    world.users.rows.append({"_id": "u1", "email": "ana@example.com", "deleted_at": None})
    await _submit(world)
    world.claims.now = NOW + timedelta(hours=23, minutes=59)
    await _submit(world)
    assert _sent_kinds(world) == ["waitlist.account"]
    world.claims.now = NOW + timedelta(hours=24, minutes=1)
    await _submit(world)
    assert _sent_kinds(world) == ["waitlist.account", "waitlist.account"]


async def test_the_letter_is_never_sent_to_a_suppressed_address(world):
    world.users.rows.append({"_id": "u1", "email": "ana@example.com", "deleted_at": None})
    await email_suppressions.suppress("ana@example.com", "bounce")
    await _submit(world)
    world.resend.post.assert_not_awaited()
    assert world.ledger.record.await_args.args[2] == email_service.SUPPRESSED


async def test_the_letter_stops_with_the_confirmation_share(world, monkeypatch):
    monkeypatch.setenv("EMAIL_DAILY_BUDGET", "90")
    world.ledger.sent_today.return_value = 54  # 60% of 90: the confirmation share is spent
    world.users.rows.append({"_id": "u1", "email": "ana@example.com", "deleted_at": None})
    await _submit(world)
    world.resend.post.assert_not_awaited()
    assert world.ledger.record.await_args.args[2] == email_service.OVER_BUDGET


async def test_the_rolling_claim_is_one_atomic_upsert_and_a_collision_is_a_no(monkeypatch):
    from pymongo.errors import DuplicateKeyError

    store = MagicMock(find_one_and_update=AsyncMock(return_value=None))
    monkeypatch.setattr(keyed_rate_limit, "rate_limits_collection", store)
    assert await keyed_rate_limit.claim_rolling("f", "k", 86400, NOW) is True
    query, update = store.find_one_and_update.await_args.args
    assert query["_id"] == "f:k" and query["$or"][1] == {"last_at": {"$lt": NOW - timedelta(days=1)}}
    assert update["$set"]["expires_at"] == NOW + timedelta(days=1)
    store.find_one_and_update.side_effect = DuplicateKeyError("inside the window")
    assert await keyed_rate_limit.claim_rolling("f", "k", 86400, NOW) is False
    store.find_one_and_update.side_effect = RuntimeError("down")
    assert await keyed_rate_limit.claim_rolling("f", "k", 86400, NOW) is False


# ── 4. The words ─────────────────────────────────────────────────────────────


def test_the_letter_is_a_personal_letter_in_the_confirmation_class_without_a_leave_link():
    assert KINDS["waitlist.account"]["class"] == "confirmation"
    assert KINDS["waitlist.account"]["sender"] == "personal"


@pytest.mark.parametrize("locale", LOCALES)
def test_the_letter_renders_in_every_language_with_a_sign_in_button_and_a_google_line(locale):
    values = {"email": "ana@example.com", "support": "hello@nowry.app", "link": "https://www.nowry.app/login"}
    rendered = render_email("waitlist.account", locale, values, logo_url="https://nowry.app/logo192.png")
    for part in (rendered.subject, rendered.text, rendered.html):
        assert "$" not in part
    assert "https://www.nowry.app/login" in rendered.text.splitlines()
    assert 'href="https://www.nowry.app/login"' in rendered.html
    assert "Google" in rendered.text and "Google" in rendered.html
    assert "waitlist/leave" not in rendered.html and "waitlist/leave" not in rendered.text
    assert rendered.html.count("<img ") == 1 and f'<html lang="{locale}">' in rendered.html
