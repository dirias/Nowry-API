"""SIGNIN-001 (FR-030, ADR-083): a verified email re-links an account whose Firebase id is gone.

Runs the real dependency over the in-memory database (``tests/_fake_world``).
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

pytest.importorskip("mongomock_motor")

import app.auth.firebase_auth as firebase_auth  # noqa: E402
from tests._fake_world import fake_world  # noqa: E402


@pytest.fixture
def world():
    with fake_world() as w:
        yield w


def _resolve(world, decoded: dict, identity_exists=False):
    """Run get_firebase_user for this token; `identity_exists` is Firebase's answer about the stored id."""
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer t")], "query_string": b""})
    with patch.object(firebase_auth, "verify_firebase_token", return_value=decoded), \
         patch.object(firebase_auth, "_get_cached_token", return_value=None), \
         patch.object(firebase_auth, "_cache_token"), \
         patch.object(firebase_auth, "_firebase_identity_exists", return_value=identity_exists), \
         patch.object(firebase_auth, "_alert_conflict", AsyncMock()) as alert:
        try:
            return world.run(firebase_auth.get_firebase_user(request)), alert
        except HTTPException as refused:
            return refused, alert


def _google(uid: str, email: str) -> dict:
    return {"uid": uid, "email": email, "email_verified": True, "firebase": {"sign_in_provider": "google.com"}}


def _stored_uid(world, user_id):
    return world.run(world.raw["users"].find_one({"_id": user_id}))["firebase_uid"]


def test_a_gone_identity_is_re_linked_and_the_account_answers_users_me(world):
    old = world.new_user("didier", firebase_uid="fb-stale", email="didier@example.com")
    resolved, alert = _resolve(world, _google("fb-new", "didier@example.com"), identity_exists=False)
    assert resolved["user_id"] == str(old)
    assert _stored_uid(world, old) == "fb-new"
    alert.assert_not_awaited()

    world.current["user"] = resolved
    me = world.http.get("/users/me")
    assert me.status_code == 200 and me.json()["id"] == str(old)


def test_the_email_matches_whatever_its_case(world):
    old = world.new_user("didier", firebase_uid="fb-stale", email="Didier.Irias@Example.com")
    resolved, _ = _resolve(world, _google("fb-new", "didier.irias@example.com"))
    assert resolved["user_id"] == str(old) and _stored_uid(world, old) == "fb-new"


def test_a_live_identity_is_never_merged_and_the_operator_is_alerted(world):
    old = world.new_user("didier", firebase_uid="fb-alive", email="didier@example.com")
    refused, alert = _resolve(world, _google("fb-other", "didier@example.com"), identity_exists=True)
    assert isinstance(refused, HTTPException) and refused.status_code == 409
    assert refused.detail == {"code": "account_conflict"}
    assert _stored_uid(world, old) == "fb-alive"
    alert.assert_awaited_once()


def test_firebase_unreachable_writes_nothing_and_still_serves_the_account(world):
    old = world.new_user("didier", firebase_uid="fb-stale", email="didier@example.com")
    resolved, _ = _resolve(world, _google("fb-new", "didier@example.com"), identity_exists=None)
    assert resolved["user_id"] == str(old) and _stored_uid(world, old) == "fb-stale"


def test_an_unverified_email_is_asked_to_confirm_and_takes_nothing(world):
    """ADR-083 case 2: before the invite gate, so the answer is "confirm", not "private beta"."""
    old = world.new_user("didier", firebase_uid="fb-stale", email="didier@example.com")
    decoded = {"uid": "fb-pw", "email": "didier@example.com", "email_verified": False}
    refused, _ = _resolve(world, decoded)
    assert isinstance(refused, HTTPException) and refused.status_code == 409
    assert refused.detail == {"code": "email_unverified"}
    assert _stored_uid(world, old) == "fb-stale"
    # Once confirmed, the same identity re-links.
    resolved, _ = _resolve(world, {**decoded, "email_verified": True})
    assert resolved["user_id"] == str(old) and _stored_uid(world, old) == "fb-pw"


def test_a_deleted_record_is_never_re_linked(world):
    old = world.new_user("gone", firebase_uid="fb-stale", email="gone@example.com")
    world.run(world.raw["users"].update_one({"_id": old}, {"$set": {"deleted_at": "2026-11-01"}}))
    with patch.object(firebase_auth, "gate_account_creation", AsyncMock(side_effect=HTTPException(403, {"code": "beta_invite_required"}))):
        refused, _ = _resolve(world, _google("fb-new", "gone@example.com"))
    assert isinstance(refused, HTTPException) and refused.status_code == 403
    assert _stored_uid(world, old) == "fb-stale"


def test_login_reads_the_resolved_account_not_the_uid_again(world):
    old = world.new_user("didier", firebase_uid="fb-stale", email="didier@example.com")
    world.current["user"] = {"firebase_uid": "fb-new", "email": "didier@example.com", "user_id": str(old)}
    ok = world.http.post("/auth/login", json={"firebase_uid": "fb-new", "email": "didier@example.com"})
    assert ok.status_code == 200 and ok.json()["user_id"] == str(old)
