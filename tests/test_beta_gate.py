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
from app.routers.beta import WAITLIST_ACK, join_waitlist, waitlist_document

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _clear(monkeypatch):
    for name in ("BETA_ACTIVE", "BETA_INVITE_REQUIRED", "BETA_UPGRADES_OPEN", "BETA_INVITE_CODES"):
        monkeypatch.delenv(name, raising=False)


# ── 1. Unset means off ───────────────────────────────────────────────────────


def test_unset_env_is_no_beta(monkeypatch):
    _clear(monkeypatch)
    assert beta_config() == {"active": False, "invite_required": False, "upgrades_open": True}
    assert gate_new_account("anything") is None
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


def test_gate_refuses_a_new_account_without_a_code(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BETA_INVITE_REQUIRED", "true")
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0-7f3a")
    for header in (None, "", "wrong"):
        with pytest.raises(HTTPException) as raised:
            gate_new_account(header)
        assert raised.value.status_code == 403
        assert raised.value.detail == {"code": BETA_INVITE_REQUIRED_CODE}


def test_gate_stamps_the_cohort_on_a_right_code(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BETA_INVITE_REQUIRED", "true")
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0-7f3a,students:c1-9b2e")
    assert gate_new_account("C1-9B2E", now=NOW) == {"cohort": "students", "invite_code": "c1-9b2e", "joined_at": NOW}


def test_gate_is_off_when_invites_are_not_required_even_with_codes_set(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BETA_INVITE_CODES", "friends:c0-7f3a")
    assert gate_new_account(None) is None


def test_provisioning_reads_the_gate_before_writing(monkeypatch):
    """The dependency calls the gate with the request header, before insert_one."""
    import inspect
    from app.auth import firebase_auth

    source = inspect.getsource(firebase_auth.get_firebase_user)
    gate_at = source.index("gate_new_account(request.headers.get(INVITE_HEADER))")
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
    assert args[1]["$setOnInsert"] == {"created_at": NOW}
    assert kwargs["upsert"] is True


@pytest.mark.parametrize("field, value", [("email", "not-an-address"), ("locale", "x"), ("source", "/" * 201)])
def test_waitlist_rejects_out_of_bounds_fields(field, value):
    from pydantic import ValidationError

    payload = {"email": "ana@example.com", "locale": "es", "source": "/"}
    payload[field] = value
    with pytest.raises(ValidationError):
        WaitlistCreate(**payload)


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
