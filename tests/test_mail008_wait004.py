"""
MAIL-008 and WAIT-004 — leaving the waitlist, and its one confirmation
(docs/prd-road-to-market.md FR-026, FR-027; ADR-078 point 1, ADR-080).

  1. The words: a letter to a visitor has no button and always a way to leave.
  2. The confirmation: once per address, ever; never after leaving, a bounce
     or a complaint; released when it did not leave; inside the day's share.
  3. Leaving: reading changes nothing, the POST does, any token gets one answer.
  4. The webhook: a signature or nothing; a permanent bounce and a complaint mark.
  5. The routes, end to end on a bare app.
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.emails.render import KINDS, LOCALES, render_email
from app.models.Beta import WaitlistCreate
from app.routers import email_webhooks
from app.services import email as email_service
from app.services import email_suppressions, waitlist
from app.services.email_ledger import recipient_hash
from tests._fake_collection import FakeCollection

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
ANA = "ana@example.com"
LEAVE = "https://www.nowry.app/waitlist/leave/tok"
VALUES = {"email": ANA, "support": "hello@nowry.app", "leave_link": LEAVE}
ENV = {
    "RESEND_API_KEY": "re_test",
    "EMAIL_FROM_PERSONAL": "Didier from Nowry <hello@nowry.app>",
    "FRONTEND_URL": "https://www.nowry.app",
    "API_PUBLIC_URL": "https://api.nowry.app",
}
CLEARED = ("EMAIL_ALLOWLIST", "EMAIL_DAILY_BUDGET", "EMAIL_POSTAL_ADDRESS", "EMAIL_MARK_URL", "RAILWAY_PUBLIC_DOMAIN", "RESEND_WEBHOOK_SECRET")


def _form(email: str = "Ana@Example.com", locale: str = "es") -> WaitlistCreate:
    return WaitlistCreate(email=email, locale=locale, source="/")


def _resend(status: int = 200):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=MagicMock(status_code=status, text="x", headers={}))
    return client


@pytest.fixture
def world(monkeypatch):
    """Settings for a visitor email, a ledger and a suppression list that touch no database."""
    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    ledger = MagicMock(sent_today=AsyncMock(return_value=0), record=AsyncMock(), note_resend_count=AsyncMock())
    for name in ("sent_today", "record", "note_resend_count"):
        monkeypatch.setattr(email_service.email_ledger, name, getattr(ledger, name))
    suppressions = FakeCollection()
    monkeypatch.setattr(email_suppressions, "email_suppressions_collection", suppressions)
    return MagicMock(ledger=ledger, suppressions=suppressions, rows=FakeCollection())


# ── 1. The words ─────────────────────────────────────────────────────────────


def test_the_confirmation_is_a_visitor_letter_in_the_confirmation_class():
    assert KINDS["waitlist.confirm"] == {"class": "confirmation", "sender": "personal"}


@pytest.mark.parametrize("locale", LOCALES)
def test_the_confirmation_has_no_button_and_the_leave_link_in_both_parts(locale):
    rendered = render_email("waitlist.confirm", locale, VALUES, logo_url="https://www.nowry.app/logo192.png")
    assert rendered.locale == locale and rendered.subject
    assert LEAVE in rendered.text.splitlines()  # alone on its line
    assert f'href="{LEAVE}"' in rendered.html
    assert 'class="btn"' not in rendered.html
    assert "$" not in rendered.text and "$" not in rendered.html.split("</style>")[1]
    assert ANA in rendered.text  # the footer says which address this was sent to


def test_a_visitor_letter_without_a_leave_link_is_a_fault():
    with pytest.raises(KeyError):
        render_email("waitlist.confirm", "en", {"email": ANA, "support": "s"}, logo_url="x")


def test_an_account_email_never_carries_a_leave_link():
    values = {**VALUES, "link": "https://www.nowry.app/auth/action?mode=verifyEmail&oobCode=abc"}
    rendered = render_email("account.verify", "en", values, logo_url="x")
    assert LEAVE not in rendered.text and LEAVE not in rendered.html


@pytest.mark.parametrize("locale, name", [("en", "Didier from Nowry"), ("es", "Didier de Nowry"), ("de", "Didier von Nowry"), ("xx", "Didier from Nowry")])
def test_the_personal_sender_keeps_the_address_and_translates_the_name(locale, name):
    assert email_service.personal_sender("Anything <hello@nowry.app>", locale) == f"{name} <hello@nowry.app>"
    assert email_service.personal_sender("", locale) == ""


def test_the_unsubscribe_headers_are_the_one_click_pair():
    assert waitlist.unsubscribe_headers("https://api.nowry.app/beta/waitlist/leave/tok") == {
        "List-Unsubscribe": "<https://api.nowry.app/beta/waitlist/leave/tok>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def test_the_api_address_is_the_setting_or_railways_domain(monkeypatch):
    monkeypatch.setenv("API_PUBLIC_URL", "https://api.nowry.app/")
    assert waitlist.api_public_url() == "https://api.nowry.app"
    monkeypatch.delenv("API_PUBLIC_URL")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "nowry.up.railway.app")
    assert waitlist.api_public_url() == "https://nowry.up.railway.app"
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN")
    assert waitlist.api_public_url() == "" and waitlist.leave_links("tok") is None


# ── 2. The confirmation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_new_address_is_sent_one_confirmation_with_its_leave_link_and_headers(world):
    _, is_new = await waitlist.join(_form(), world.rows, NOW)
    token = world.rows.rows[0]["leave_token"]
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        outcome = await waitlist.send_confirmation(ANA, world.rows, NOW)

    assert is_new and outcome == email_service.SENT
    payload = client.post.await_args.kwargs["json"]
    assert payload["to"] == [ANA] and payload["from"] == "Didier de Nowry <hello@nowry.app>"  # the row's language
    assert payload["subject"] == "Estás en la lista de espera de Nowry"
    assert f"https://www.nowry.app/waitlist/leave/{token}" in payload["text"]
    assert "hello@nowry.app" in payload["text"] and "<hello@nowry.app>" not in payload["text"]  # the footer names the address alone
    assert payload["headers"] == waitlist.unsubscribe_headers(f"https://api.nowry.app/beta/waitlist/leave/{token}")
    row = world.rows.rows[0]
    assert row["confirmation_sent_at"] == NOW and "confirmation_claimed_at" not in row
    assert world.ledger.record.await_args.args[:3] == ("waitlist.confirm", "confirmation", "sent")


@pytest.mark.asyncio
async def test_a_repeat_submission_sends_nothing_more(world):
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        for _ in range(3):
            await waitlist.join(_form(), world.rows, NOW)
            await waitlist.send_confirmation(ANA, world.rows, NOW)
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_leaving_and_joining_again_sends_no_second_confirmation(world):
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        await waitlist.join(_form(), world.rows, NOW)
        await waitlist.send_confirmation(ANA, world.rows, NOW)
        token = world.rows.rows[0]["leave_token"]
        assert await waitlist.leave(token, world.rows, NOW)
        _, is_new = await waitlist.join(_form(), world.rows, NOW + timedelta(days=3))
        outcome = await waitlist.send_confirmation(ANA, world.rows, NOW + timedelta(days=3))

    row = world.rows.rows[0]
    assert not is_new and outcome == "skipped" and client.post.await_count == 1
    assert row["status"] == "waiting" and "left_at" not in row and row["leave_token"] == token


@pytest.mark.asyncio
async def test_an_address_that_left_before_its_confirmation_is_not_written_to(world):
    await waitlist.join(_form(), world.rows, NOW)
    await waitlist.leave(world.rows.rows[0]["leave_token"], world.rows, NOW)
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        assert await waitlist.send_confirmation(ANA, world.rows, NOW) == "skipped"
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_confirmation_that_did_not_leave_is_released_and_sent_on_a_later_submission(world):
    await waitlist.join(_form(), world.rows, NOW)
    world.ledger.sent_today.return_value = 54  # the confirmation class's share of 90
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        assert await waitlist.send_confirmation(ANA, world.rows, NOW) == email_service.OVER_BUDGET
        assert "confirmation_sent_at" not in world.rows.rows[0] and "confirmation_claimed_at" not in world.rows.rows[0]
        world.ledger.sent_today.return_value = 0
        assert await waitlist.send_confirmation(ANA, world.rows, NOW + timedelta(days=1)) == email_service.SENT
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_a_confirmation_being_sent_by_another_worker_is_not_sent_twice(world):
    await waitlist.join(_form(), world.rows, NOW)
    world.rows.rows[0]["confirmation_claimed_at"] = NOW - timedelta(minutes=1)
    assert await waitlist.send_confirmation(ANA, world.rows, NOW) == "skipped"
    # A claim left by a worker that died is taken again after ten minutes.
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        assert await waitlist.send_confirmation(ANA, world.rows, NOW + timedelta(minutes=11)) == email_service.SENT


@pytest.mark.asyncio
async def test_a_row_older_than_the_leave_token_is_given_one_when_first_written_to(world):
    await world.rows.insert_one({"email": ANA, "locale": "en", "status": "waiting", "created_at": NOW})
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        assert await waitlist.send_confirmation(ANA, world.rows, NOW) == email_service.SENT
    token = world.rows.rows[0]["leave_token"]
    assert waitlist.looks_like_token(token) and token in client.post.await_args.kwargs["json"]["text"]


@pytest.mark.asyncio
async def test_without_a_public_api_address_no_confirmation_is_sent(world, monkeypatch):
    monkeypatch.delenv("API_PUBLIC_URL")
    await waitlist.join(_form(), world.rows, NOW)
    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        assert await waitlist.send_confirmation(ANA, world.rows, NOW) == email_service.UNCONFIGURED
    client.post.assert_not_awaited()
    assert "confirmation_claimed_at" not in world.rows.rows[0]


@pytest.mark.asyncio
async def test_a_failure_while_sending_is_an_outcome_never_an_error(world):
    broken = MagicMock()
    broken.find_one_and_update = AsyncMock(side_effect=RuntimeError("database away"))
    assert await waitlist.send_confirmation(ANA, broken, NOW) == email_service.FAILED


@pytest.mark.asyncio
async def test_a_bounced_or_complained_address_gets_no_visitor_letter_but_still_gets_account_email(world, monkeypatch):
    await waitlist.join(_form(), world.rows, NOW)
    await waitlist.mark_suppressed("Ana@Example.com", email_suppressions.COMPLAINED, world.rows, world.suppressions, NOW)

    row = world.rows.rows[0]
    assert row["suppressed"] == "complained" and row["suppressed_at"] == NOW
    stored = world.suppressions.rows[0]
    assert stored["_id"] == recipient_hash(ANA) and ANA not in json.dumps(stored, default=str)  # by hash, never the address

    client = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=client):
        # The row is marked, so nothing is owed; and were it asked directly, the list refuses.
        assert await waitlist.send_confirmation(ANA, world.rows, NOW) == "skipped"
        assert await email_service.send_email("waitlist.confirm", ANA, "en", {"leave_link": LEAVE}) == email_service.SUPPRESSED
        client.post.assert_not_awaited()
        monkeypatch.setenv("EMAIL_FROM_ACCOUNT", "Nowry <account@nowry.app>")
        link = "https://www.nowry.app/auth/action?mode=resetPassword&oobCode=abc"
        assert await email_service.send_email("account.reset", ANA, "en", {"link": link}) == email_service.SENT


@pytest.mark.asyncio
async def test_a_suppression_list_that_cannot_be_read_sends_nothing():
    broken = MagicMock()
    broken.find_one = AsyncMock(side_effect=RuntimeError("database away"))
    assert await email_suppressions.is_suppressed(ANA, broken) is True


# ── 3. Leaving ───────────────────────────────────────────────────────────────


def test_an_address_is_masked_to_its_first_letter():
    assert waitlist.mask_email("mika@example.com") == "m•••@example.com"
    assert waitlist.mask_email("not-an-address") == "•••"


@pytest.mark.asyncio
async def test_reading_a_token_changes_nothing_and_shows_a_masked_address(world):
    await waitlist.join(_form(), world.rows, NOW)
    token = world.rows.rows[0]["leave_token"]
    assert await waitlist.leave_hint(token, world.rows) == {"email_hint": "a•••@example.com", "locale": "es"}
    assert world.rows.rows[0]["status"] == "waiting"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "short", "x" * 200, "has spaces in the token....", "a" * 43])
async def test_an_unknown_or_misshapen_token_shows_nothing_and_removes_no_one(world, token):
    await waitlist.join(_form(), world.rows, NOW)
    assert await waitlist.leave_hint(token, world.rows) is None
    assert await waitlist.leave(token, world.rows, NOW) is False
    assert world.rows.rows[0]["status"] == "waiting"


@pytest.mark.asyncio
async def test_leaving_marks_the_row_and_keeps_it(world):
    await waitlist.join(_form(), world.rows, NOW)
    token = world.rows.rows[0]["leave_token"]
    assert await waitlist.leave(token, world.rows, NOW + timedelta(hours=1)) is True
    row = world.rows.rows[0]
    assert (row["status"], row["left_at"], row["email"]) == ("left", NOW + timedelta(hours=1), ANA)
    # A second press changes nothing.
    assert await waitlist.leave(token, world.rows, NOW + timedelta(hours=2)) is False
    assert row["left_at"] == NOW + timedelta(hours=1)


# ── 4. The webhook ───────────────────────────────────────────────────────────

SECRET = "whsec_" + base64.b64encode(b"a-test-signing-key-32-bytes-long").decode()


def _sign(body: bytes, message_id: str = "msg_1", at: int = 1_800_000_000) -> dict:
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    digest = hmac.new(key, f"{message_id}.{at}.".encode() + body, hashlib.sha256).digest()
    return {"svix-id": message_id, "svix-timestamp": str(at), "svix-signature": "v1," + base64.b64encode(digest).decode()}


def test_a_request_is_verified_by_its_signature_and_its_age():
    body = b'{"type":"email.bounced"}'
    signed = _sign(body)
    args = (signed["svix-id"], signed["svix-timestamp"], signed["svix-signature"], body)
    assert email_webhooks.verified(SECRET, *args, now=1_800_000_010)
    assert email_webhooks.verified(SECRET, signed["svix-id"], signed["svix-timestamp"], "v1,other " + signed["svix-signature"], body, now=1_800_000_010)
    assert not email_webhooks.verified(SECRET, *args, now=1_800_000_000 + 301)  # too old
    assert not email_webhooks.verified(SECRET, signed["svix-id"], signed["svix-timestamp"], signed["svix-signature"], body + b" ", now=1_800_000_010)
    assert not email_webhooks.verified(SECRET, "msg_2", signed["svix-timestamp"], signed["svix-signature"], body, now=1_800_000_010)
    assert not email_webhooks.verified("", *args, now=1_800_000_010)  # no secret set
    assert not email_webhooks.verified("whsec_not base64!", *args, now=1_800_000_010)
    assert not email_webhooks.verified(SECRET, "msg_1", "yesterday", signed["svix-signature"], body, now=1_800_000_010)


@pytest.mark.parametrize(
    "event, reason",
    [
        ({"type": "email.complained", "data": {"to": [ANA]}}, "complained"),
        ({"type": "email.bounced", "data": {"to": [ANA], "bounce": {"type": "Permanent"}}}, "bounced"),
        ({"type": "email.bounced", "data": {"to": [ANA]}}, "bounced"),
        ({"type": "email.bounced", "data": {"to": [ANA], "bounce": {"type": "Transient"}}}, None),
        ({"type": "email.delivered", "data": {"to": [ANA]}}, None),
        ({"type": "email.opened"}, None),
    ],
)
def test_only_a_permanent_bounce_and_a_complaint_mark(event, reason):
    assert email_webhooks.suppression_reason(event) == reason


def test_recipients_are_read_from_a_list_or_a_single_address():
    assert email_webhooks.recipients({"data": {"to": [ANA, 7, "nope"]}}) == [ANA]
    assert email_webhooks.recipients({"data": {"to": ANA}}) == [ANA]
    assert email_webhooks.recipients({}) == []


# ── 5. The routes ────────────────────────────────────────────────────────────


@pytest.fixture
def client(world, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware

    import app.routers.beta as beta_module
    from app.core.limiter import limiter

    monkeypatch.setattr(beta_module, "waitlist_collection", world.rows)
    monkeypatch.setattr(beta_module, "notify", AsyncMock(return_value=True))
    monkeypatch.setattr(email_webhooks, "waitlist_collection", world.rows)
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", SECRET)
    limiter.reset()
    api = FastAPI()
    api.state.limiter = limiter
    api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    api.add_middleware(SlowAPIMiddleware)
    api.include_router(beta_module.router)
    api.include_router(email_webhooks.router)
    resend = _resend()
    with patch.object(email_service.httpx, "AsyncClient", return_value=resend):
        yield MagicMock(http=TestClient(api), resend=resend, world=world)


def _seed(client, locale: str = "en") -> str:
    """A waiting row put straight on the list, so the form's own limit (5 a minute) is not spent on set-up."""
    token = waitlist.new_leave_token()
    client.world.rows.rows.append({"_id": token, "email": ANA, "locale": locale, "status": "waiting", "leave_token": token, "created_at": NOW})
    return token


