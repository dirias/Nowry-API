"""
INVITE-008 — invitations in batches, emailed (ADR-077 amendment, docs/prd-admin.md FR-012).

Pins:

  1. Entries are trimmed, lowercased and collapsed; only a bare address is an address.
  2. Each address is sorted: invalid, left_list (left the waitlist, or suppressed),
     has_account, open_invite (left as it is), will_invite.
  3. A dry run writes nothing; a send creates, emails and logs each invitation, records
     `sent_at`/`send_count`/`last_error`, and writes in the waitlist row's language.
  4. A failed letter leaves the invitation open with its error.
  5. A send over today's invitation share is refused whole (409), nothing created.
  6. The answer never carries a link or a token.
  7. `beta.invitation` renders in five languages, with the button, the date, the Android
     line and the leave link; it is a personal letter of the invitation class.
  8. The routes: a dry run needs only require_admin; a send needs the recent sign-in;
     `/cohorts` lists cohorts in use; the list answers totals per state.
"""
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import invites as rules
from app.emails.render import KINDS, LOCALES, render_email
from app.models.Beta import InviteBatchRequest
from app.services import admin_actions, beta_invites, email as email_service, invitation_email, invite_batch
from tests._fake_collection import FakeCollection

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
ADMIN = "65f0a1b2c3d4e5f6a7b8c9d0"


# ── 1. Entries ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, ok", [
    ("ana@example.com", True), ("a.b+tag@sub.example.co", True),
    ("not-an-address", False), ("ana@", False), ("Ana <ana@example.com>", False),
    ("ana @example.com", False), ("ana@example.com,bo@example.com", False), ("", False),
])
def test_only_a_bare_address_is_an_address(text, ok):
    assert invite_batch.is_address(text) is ok


def test_entries_are_trimmed_lowercased_and_collapsed_in_order():
    pairs = invite_batch.entries([" Ana@Example.com ", "bo@example.com", "ana@example.com", "nonsense", "NONSENSE "])
    assert pairs == [(" Ana@Example.com ", "ana@example.com"), ("bo@example.com", "bo@example.com"), ("nonsense", None)]


# ── 2. Sorting ───────────────────────────────────────────────────────────────


def test_each_address_gets_one_outcome_in_order_of_precedence():
    facts = invite_batch.Facts(left={"l@x.com", "both@x.com"}, accounts={"a@x.com", "both@x.com"}, open_invites={"o@x.com": "id1", "a@x.com": "id2"})
    assert invite_batch.classify(None, facts) == "invalid"
    assert invite_batch.classify("both@x.com", facts) == "left_list"
    assert invite_batch.classify("a@x.com", facts) == "has_account"
    assert invite_batch.classify("o@x.com", facts) == "open_invite"
    assert invite_batch.classify("new@x.com", facts) == "will_invite"


@pytest.fixture
def world(monkeypatch):
    """Fake invites, waitlist, accounts and admin log; email answered 'sent'; a day with 100 left."""
    stores = {name: FakeCollection() for name in ("invites", "waitlist", "users", "log")}
    monkeypatch.setattr(beta_invites, "invites_collection", stores["invites"])
    monkeypatch.setattr(invite_batch, "invites_collection", stores["invites"])
    monkeypatch.setattr(invite_batch, "waitlist_collection", stores["waitlist"])
    monkeypatch.setattr(invite_batch, "users_collection", stores["users"])
    monkeypatch.setattr(invitation_email, "invites_collection", stores["invites"])
    monkeypatch.setattr(invitation_email, "waitlist_collection", stores["waitlist"])
    monkeypatch.setattr(admin_actions, "admin_actions_collection", stores["log"])
    monkeypatch.setattr(invite_batch.email_suppressions, "is_suppressed", AsyncMock(side_effect=lambda a: a == "bounced@x.com"))
    monkeypatch.setattr(invite_batch.email_ledger, "sent_today", AsyncMock(return_value=0))
    monkeypatch.setenv("EMAIL_DAILY_BUDGET", "125")  # invitation share: 100
    monkeypatch.setenv("API_PUBLIC_URL", "https://api.nowry.app")
    monkeypatch.setenv("FRONTEND_URL", "https://nowry.app")
    send = AsyncMock(return_value="sent")
    monkeypatch.setattr(email_service, "send_email", send)
    stores["send"] = send
    return stores


