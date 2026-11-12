"""
GTM-001 — the beta gate on the server (ADR-038, docs/prd-road-to-market.md FR-001..FR-005).

Pins:

  1. Unset env means no beta: no mark, no gate, upgrades open.
  2. `cohort:code` parsing, case-insensitive codes, a bare code for an unnamed cohort.
  3. `gate_new_account`: off → None; on with no or a wrong code → 403 with the
     stable code; on with a right code → the stamp with its cohort.
  4. The waitlist upserts by lowercased email.
  5. Checkout refuses with `upgrades_closed` while the flag is off, before
     touching the price list or Mongo.

Env is set through `monkeypatch` so no test leaks a flag into another.
"""
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import pytest
from fastapi import HTTPException

from app.config.beta import (
    BETA_INVITE_REQUIRED_CODE,
    UPGRADES_CLOSED_CODE,
    beta_config,
    gate_new_account,
    invite_cohort,
    parse_invite_codes,
    require_upgrades_open,
)
from app.models.Beta import WaitlistCreate
from app.routers.beta import WAITLIST_ACK, join_waitlist, upsert_waitlist, waitlist_document

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _clear(monkeypatch):
    for name in ("BETA_ACTIVE", "BETA_INVITE_REQUIRED", "BETA_UPGRADES_OPEN", "BETA_INVITE_CODES", "BETA_CODE_MAX_USES", "BETA_CODE_EXPIRES"):
        monkeypatch.delenv(name, raising=False)


# ── 1. Unset means off ───────────────────────────────────────────────────────


async def _no_count(code: str, ceiling: int) -> int:
    raise AssertionError("a code without a ceiling is never counted")


@pytest.mark.asyncio
async def test_unset_env_is_no_beta(monkeypatch):
    _clear(monkeypatch)
    assert beta_config() == {"active": False, "invite_required": False, "upgrades_open": True}
    assert await gate_new_account("anything", _no_count) is None
    require_upgrades_open()  # does not raise


@pytest.mark.parametrize("raw, expected", [("true", True), ("1", True), ("YES", True), ("on", True), ("false", False), ("0", False), ("", False)])
def test_flags_read_the_usual_spellings(monkeypatch, raw, expected):
    _clear(monkeypatch)
    monkeypatch.setenv("BETA_ACTIVE", raw)
    assert beta_config()["active"] is expected


# ── 2. Codes name cohorts ────────────────────────────────────────────────────


def test_codes_parse_to_cohorts():
    assert parse_invite_codes("friends:c0-7F3A, students:c1-9b2e ,,letmein") == {
        "c0-7f3a": "friends",
        "c1-9b2e": "students",
        "letmein": "",
    }
    assert parse_invite_codes(None) == {}
    assert parse_invite_codes("  ") == {}


