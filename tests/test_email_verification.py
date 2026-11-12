"""ADR-076, FR-018: an email is someone's word until Firebase has verified it.

The rules are pure (``app/auth/email_verification.py``); the dependency tests
run the real ``get_firebase_user`` over the in-memory database, with Firebase's
answer for the token supplied.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

pytest.importorskip("mongomock_motor")

from app.auth.email_verification import session_is_cut, verification_update  # noqa: E402
from tests._fake_world import fake_world  # noqa: E402

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


@pytest.fixture
def world():
    with fake_world() as w:
        yield w


# ── the rules ───────────────────────────────────────────────────────────────

def test_an_unverified_token_marks_an_unmarked_account_and_changes_nothing_else():
    assert verification_update({}, {"email_verified": False}) == {"email_verified": False}
    assert verification_update({"email_verified": False}, {"email_verified": False}) == {}
    assert verification_update({"email_verified": True}, {"email_verified": False}) == {}, "never back to false"


def test_the_owners_own_click_verifies_and_cuts_nothing():
    token = {"email_verified": True, "sign_in_provider": "password", "auth_time": 1000}
    assert verification_update({"email_verified": False}, token, NOW) == {"email_verified": True, "email_verified_at": NOW}


def test_another_sign_in_verifying_an_unverified_account_cuts_older_sessions():
    token = {"email_verified": True, "sign_in_provider": "google.com", "auth_time": 2000}
    update = verification_update({"email_verified": False}, token, NOW)
    assert update == {"email_verified": True, "email_verified_at": NOW, "sessions_valid_after": 2000}


def test_an_account_with_no_mark_is_verified_without_a_cut():
    """An account from before the record: there is no telling how it was made."""
    token = {"email_verified": True, "sign_in_provider": "google.com", "auth_time": 2000}
    assert "sessions_valid_after" not in verification_update({}, token, NOW)
    assert verification_update({"email_verified": True}, token, NOW) == {}


@pytest.mark.parametrize("valid_after, auth_time, cut", [
    (None, 5, False), (0, 5, False), (2000, 1999, True), (2000, 2000, False), (2000, 2001, False), (2000, None, True), (2000, "x", True),
])
def test_a_session_is_cut_only_when_it_is_older_than_the_mark(valid_after, auth_time, cut):
    assert session_is_cut(valid_after, {"auth_time": auth_time}) is cut


# ── the dependency ──────────────────────────────────────────────────────────

def _resolve(world, decoded, token=b"t"):
    import app.auth.firebase_auth as firebase_auth

    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer " + token)], "query_string": b""})
    with patch.object(firebase_auth, "verify_firebase_token", return_value=decoded), \
         patch.object(firebase_auth, "_get_cached_token", return_value=None), \
         patch.object(firebase_auth, "_cache_token"):
        return world.run(firebase_auth.get_firebase_user(request))


def _doc(world, uid):
    return world.run(world.raw["users"].find_one({"_id": uid}))


def test_the_account_document_follows_firebases_flag(world):
    uid = world.new_user("ana")
    fb = _doc(world, uid)["firebase_uid"]
    _resolve(world, {"uid": fb, "email": "ana@example.com", "email_verified": False, "auth_time": 100, "firebase": {"sign_in_provider": "password"}})
    assert _doc(world, uid)["email_verified"] is False
    _resolve(world, {"uid": fb, "email": "ana@example.com", "email_verified": True, "auth_time": 100, "firebase": {"sign_in_provider": "password"}})
    doc = _doc(world, uid)
    assert doc["email_verified"] is True and doc["email_verified_at"] and "sessions_valid_after" not in doc


def test_the_owners_google_sign_in_ends_the_session_of_whoever_registered_first(world):
    """Someone registers the owner's address first; the owner's Google sign-in lands in that account."""
    import app.auth.firebase_auth as firebase_auth

    uid = world.new_user("victim", email_verified=False)
    fb = _doc(world, uid)["firebase_uid"]
    first = {"uid": fb, "email": "victim@example.com", "email_verified": False, "auth_time": 1000, "firebase": {"sign_in_provider": "password"}}
    owner = {"uid": fb, "email": "victim@example.com", "email_verified": True, "auth_time": 5000, "firebase": {"sign_in_provider": "google.com"}}

    assert _resolve(world, first)["user_id"] == str(uid)
    assert _resolve(world, owner)["user_id"] == str(uid), "the sign-in that verifies is not cut by its own mark"
    assert _doc(world, uid)["sessions_valid_after"] == 5000

    refreshed = dict(first, email_verified=True)  # a refreshed token keeps its original auth_time
    for stale in (first, refreshed):
        with pytest.raises(HTTPException) as refused:
            _resolve(world, stale)
        assert refused.value.status_code == 401
    assert _resolve(world, owner)["user_id"] == str(uid)

    # A token cached before the cut is refused in the same process.
    cached = {"firebase_uid": fb, "user_id": str(uid), "auth_time": 1000}
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer old")], "query_string": b""})
    with patch.object(firebase_auth, "_get_cached_token", return_value=cached):
        with pytest.raises(HTTPException) as refused:
            world.run(firebase_auth.get_firebase_user(request))
    assert refused.value.status_code == 401
    firebase_auth._sessions_cut.pop(fb, None)


def test_a_new_account_is_stored_with_firebases_flag_and_how_it_was_made(world):
    decoded = {"uid": "fb-new-pw", "email": "nuevo@example.com", "email_verified": False, "auth_time": 1, "firebase": {"sign_in_provider": "password"}}
    resolved = _resolve(world, decoded)
    doc = world.run(world.raw["users"].find_one({"firebase_uid": "fb-new-pw"}))
    assert str(doc["_id"]) == resolved["user_id"]
    assert doc["email_verified"] is False and doc["signup_provider"] == "password"


def test_a_board_invitation_reaches_only_a_verified_account(world):
    owner = world.new_user("owner", email_verified=True)
    world.new_user("proved", email_verified=True)
    world.new_user("claimed", email_verified=False)
    world.new_user("unmarked")
    board = world.run(world.raw["blackboards"].insert_one({"owner_user_id": str(owner), "title": "b", "collaborators": []})).inserted_id
    world.act_as(owner)

    def invite(email):
        return world.http.put(f"/blackboards/{board}/invite", json={"invitee_email": email})

    assert invite("proved@example.com").status_code == 200
    for unproven in ("claimed@example.com", "unmarked@example.com", "nobody@example.com"):
        answer = invite(unproven)
        assert answer.status_code == 404 and answer.json()["detail"] == "user_not_found"