async def seed(world):
    await world["waitlist"].insert_one({"email": "left@x.com", "status": "left", "leave_token": "t" * 30})
    await world["waitlist"].insert_one({"email": "wait@x.com", "status": "waiting", "locale": "es-MX", "leave_token": "w" * 30})
    await world["users"].insert_one({"email": "member@x.com", "deleted_at": None})
    await world["users"].insert_one({"email": "gone@x.com", "deleted_at": NOW})
    await beta_invites.create_invite("open@x.com", "friends", ADMIN, now=NOW - timedelta(days=1))
    await beta_invites.create_invite("stale@x.com", "friends", ADMIN, days=1, now=NOW - timedelta(days=3))


BATCH = ["wait@x.com", "new@x.com", "left@x.com", "bounced@x.com", "member@x.com", "gone@x.com", "open@x.com", "stale@x.com", "nope", "NEW@x.com"]


@pytest.mark.asyncio
async def test_a_dry_run_sorts_every_line_and_writes_nothing(world):
    await seed(world)
    answer = await invite_batch.run_batch(BATCH, "c1-waitlist", 14, "en", True, ADMIN, NOW)
    outcomes = {result["input"]: result["outcome"] for result in answer["results"]}
    assert outcomes == {
        "wait@x.com": "will_invite", "new@x.com": "will_invite", "left@x.com": "left_list", "bounced@x.com": "left_list",
        "member@x.com": "has_account", "gone@x.com": "will_invite", "open@x.com": "open_invite",
        "stale@x.com": "will_invite", "nope": "invalid",
    }
    assert answer["counts"] == {"will_invite": 4, "left_list": 2, "has_account": 1, "open_invite": 1, "invalid": 1}
    assert answer["dry_run"] is True and answer["invitations_left_today"] == 100
    open_row = next(r for r in answer["results"] if r["outcome"] == "open_invite")
    assert open_row["invite_id"] == str(next(r["_id"] for r in world["invites"].rows if r["email"] == "open@x.com"))
    assert len(world["invites"].rows) == 2 and world["log"].rows == [] and world["send"].await_count == 0


@pytest.mark.asyncio
async def test_a_send_invites_emails_and_logs_each_address_in_its_language(world):
    await seed(world)
    answer = await invite_batch.run_batch(BATCH, "c1-waitlist", 30, "fr", False, ADMIN, NOW)
    sent = [r for r in answer["results"] if r["outcome"] == "sent"]
    assert {r["email"] for r in sent} == {"wait@x.com", "new@x.com", "gone@x.com", "stale@x.com"}
    assert answer["counts"]["sent"] == 4 and "will_invite" not in answer["counts"]
    rows = {r["email"]: r for r in world["invites"].rows if r["cohort"] == "c1-waitlist"}
    assert rows["wait@x.com"]["locale"] == "es" and rows["new@x.com"]["locale"] == "fr"
    for row in rows.values():
        assert row["sent_at"] == NOW and row["send_count"] == 1 and row["last_error"] is None
        assert row["expires_at"] == NOW + timedelta(days=30) and row["lifetime_days"] == 30
    assert {r["invite_id"] for r in sent} == {str(r["_id"]) for r in rows.values()}
    assert [r["action"] for r in world["log"].rows] == ["invite.send"] * 4
    kinds = {call.args[0] for call in world["send"].await_args_list}
    assert kinds == {"beta.invitation"}


@pytest.mark.asyncio
async def test_the_letter_carries_the_link_the_date_and_the_leave_link(world):
    answer = await invite_batch.run_batch(["new@x.com"], "c1-waitlist", 14, "es", False, ADMIN, NOW)
    kind, to, locale, values = world["send"].await_args.args
    headers = world["send"].await_args.kwargs["headers"]
    assert (kind, to, locale) == ("beta.invitation", "new@x.com", "es")
    assert values["link"].startswith("https://nowry.app/register?invite=nwi_")
    assert world["invites"].rows[0]["token_hash"] == rules.hash_token(values["link"].split("invite=")[1])
    assert values["expires"] == "28 de noviembre"
    waitlist_row = world["waitlist"].rows[0]
    assert waitlist_row["status"] == "invited" and waitlist_row["source"] == "invitation" and waitlist_row["locale"] == "es"
    assert values["leave_link"] == f"https://nowry.app/waitlist/leave/{waitlist_row['leave_token']}"
    assert headers["List-Unsubscribe"] == f"<https://api.nowry.app/beta/waitlist/leave/{waitlist_row['leave_token']}>"
    assert "nwi_" not in repr(answer)