def test_joining_answers_the_same_and_confirms_once(client):
    form = {"email": "Ana@Example.com", "locale": "es", "source": "/"}
    answers = [client.http.post("/beta/waitlist", json=form) for _ in range(3)]
    assert {(a.status_code, a.text) for a in answers} == {(201, answers[0].text)}
    assert client.resend.post.await_count == 1


def test_the_leave_page_reads_then_leaves_and_any_token_gets_one_answer(client):
    token = _seed(client, "ja")

    assert client.http.get(f"/beta/waitlist/leave/{token}").json() == {"email_hint": "a•••@example.com", "locale": "ja"}
    assert client.http.get(f"/beta/waitlist/leave/{'z' * 43}").json() == {"email_hint": None, "locale": None}
    assert client.world.rows.rows[0]["status"] == "waiting"  # reading removed no one

    known = client.http.post(f"/beta/waitlist/leave/{token}")
    again = client.http.post(f"/beta/waitlist/leave/{token}")
    unknown = client.http.post(f"/beta/waitlist/leave/{'z' * 43}")
    assert {(r.status_code, r.text) for r in (known, again, unknown)} == {(200, '{"left":true}')}
    assert client.world.rows.rows[0]["status"] == "left"


def test_a_mail_providers_one_click_post_leaves(client):
    token = _seed(client)
    response = client.http.post(
        f"/beta/waitlist/leave/{token}", content="List-Unsubscribe=One-Click", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 200 and client.world.rows.rows[0]["status"] == "left"


def test_the_webhook_refuses_without_a_signature_and_marks_with_one(client, monkeypatch):
    import time

    _seed(client)
    body = json.dumps({"type": "email.complained", "data": {"to": [ANA]}}).encode()

    assert client.http.post("/webhooks/resend", content=body).status_code == 401
    assert client.http.post("/webhooks/resend", content=body, headers={**_sign(body, at=int(time.time())), "svix-signature": "v1,AAAA"}).status_code == 401
    assert "suppressed" not in client.world.rows.rows[0]

    accepted = client.http.post("/webhooks/resend", content=body, headers=_sign(body, at=int(time.time())))
    assert accepted.status_code == 200 and accepted.json() == {"received": True}
    assert client.world.rows.rows[0]["suppressed"] == "complained"
    assert client.world.suppressions.rows[0]["_id"] == recipient_hash(ANA)

    monkeypatch.delenv("RESEND_WEBHOOK_SECRET")
    assert client.http.post("/webhooks/resend", content=body, headers=_sign(body, at=int(time.time()))).status_code == 401
