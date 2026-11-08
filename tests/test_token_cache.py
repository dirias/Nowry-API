"""QA-021: the token cache and ``optional_auth``.

A cache that served one user's claims for another's token would be a
cross-user leak, and its lines (14–43 %) were the least covered in the auth
module. ``optional_auth`` is what the public browse routes use to tell a
visitor from a member: it must answer None for no token and for a bad one,
never 401, and the member for a good one.
"""
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.security import HTTPAuthorizationCredentials

import app.auth.firebase_auth as fa

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402


@pytest.fixture(autouse=True)
def empty_cache():
    saved = dict(fa._token_cache)
    fa._token_cache.clear()
    yield
    fa._token_cache.clear()
    fa._token_cache.update(saved)


# ── the cache ───────────────────────────────────────────────────────────────

def test_a_miss_is_none_and_a_hit_is_the_data_for_that_token_only():
    assert fa._get_cached_token("t1") is None
    fa._cache_token("t1", {"firebase_uid": "u1"})
    fa._cache_token("t2", {"firebase_uid": "u2"})
    assert fa._get_cached_token("t1") == {"firebase_uid": "u1"}
    assert fa._get_cached_token("t2") == {"firebase_uid": "u2"}
    assert fa._get_cached_token("t3") is None


def test_an_entry_expires_after_the_ttl_and_is_dropped():
    with patch("app.auth.firebase_auth.time.time", return_value=1_000.0):
        fa._cache_token("t1", {"firebase_uid": "u1"})
    with patch("app.auth.firebase_auth.time.time", return_value=1_000.0 + fa._CACHE_TTL - 1):
        assert fa._get_cached_token("t1") == {"firebase_uid": "u1"}
    with patch("app.auth.firebase_auth.time.time", return_value=1_000.0 + fa._CACHE_TTL + 1):
        assert fa._get_cached_token("t1") is None
    assert "t1" not in fa._token_cache


def test_the_sweep_past_a_thousand_entries_drops_only_the_expired():
    with patch("app.auth.firebase_auth.time.time", return_value=1_000.0):
        for n in range(1_000):
            fa._cache_token(f"old-{n}", {"n": n})
    with patch("app.auth.firebase_auth.time.time", return_value=1_000.0 + fa._CACHE_TTL + 1):
        fa._cache_token("fresh", {"n": "fresh"})
        assert "fresh" in fa._token_cache
        assert not any(k.startswith("old-") for k in fa._token_cache)


# ── optional_auth ───────────────────────────────────────────────────────────

def _credentials(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def test_no_credentials_is_a_visitor():
    assert _run(fa.optional_auth(SimpleNamespace(headers={}), None)) is None


def test_a_rejected_token_is_a_visitor_not_a_401():
    with patch("app.auth.firebase_auth.verify_firebase_token", side_effect=ValueError("expired")):
        assert _run(fa.optional_auth(SimpleNamespace(headers={}), _credentials("bad"))) is None
    assert fa._get_cached_token("bad") is None


def test_a_good_token_resolves_the_member_and_is_cached():
    with fake_world() as world:
        uid = world.new_user("member")
        firebase_uid = world.users[uid]["firebase_uid"]
        decoded = {"uid": firebase_uid, "email": "member@example.com", "email_verified": True, "name": "M"}
        with patch("app.auth.firebase_auth.verify_firebase_token", return_value=decoded) as verify:
            first = world.run(fa.optional_auth(SimpleNamespace(headers={}), _credentials("good")))
            second = world.run(fa.optional_auth(SimpleNamespace(headers={}), _credentials("good")))
        assert first["firebase_uid"] == firebase_uid and first["user_id"] == str(uid)
        assert second == first and verify.call_count == 1


def test_two_members_never_share_an_entry():
    with fake_world() as world:
        a, b = world.new_user("a"), world.new_user("b")
        tokens = {"tok-a": {"uid": world.users[a]["firebase_uid"]}, "tok-b": {"uid": world.users[b]["firebase_uid"]}}
        with patch("app.auth.firebase_auth.verify_firebase_token", side_effect=lambda token: tokens[token]):
            who_a = world.run(fa.optional_auth(SimpleNamespace(headers={}), _credentials("tok-a")))
            who_b = world.run(fa.optional_auth(SimpleNamespace(headers={}), _credentials("tok-b")))
        assert who_a["user_id"] == str(a) and who_b["user_id"] == str(b)
        assert fa._get_cached_token("tok-a")["user_id"] != fa._get_cached_token("tok-b")["user_id"]