@pytest.mark.asyncio
async def test_a_waiting_row_becomes_invited_only_once_its_letter_left(world):
    await seed(world)
    world["send"].return_value = "failed"
    await invite_batch.run_batch(["wait@x.com"], "c1-waitlist", 14, "en", False, ADMIN, NOW)
    assert next(r for r in world["waitlist"].rows if r["email"] == "wait@x.com")["status"] == "waiting"
    world["send"].return_value = "outbox"  # the dev suite's outbox counts as delivered
    await invite_batch.run_batch(["other@x.com"], "c1-waitlist", 14, "en", False, ADMIN, NOW)
    assert next(r for r in world["invites"].rows if r["email"] == "other@x.com")["sent_at"] == NOW


@pytest.mark.asyncio
async def test_a_failed_letter_leaves_the_invitation_open_with_its_error(world):
    world["send"].return_value = "over_budget"
    answer = await invite_batch.run_batch(["new@x.com"], "c1-waitlist", 14, "en", False, ADMIN, NOW)
    assert answer["results"][0]["outcome"] == "send_failed" and answer["results"][0]["invite_id"]
    row = world["invites"].rows[0]
    assert rules.invite_state(row, NOW) == "open"
    assert row["last_error"] == "over_budget" and row["sent_at"] is None and row["send_count"] == 0
    assert world["log"].rows[0]["details"]["outcome"] == "over_budget"


@pytest.mark.asyncio
async def test_a_letter_that_raises_is_a_failure_not_a_500(world, monkeypatch):
    monkeypatch.setattr(email_service, "send_email", AsyncMock(side_effect=RuntimeError("boom")))
    answer = await invite_batch.run_batch(["new@x.com"], "c1-waitlist", 14, "en", False, ADMIN, NOW)
    assert answer["results"][0]["outcome"] == "send_failed" and world["invites"].rows[0]["last_error"] == "failed"


@pytest.mark.asyncio
async def test_without_a_public_api_address_no_letter_is_sent(world, monkeypatch):
    monkeypatch.delenv("API_PUBLIC_URL")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    answer = await invite_batch.run_batch(["new@x.com"], "c1-waitlist", 14, "en", False, ADMIN, NOW)
    assert answer["results"][0]["outcome"] == "send_failed" and world["send"].await_count == 0


@pytest.mark.asyncio
async def test_a_batch_over_todays_share_is_refused_whole(world, monkeypatch):
    monkeypatch.setattr(invite_batch.email_ledger, "sent_today", AsyncMock(return_value=99))
    with pytest.raises(HTTPException) as refused:
        await invite_batch.run_batch(["a@x.com", "b@x.com", "member@x.com"], "c1-waitlist", 14, "en", False, ADMIN, NOW)
    assert refused.value.status_code == 409
    assert refused.value.detail == {"code": "over_budget", "needed": 3, "left": 1}
    assert world["invites"].rows == [] and world["send"].await_count == 0 and world["log"].rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("budget, sent, left", [("125", 0, 100), ("10", 3, 5), ("10", 9, 0), (None, 0, 72)])
async def test_invitations_left_today(monkeypatch, budget, sent, left):
    if budget is None:
        monkeypatch.delenv("EMAIL_DAILY_BUDGET", raising=False)
    else:
        monkeypatch.setenv("EMAIL_DAILY_BUDGET", budget)
    monkeypatch.setattr(invite_batch.email_ledger, "sent_today", AsyncMock(return_value=sent))
    assert await invite_batch.invitations_left_today() == left


# ── 7. The email ─────────────────────────────────────────────────────────────


def test_the_invitation_is_a_personal_letter_of_the_invitation_class():
    assert KINDS["beta.invitation"] == {"class": "invitation", "sender": "personal"}


@pytest.mark.parametrize("locale", LOCALES)
def test_the_invitation_renders_in_every_language(locale):
    values = {"email": "ana@example.com", "support": "hello@nowry.app", "link": "https://nowry.app/register?invite=nwi_x",
              "expires": "28 November", "leave_link": "https://nowry.app/waitlist/leave/tok"}
    rendered = render_email("beta.invitation", locale, values, logo_url="https://nowry.app/logo192.png")
    for part in (rendered.text, rendered.html):
        assert "https://nowry.app/register?invite=nwi_x" in part and "28 November" in part
        assert "ana@example.com" in part and "https://nowry.app/waitlist/leave/tok" in part
        assert "Android" in part and "Didier, Nowry" in part and "$" not in part


def test_the_english_letter_reads_as_written():
    values = {"email": "ana@example.com", "support": "hello@nowry.app", "link": "L", "expires": "28 November", "leave_link": "Q"}
    text = render_email("beta.invitation", "en", values, logo_url="x").text
    assert "This invitation is for ana@example.com only. It works once, until 28 November." in text
    assert "Testing the Android app? Create your account with this button first, then sign in on the app with the same address." in text


