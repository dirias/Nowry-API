"""PASS-001..005 (ADR-092, FR-044..048): passwords are changed and set through the API.

Change: the current password is checked, a wrong one counts (5 an hour), the new
one is set, older sessions are cut at Firebase's revocation time, and one notice
goes out. Set (Google-only): a fresh Google sign-in, no existing password, the
password added, older sessions cut at the current sign-in, one notice.
"""
import time
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402

NEW = "N3w-password!"
OLD = "0ld-password!"


@pytest.fixture
def world():
    with fake_world() as w:
        uid = w.new_user("ana", email="ana@example.com", email_verified=True, preferences={"general": {"language": "es"}})
        w.act_as(uid)
        w.uid = uid
        yield w


def _patched(providers=("password",), current_ok=True, revoked_at=None):
    from app.services import account_password as svc

    sets = AsyncMock()
    send = AsyncMock(return_value="sent")
    patches = [
        patch.object(svc, "providers", AsyncMock(return_value=set(providers))),
        patch.object(svc, "check_current", AsyncMock(return_value=current_ok)),
        patch.object(svc, "set_password", sets),
        patch.object(svc, "revocation_time", AsyncMock(return_value=revoked_at or int(time.time()))),
        patch.object(svc, "send_email", send),
    ]
    return patches, sets, send


def _call(world, path, body, **kw):
    patches, sets, send = _patched(**kw)
    for p in patches:
        p.start()
    try:
        response = world.http.post(path, json=body)
    finally:
        for p in patches:
            p.stop()
    return response, sets, send


def _user(world):
    return world.run(world.raw["users"].find_one({"_id": world.uid}))


# ── PASS-001: change ─────────────────────────────────────────────────────────

def test_the_right_current_password_changes_it_cuts_older_sessions_and_tells_once(world):
    response, sets, send = _call(world, "/auth/password/change", {"current_password": OLD, "new_password": NEW, "language": "fr"}, revoked_at=1_800_000_000)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "changed"
    sets.assert_awaited_once()
    assert sets.await_args.args[1] == NEW
    assert _user(world)["sessions_valid_after"] == 1_800_000_000
    send.assert_awaited_once()
    assert send.await_args.args[:3] == ("account.password", "ana@example.com", "fr")
    assert _user(world).get("password_changed_at") is not None


def test_a_wrong_current_password_is_refused_and_changes_nothing(world):
    response, sets, send = _call(world, "/auth/password/change", {"current_password": "nope-nope", "new_password": NEW}, current_ok=False)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "wrong_password"
    sets.assert_not_awaited()
    send.assert_not_awaited()
    assert "sessions_valid_after" not in _user(world)


def test_five_wrong_attempts_lock_the_change_for_the_hour(world):
    codes = [_call(world, "/auth/password/change", {"current_password": "nope-nope", "new_password": NEW}, current_ok=False)[0].status_code for _ in range(5)]
    assert codes == [400] * 5
    locked, sets, _ = _call(world, "/auth/password/change", {"current_password": OLD, "new_password": NEW}, current_ok=True)
    assert locked.status_code == 429
    assert locked.json()["detail"]["code"] == "too_many_attempts" and locked.json()["detail"]["retry_after"] > 0
    sets.assert_not_awaited()  # not even the right password gets through while locked


def test_a_short_new_password_is_refused(world):
    response, sets, _ = _call(world, "/auth/password/change", {"current_password": OLD, "new_password": "short"})
    assert response.status_code == 422
    sets.assert_not_awaited()


def test_a_google_only_account_has_no_password_to_change(world):
    response, sets, _ = _call(world, "/auth/password/change", {"current_password": OLD, "new_password": NEW}, providers=("google.com",))
    assert response.status_code == 409 and response.json()["detail"]["code"] == "no_password"
    sets.assert_not_awaited()


# ── PASS-002: set ────────────────────────────────────────────────────────────

def test_a_google_only_account_sets_a_password_after_a_fresh_google_sign_in(world):
    signed_in = int(time.time()) - 30
    world.current["user"]["auth_time"] = signed_in
    response, sets, send = _call(world, "/auth/password/set", {"new_password": NEW}, providers=("google.com",))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "set"
    sets.assert_awaited_once()
    assert _user(world)["sessions_valid_after"] == signed_in  # this device stays, older ones go
    assert send.await_args.args[:3] == ("account.password", "ana@example.com", "es")  # the stored language


def test_setting_a_password_needs_a_sign_in_less_than_5_minutes_old(world):
    world.current["user"]["auth_time"] = int(time.time()) - 600
    response, sets, _ = _call(world, "/auth/password/set", {"new_password": NEW}, providers=("google.com",))
    assert response.status_code == 401 and response.json()["detail"]["code"] == "reauth_required"
    sets.assert_not_awaited()


def test_an_account_with_a_password_changes_it_instead_of_setting_one(world):
    response, sets, _ = _call(world, "/auth/password/set", {"new_password": NEW}, providers=("google.com", "password"))
    assert response.status_code == 409 and response.json()["detail"]["code"] == "has_password"
    sets.assert_not_awaited()


# ── PASS-003, PASS-004, PASS-005 ─────────────────────────────────────────────

def test_the_notice_renders_in_five_languages_with_the_support_address():
    from app.emails.render import KINDS, render_email

    assert KINDS["account.password"] == {"class": "account", "sender": "account", "button": "none"}
    for locale in ("en", "es", "fr", "de", "ja"):
        rendered = render_email("account.password", locale, {"email": "a@example.com", "support": "support@nowry.app"}, logo_url="x")
        assert "support@nowry.app" in rendered.text and rendered.subject


@pytest.mark.asyncio
async def test_the_google_only_reset_email_links_to_set_a_password(monkeypatch):
    from app.services import account_email
    from app.services import email as email_service

    monkeypatch.setenv("FRONTEND_URL", "https://www.nowry.app")
    send = AsyncMock(return_value="sent")
    with patch.object(email_service, "send_email", send):
        await account_email.send_google_notice("ana@example.com", "en")
    assert send.await_args.args[3]["link"] == "https://www.nowry.app/set-password"


def test_our_verified_flag_is_read_from_firebase_never_set_alone():
    """PASS-005: `email_verified` follows the token Firebase signs, so the two cannot disagree."""
    from app.auth.email_verification import verification_update

    assert verification_update({"email_verified": False}, {"email_verified": True})["email_verified"] is True
    assert verification_update({"email_verified": None}, {"email_verified": False}) == {"email_verified": False}
    assert verification_update({"email_verified": True}, {"email_verified": True}) == {}


@pytest.mark.asyncio
async def test_the_cut_is_never_older_than_the_change_even_when_firebase_answers_late(monkeypatch):
    """Found by the dev suite: Firebase can still answer with its earlier
    valid-after (here, when the account was made) just after the password is
    updated. The cut then fell before another device's sign-in and that device
    stayed in. The cut is never older than the moment the change began."""
    from unittest.mock import MagicMock

    from app.services import account_password as svc

    began = int(time.time())
    stale = MagicMock(tokens_valid_after_timestamp=(began - 600) * 1000)
    monkeypatch.setattr(svc.firebase_admin_auth, "get_user", lambda uid: stale)
    assert await svc.revocation_time("uid-1", not_before=began) == began

    fresh = MagicMock(tokens_valid_after_timestamp=(began + 1) * 1000)
    monkeypatch.setattr(svc.firebase_admin_auth, "get_user", lambda uid: fresh)
    assert await svc.revocation_time("uid-1", not_before=began) == began + 1