def test_invite_cohort_is_case_insensitive_and_strict(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:C0-7f3a")
    assert invite_cohort("c0-7F3A") == "friends"
    assert invite_cohort(" c0-7f3a ") == "friends"
    assert invite_cohort("c0-7f3b") is None
    assert invite_cohort("") is None
    assert invite_cohort(None) is None


# ── 3. The gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_refuses_a_new_account_without_a_code(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BETA_INVITE_REQUIRED", "true")
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0-7f3a")
    for header in (None, "", "wrong"):
        with pytest.raises(HTTPException) as raised:
            await gate_new_account(header, _no_count)
        assert raised.value.status_code == 403
        assert raised.value.detail == {"code": BETA_INVITE_REQUIRED_CODE}


@pytest.mark.asyncio
async def test_gate_stamps_the_cohort_on_a_right_code(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BETA_INVITE_REQUIRED", "true")
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0-7f3a,students:c1-9b2e")
    assert await gate_new_account("C1-9B2E", _no_count, now=NOW) == {"cohort": "students", "invite_code": "c1-9b2e", "joined_at": NOW}


@pytest.mark.asyncio
async def test_gate_is_off_when_invites_are_not_required_even_with_codes_set(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0-7f3a")
    assert await gate_new_account(None, _no_count) is None


def test_provisioning_reads_the_gate_before_writing(monkeypatch):
    """The dependency calls the gate with the request header, before insert_one."""
    import inspect
    from app.auth import firebase_auth

    source = inspect.getsource(firebase_auth.get_firebase_user)
    gate_at = source.index("await gate_account_creation(request.headers.get(INVITE_HEADER), token_data)")
    insert_at = source.index("users_collection.insert_one(new_user_doc)")
    assert gate_at < insert_at
    assert '"beta": beta_stamp' in source


# ── 4. The waitlist ──────────────────────────────────────────────────────────


def test_waitlist_document_lowercases_and_stamps():
    doc = waitlist_document(WaitlistCreate(email="Ana@Example.com", locale="es", source="/"), now=NOW)
    assert doc == {"email": "ana@example.com", "locale": "es", "source": "/", "status": "waiting", "updated_at": NOW}


@pytest.mark.asyncio
async def test_waitlist_upserts_by_email():
    collection = MagicMock()
    collection.find_one_and_update = AsyncMock(return_value={"email": "ana@example.com"})

    response = await join_waitlist(WaitlistCreate(email="Ana@Example.com", locale="es"), collection, now=NOW)

    assert response.message == WAITLIST_ACK
    args, kwargs = collection.find_one_and_update.call_args
    assert args[0] == {"email": "ana@example.com"}
    assert args[1]["$set"]["email"] == "ana@example.com"
    # ADR-080: a new row is given its leave token with its first time.
    assert args[1]["$setOnInsert"]["created_at"] == NOW and len(args[1]["$setOnInsert"]["leave_token"]) >= 40
    assert kwargs["upsert"] is True


@pytest.mark.parametrize("field, value", [("email", "not-an-address"), ("locale", "x"), ("source", "/" * 201)])
def test_waitlist_rejects_out_of_bounds_fields(field, value):
    from pydantic import ValidationError

    payload = {"email": "ana@example.com", "locale": "es", "source": "/"}
    payload[field] = value
    with pytest.raises(ValidationError):
        WaitlistCreate(**payload)


# ── 4b. The operator hears about a new email once (WAIT-001, FR-013) ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize("before, expected", [(None, True), ({"email": "ana@example.com"}, False)])
async def test_upsert_says_whether_the_email_is_new(before, expected):
    collection = MagicMock()
    collection.find_one_and_update = AsyncMock(return_value=before)

    doc, is_new = await upsert_waitlist(WaitlistCreate(email="Ana@Example.com", locale="es", source="/"), collection, now=NOW)

    assert is_new is expected
    assert doc["email"] == "ana@example.com" and doc["updated_at"] == NOW
    # The row as it was before the write is what tells a first time from a repeat.
    from pymongo import ReturnDocument

    assert collection.find_one_and_update.call_args.kwargs["return_document"] == ReturnDocument.BEFORE


def _waitlist_client(monkeypatch, before):
    """A bare app carrying only the beta router, a fake collection and a recorded `notify`."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware

    import app.routers.beta as beta_module
    from app.core.limiter import limiter

    collection = MagicMock()
    collection.find_one_and_update = AsyncMock(side_effect=before)
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(beta_module, "waitlist_collection", collection)
    monkeypatch.setattr(beta_module, "notify", notify)
    # WAIT-004's confirmation has its own tests; here it would read the scripted collection.
    monkeypatch.setattr(beta_module.waitlist, "send_confirmation", AsyncMock(return_value="skipped"))
    limiter.reset()
    api = FastAPI()
    api.state.limiter = limiter
    api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    api.add_middleware(SlowAPIMiddleware)
    api.include_router(beta_module.router)
    return TestClient(api), notify


WAITLIST_FORM = {"email": "Ana@Example.com", "locale": "es", "source": "/"}


def test_a_new_email_notifies_once_and_a_repeat_does_not(monkeypatch):
    client, notify = _waitlist_client(monkeypatch, before=[None, {"email": "ana@example.com"}, {"email": "ana@example.com"}])

    first = client.post("/beta/waitlist", json=WAITLIST_FORM)
    repeats = [client.post("/beta/waitlist", json=WAITLIST_FORM) for _ in range(2)]

    assert notify.await_count == 1
    subject, body = notify.await_args.args
    assert subject == "[Nowry] Waitlist: ana@example.com"
    for line in ("Email: ana@example.com", "Locale: es", "Source: /", "Joined: 20"):
        assert line in body
    # No enumeration: the visitor cannot tell a first time from a repeat.
    for repeat in repeats:
        assert (repeat.status_code, repeat.json(), repeat.headers["content-type"]) == (first.status_code, first.json(), first.headers["content-type"])
    assert first.status_code == 201 and first.json() == {"message": WAITLIST_ACK}


def test_a_notification_that_fails_is_logged_and_the_sign_up_still_succeeds(monkeypatch, caplog):
    """The real `notify`, with Resend unreachable: 201, the row written, a warning in the log."""
    import logging

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.routers.beta as beta_module
    from app.core.limiter import limiter
    from app.services import notifications

    for name, value in {"RESEND_API_KEY": "re_test", "NOTIFY_TO_EMAIL": "ops@example.com", "NOTIFY_FROM_EMAIL": "Nowry <notify@nowry.app>"}.items():
        monkeypatch.setenv(name, value)
    collection = MagicMock()
    collection.find_one_and_update = AsyncMock(return_value=None)
    monkeypatch.setattr(beta_module, "waitlist_collection", collection)
    monkeypatch.setattr(beta_module.waitlist, "send_confirmation", AsyncMock(return_value="skipped"))  # WAIT-004 has its own tests
    monkeypatch.setattr(notifications.httpx, "AsyncClient", MagicMock(side_effect=RuntimeError("resend is down")))
    limiter.reset()
    api = FastAPI()
    api.state.limiter = limiter
    api.include_router(beta_module.router)

    with caplog.at_level(logging.WARNING):
        response = TestClient(api).post("/beta/waitlist", json=WAITLIST_FORM)

    assert response.status_code == 201 and response.json() == {"message": WAITLIST_ACK}
    collection.find_one_and_update.assert_awaited_once()
    assert notifications.httpx.AsyncClient.called
    assert any("notify: could not send" in record.getMessage() for record in caplog.records)


def test_the_waitlist_notice_carries_what_the_operator_needs():
    from app.services.notifications import waitlist_notification

    subject, body = waitlist_notification({"email": "ana@example.com", "locale": "es", "source": "/register", "updated_at": NOW})
    assert subject == "[Nowry] Waitlist: ana@example.com"
    assert body == "Email: ana@example.com\nLocale: es\nSource: /register\nJoined: 2026-10-02T12:00:00+00:00"


# ── 5. Closed upgrades ───────────────────────────────────────────────────────


def test_checkout_is_refused_while_upgrades_are_closed(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BETA_UPGRADES_OPEN", "false")
    with pytest.raises(HTTPException) as raised:
        require_upgrades_open()
    assert raised.value.status_code == 403
    assert raised.value.detail == {"code": UPGRADES_CLOSED_CODE}


def test_checkout_calls_the_refusal_before_the_price_whitelist():
    import inspect
    from app.routers import subscriptions

    source = inspect.getsource(subscriptions.create_checkout_session)
    assert source.index("require_upgrades_open()") < source.index("VALID_PRICE_IDS")