# ── 8. The request and the routes ────────────────────────────────────────────


def test_the_request_has_its_defaults():
    payload = InviteBatchRequest(addresses=["ana@example.com"], cohort="c1-waitlist")
    assert payload.dry_run is True and payload.expires_in_days == 14 and payload.locale == "en"


@pytest.mark.parametrize("field, value", [
    ("addresses", []), ("addresses", ["a@x.com"] * 51), ("addresses", ["x" * 321]), ("cohort", "Has Spaces"),
    ("expires_in_days", 0), ("expires_in_days", 91), ("locale", "pt"),
])
def test_the_request_refuses_out_of_bounds_fields(field, value):
    with pytest.raises(ValidationError):
        InviteBatchRequest(**{"addresses": ["ana@example.com"], "cohort": "c1-waitlist", field: value})


@pytest.mark.asyncio
async def test_a_dry_run_needs_no_recent_sign_in_and_a_send_does(world):
    from app.routers import beta_admin

    stale = {"user_id": ADMIN, "auth_time": int(time.time()) - 13 * 3600}
    answer = await beta_admin.batch_invites(InviteBatchRequest(addresses=["new@x.com"], cohort="c1-waitlist"), admin=stale)
    assert answer.results[0].outcome == "will_invite"
    with pytest.raises(HTTPException) as refused:
        await beta_admin.batch_invites(InviteBatchRequest(addresses=["new@x.com"], cohort="c1-waitlist", dry_run=False), admin=stale)
    assert refused.value.status_code == 401 and refused.value.detail == {"code": "reauth_required"}
    assert world["invites"].rows == []

    fresh = {"user_id": ADMIN, "auth_time": int(time.time())}
    answer = await beta_admin.batch_invites(InviteBatchRequest(addresses=["new@x.com"], cohort="c1-waitlist", dry_run=False), admin=fresh)
    assert answer.results[0].outcome == "sent" and "link" not in answer.model_dump_json()


@pytest.mark.asyncio
async def test_the_cohorts_in_use(world, monkeypatch):
    from app.routers import beta_admin

    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0-7f3a,dev:dev-test:50,:bare-code")
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setattr(beta_invites, "count_invites_by_cohort", AsyncMock(return_value={"spring-class": {"open": 1}}))
    answer = await beta_admin.list_cohorts(admin={"user_id": ADMIN})
    assert answer.cohorts == ["c1-waitlist", "dev", "friends", "spring-class"]
    assert (answer.default_cohort, answer.lifetimes, answer.default_lifetime) == ("c1-waitlist", [7, 14, 30, 60, 90], 14)
    assert answer.invitations_left_today == 100

    monkeypatch.setenv("ENV", "production")
    assert "dev" not in (await beta_admin.list_cohorts(admin={"user_id": ADMIN})).cohorts


@pytest.mark.asyncio
async def test_the_list_answers_totals_per_state(monkeypatch):
    from app.routers import beta_admin

    monkeypatch.setattr(beta_invites, "list_invites", AsyncMock(return_value=[]))
    monkeypatch.setattr(beta_invites, "count_invites_by_cohort", AsyncMock(return_value={
        "friends": {"open": 3, "used": 2, "revoked": 0, "expired": 1},
        "c1-waitlist": {"open": 1, "used": 0, "revoked": 4, "expired": 0},
    }))
    answer = await beta_admin.list_invites(state=None, cohort=None, limit=50, admin={"user_id": ADMIN})
    assert answer.totals == {"open": 4, "used": 2, "revoked": 4, "expired": 1}


def test_the_summary_shows_the_letter_and_never_the_token():
    from app.routers.beta_admin import summary_fields

    token = rules.make_token()
    row = {**rules.new_invite_document("ana@example.com", "friends", token, ADMIN, NOW, locale="ja"), "_id": ObjectId()}
    row.update(sent_at=NOW, send_count=2, last_error=None)
    fields = summary_fields(row, NOW)
    assert (fields["locale"], fields["sent_at"], fields["send_count"], fields["last_error"]) == ("ja", NOW, 2, None)
    assert token not in repr(fields) and row["token_hash"] not in repr(fields)


@pytest.mark.parametrize("path", ["/beta/admin/invites/batch", "/beta/admin/cohorts"])
def test_the_new_routes_are_admin_gated(path):
    from app.auth.dependencies import require_admin
    from app.routers.beta_admin import router

    def calls(dependant):
        found = {dependant.call}
        for sub in dependant.dependencies:
            found |= calls(sub)
        return found

    route = next(r for r in router.routes if r.path == path)
    assert require_admin in calls(route.dependant)
